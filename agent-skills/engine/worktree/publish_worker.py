# -*- coding: utf-8 -*-
"""Deterministic scheduler for archived task-worktree publications.

The worker owns selection, due-time checks, and task-record persistence. The
injected publisher owns the actual publish operation; this module performs no
Git/network operation and does not deliver notifications.
"""
from __future__ import annotations

import datetime as dt
import inspect
import re
from dataclasses import dataclass

from . import publish_driver, publish_pending

_ALIASES = ("be", "web")
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)


@dataclass(frozen=True)
class PublicationResult:
    task_id: str
    alias: str
    operation_key: str
    landed_sha: str
    status: str
    remote_sha: str | None = None
    error: str | None = None
    attempted: bool = False
    skipped_reason: str | None = None
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerReport:
    results: tuple[PublicationResult, ...]
    events: tuple[dict, ...]


def _utc(value=None):
    if value is None:
        value = dt.datetime.now(dt.timezone.utc)
    elif isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, dt.datetime):
        raise TypeError("now must be a datetime, ISO timestamp, or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _policy_allows(cfg, alias):
    if alias not in _ALIASES or getattr(cfg, "profile_name", None) != "xinhua":
        return False
    if getattr(cfg, "integration", None) != "fxh":
        return False
    try:
        repo = cfg.repo(alias)
    except Exception:
        return False
    policy = getattr(repo, "automatic", None) or {}
    return (
        getattr(repo, "workspace_mode", None) == "task-worktree"
        and policy.get("commit_task_branch") is True
        and policy.get("land_to_local") == "fxh"
        and policy.get("push_only") == "fxh-dev"
        and policy.get("archive_after_verified_land") is True
    )


def _report_value(report, name, default=None):
    if isinstance(report, dict):
        return report.get(name, default)
    return getattr(report, name, default)


def _invoke_publisher(publisher, cfg, reg, task, alias, landed_sha, now):
    """Pass the clock override when supported; never opt into manual retry."""
    try:
        signature = inspect.signature(publisher)
        supports_now = (
            "now" in signature.parameters
            or any(param.kind == inspect.Parameter.VAR_KEYWORD
                   for param in signature.parameters.values())
        )
    except (TypeError, ValueError):
        supports_now = True
    args = (cfg, reg, task, alias, landed_sha)
    return publisher(*args, now=now) if supports_now else publisher(*args)


def _event(task_id, alias, operation_key, event):
    return {
        "task_id": task_id,
        "alias": alias,
        "operation_key": operation_key,
        "type": str(event),
    }


def _unique_events(events):
    seen = set()
    unique = []
    for event in events:
        marker = str(event)
        if marker not in seen:
            seen.add(marker)
            unique.append(marker)
    return tuple(unique)


def _advance_state(store, operation_key, now, policy=None):
    state = store.get(operation_key)
    if state is None:
        return None, ()
    advanced = store.advance(operation_key, now=now, policy=policy)
    if advanced is None:
        return state, ()
    return advanced.state, tuple(advanced.events or ())


def _is_due(state, now, policy=None):
    if not state:
        # An archived row with a persisted operation key and no outcome may be
        # the crash window between archive and the first publish attempt.
        return True
    if state.get("status") == publish_pending.PUBLISHED:
        return False
    return publish_pending.should_retry(state, now=now, policy=policy)


def run_due_publications(cfg, reg, now=None, publisher=None):
    """Run due publications for eligible archived be/web task-worktree rows.

    Task rows must persist both ``publish_operation_key`` and a full
    ``landed_sha``. A pending record is retried only when its persisted retry
    time is due; the initial crash-recovery attempt is allowed when no pending
    state exists. The publisher is called without ``retry=True`` so its own
    guards remain authoritative.

    The returned report includes notification intents as events. No transport
    is invoked here.
    """
    current = _utc(now)
    publish = publisher or publish_driver.publish_task_branch
    if reg is None or not hasattr(reg, "all"):
        raise TypeError("reg must provide all(include_archived=True, include_hub=False)")

    rows = reg.all(include_archived=True, include_hub=False)
    store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
    results = []
    emitted = []

    for listed in sorted(rows or [], key=lambda item: str(item.get("id") or "")):
        task_id = str((listed or {}).get("id") or "").strip()
        if not task_id or listed.get("state") != "archived":
            continue
        repos = listed.get("repos")
        if not isinstance(repos, dict) or len(repos) != 1:
            continue
        alias = next(iter(repos))
        if not _policy_allows(cfg, alias):
            continue
        try:
            repo_cfg = cfg.repo(alias)
            retry_policy = publish_pending.resolve_policy(
                (getattr(repo_cfg, "automatic", None) or {}).get("publish_pending_policy")
            )
        except Exception:
            results.append(PublicationResult(
                task_id, alias, "", "", status="rejected",
                error="发布重试策略无效，已跳过该任务",
                skipped_reason="invalid_policy",
            ))
            continue

        try:
            task = reg.load(task_id, must=False) if hasattr(reg, "load") else listed
        except Exception:
            continue
        if not isinstance(task, dict) or task.get("state") != "archived":
            continue
        task_repos = task.get("repos")
        if not isinstance(task_repos, dict) or list(task_repos) != [alias]:
            continue
        task_repo = task_repos.get(alias)
        if not isinstance(task_repo, dict):
            continue

        landed_sha = str(task_repo.get("landed_sha") or "").strip().lower()
        operation_key = str(task_repo.get("publish_operation_key") or "").strip()
        expected_key = f"{task_id}/{alias}/{landed_sha}" if landed_sha else ""
        if (not _FULL_SHA.fullmatch(landed_sha)
                or operation_key != expected_key
                or not _policy_allows(cfg, alias)):
            continue

        if (task_repo.get("publish_status") == publish_pending.PUBLISHED
                and task_repo.get("remote_sha")):
            results.append(PublicationResult(
                task_id, alias, operation_key, landed_sha,
                status=publish_pending.PUBLISHED,
                remote_sha=str(task_repo["remote_sha"]), attempted=False,
                skipped_reason="already_published",
            ))
            continue

        try:
            state, advance_events = _advance_state(
                store, operation_key, current, retry_policy,
            )
        except Exception as error:
            results.append(PublicationResult(
                task_id, alias, operation_key, landed_sha,
                status="unknown", error=f"发布状态读取失败：{type(error).__name__}",
                skipped_reason="state_unavailable",
            ))
            continue

        for event in advance_events:
            emitted.append(_event(task_id, alias, operation_key, event))

        if state and state.get("status") == publish_pending.PUBLISHED:
            confirmed_sha = state.get("remote_sha") or task_repo.get("remote_sha")
            changed = task_repo.get("publish_status") != publish_pending.PUBLISHED
            task_repo["publish_status"] = publish_pending.PUBLISHED
            if confirmed_sha:
                changed = changed or task_repo.get("remote_sha") != str(confirmed_sha)
                task_repo["remote_sha"] = str(confirmed_sha)
                changed = changed or "publish_error" in task_repo
                task_repo.pop("publish_error", None)
                missing_sha_error = None
            else:
                missing_sha_error = "发布状态已完成，但登记记录中缺少已确认的远端 SHA"
                changed = changed or task_repo.get("publish_error") != missing_sha_error
                task_repo["publish_error"] = missing_sha_error
            if changed and hasattr(reg, "save"):
                reg.save(task)
            results.append(PublicationResult(
                task_id, alias, operation_key, landed_sha,
                status=publish_pending.PUBLISHED,
                remote_sha=str(confirmed_sha) if confirmed_sha else None,
                error=missing_sha_error,
                attempted=False, skipped_reason="already_published_state",
            ))
            continue

        if not _is_due(state, current, retry_policy):
            if state:
                new_status = state.get("status")
                changed = new_status is not None and task_repo.get("publish_status") != new_status
                if new_status:
                    task_repo["publish_status"] = new_status
                error_text = state.get("last_error")
                if error_text and task_repo.get("publish_error") != str(error_text)[:500]:
                    task_repo["publish_error"] = str(error_text)[:500]
                    changed = True
                if changed and hasattr(reg, "save"):
                    reg.save(task)
            results.append(PublicationResult(
                task_id, alias, operation_key, landed_sha,
                status=str((state or {}).get("status") or "idle"),
                error=(str(state.get("last_error")) if state and state.get("last_error") else None),
                attempted=False, skipped_reason="not_due",
                events=tuple(advance_events),
            ))
            continue

        try:
            report = _invoke_publisher(publish, cfg, reg, task, alias, landed_sha, current)
        except Exception as error:
            message = f"发布执行器异常：{type(error).__name__}: {error}"[:500]
            try:
                failed = store.record_failure(
                    operation_key, publish_pending.UNKNOWN, message, now=current,
                    policy=retry_policy,
                )
                state = failed.state
                result_events = _unique_events(tuple(advance_events) + tuple(failed.events or ()))
            except Exception:
                state = state or {}
                result_events = _unique_events(advance_events)
            status = str(state.get("status") or publish_pending.NEEDS_ATTENTION)
            task_repo["publish_status"] = status
            task_repo["publish_error"] = message
            if hasattr(reg, "save"):
                reg.save(task)
            for event in result_events:
                emitted.append(_event(task_id, alias, operation_key, event))
            results.append(PublicationResult(
                task_id, alias, operation_key, landed_sha,
                status=status, error=message, attempted=True,
                events=result_events,
            ))
            continue

        status = str(_report_value(report, "status", "unknown") or "unknown")
        remote_sha = _report_value(report, "remote_sha")
        message = str(_report_value(report, "message", "") or "")
        failure_class = _report_value(report, "failure_class")
        if status == publish_pending.PUBLISHED and not remote_sha:
            message = message or "发布器未返回已确认的远端 SHA"
        result_events = _unique_events(
            tuple(advance_events) + tuple(_report_value(report, "events", ()) or ())
        )
        task_repo["publish_status"] = status
        if remote_sha:
            task_repo["remote_sha"] = str(remote_sha)
        if status == publish_pending.PUBLISHED and remote_sha:
            task_repo.pop("publish_error", None)
        elif message or failure_class:
            task_repo["publish_error"] = str(message or failure_class)[:500]
        if hasattr(reg, "save"):
            reg.save(task)
        for event in result_events:
            emitted.append(_event(task_id, alias, operation_key, event))
        results.append(PublicationResult(
            task_id, alias, operation_key, landed_sha,
            status=status,
            remote_sha=str(remote_sha) if remote_sha else None,
            error=(message or (str(failure_class) if failure_class else None))
            if status != publish_pending.PUBLISHED or not remote_sha else None,
            attempted=bool(_report_value(report, "attempted", True)),
            events=result_events,
        ))

    unique_emitted = []
    seen_emitted = set()
    for item in emitted:
        marker = (item["task_id"], item["alias"], item["operation_key"], item["type"])
        if marker not in seen_emitted:
            seen_emitted.add(marker)
            unique_emitted.append(item)
    return WorkerReport(tuple(results), tuple(unique_emitted))
