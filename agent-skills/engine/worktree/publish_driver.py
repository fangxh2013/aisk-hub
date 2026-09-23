# -*- coding: utf-8 -*-
"""Guarded publisher for task-worktree lands to the personal integration remote.

The only network write is an ordinary push of local fxh to origin/fxh-dev.
Remote refs are not fetched: the server performs the normal fast-forward check
as part of that push. Notification delivery is left to the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import gitops as git
from . import publish_pending

INTEGRATION_BRANCH = "fxh"
PUSH_REMOTE = "origin"
PUSH_BRANCH = "fxh-dev"
PUSH_REFSPEC = "fxh:refs/heads/fxh-dev"
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PublishReport:
    task_id: str
    repo_alias: str
    operation_key: str | None
    landed_sha: str | None
    local_tip: str | None
    remote_sha: str | None
    attempted: bool
    published: bool
    status: str
    failure_class: str | None
    message: str
    events: tuple[str, ...] = ()
    state: dict | None = None


class _PublishGate(Exception):
    def __init__(self, failure_class, message):
        super().__init__(message)
        self.failure_class = failure_class
        self.message = message


def _run(git_ops, args, cwd):
    try:
        return git_ops.run(args, cwd=cwd, check=False)
    except Exception as error:
        raise _PublishGate(publish_pending.UNKNOWN, "读取 Git 发布配置失败") from error


def _config_values(git_ops, repo, key):
    result = _run(git_ops, ["config", "--local", "--get-all", key], repo)
    if result.returncode == 1 and not (result.stdout or "").strip():
        return []
    if result.returncode != 0:
        raise _PublishGate(publish_pending.UNKNOWN, "无法确认 Git 本地配置")
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _single_config(git_ops, repo, key, expected, *, optional=False):
    values = _config_values(git_ops, repo, key)
    if optional and not values:
        return
    if values != [expected]:
        raise _PublishGate(
            publish_pending.PROTECTION,
            f"Git 配置 {key} 必须唯一且等于 {expected}",
        )


def _resolved_urls(git_ops, repo):
    raw_urls = _config_values(git_ops, repo, "remote.origin.url")
    raw_push_urls = _config_values(git_ops, repo, "remote.origin.pushurl")
    if len(raw_urls) != 1 or len(raw_push_urls) > 1:
        raise _PublishGate(publish_pending.UNKNOWN, "origin URL 缺失或配置了多个地址")

    fetch = _run(git_ops, ["remote", "get-url", "--all", "origin"], repo)
    push = _run(git_ops, ["remote", "get-url", "--push", "--all", "origin"], repo)
    if fetch.returncode != 0 or push.returncode != 0:
        raise _PublishGate(publish_pending.UNKNOWN, "无法解析 origin 的读写地址")
    fetch_urls = [line.strip() for line in (fetch.stdout or "").splitlines() if line.strip()]
    push_urls = [line.strip() for line in (push.stdout or "").splitlines() if line.strip()]
    if len(fetch_urls) != 1 or len(push_urls) != 1 or fetch_urls[0] != push_urls[0]:
        raise _PublishGate(publish_pending.UNKNOWN, "origin 读写地址不唯一或不一致")
    if raw_push_urls and raw_push_urls[0] != raw_urls[0]:
        raise _PublishGate(publish_pending.UNKNOWN, "origin pushurl 与 origin url 不一致")

    mirror_values = _config_values(git_ops, repo, "remote.origin.mirror")
    if any(value.lower() in _TRUE_VALUES for value in mirror_values):
        raise _PublishGate(publish_pending.PROTECTION, "origin mirror 模式禁止自动推送")
    return fetch_urls[0]


def _validate_upstream(git_ops, repo):
    _single_config(git_ops, repo, "branch.fxh.remote", PUSH_REMOTE)
    _single_config(git_ops, repo, "branch.fxh.merge", "refs/heads/fxh-dev")
    _single_config(git_ops, repo, "branch.fxh.pushRemote", PUSH_REMOTE, optional=True)
    _single_config(git_ops, repo, "remote.pushDefault", PUSH_REMOTE, optional=True)

    upstream = _run(git_ops, ["rev-parse", "--abbrev-ref", "fxh@{upstream}"], repo)
    if upstream.returncode != 0 or (upstream.stdout or "").strip() != "origin/fxh-dev":
        raise _PublishGate(
            publish_pending.PROTECTION,
            "fxh upstream 必须精确解析为 origin/fxh-dev",
        )


def _validate_task_repo(cfg, task, alias, repo_cfg):
    if getattr(cfg, "profile_name", None) != "xinhua" or alias not in ("be", "web"):
        raise _PublishGate(
            publish_pending.PROTECTION,
            "fxh-dev 自动发布仅允许 xinhua 档案的 be/web 仓库",
        )
    if repo_cfg.workspace_mode != "task-worktree":
        raise _PublishGate(publish_pending.PROTECTION, "仅允许 task-worktree 仓库自动推送")
    policy = repo_cfg.automatic or {}
    if (policy.get("land_to_local") != INTEGRATION_BRANCH
            or cfg.integration != INTEGRATION_BRANCH
            or policy.get("push_only") != PUSH_BRANCH):
        raise _PublishGate(
            publish_pending.PROTECTION,
            "自动发布策略必须精确为 land_to_local=fxh、integration=fxh、push_only=fxh-dev",
        )
    expected_origin_url = policy.get("expected_origin_url")
    if (not isinstance(expected_origin_url, str) or not expected_origin_url.strip()
            or expected_origin_url != expected_origin_url.strip()):
        raise _PublishGate(
            publish_pending.PROTECTION,
            "be/web 档案必须显式配置 automatic.expected_origin_url，拒绝未知远端",
        )

    task_repo = (task.get("repos") or {}).get(alias)
    if not isinstance(task_repo, dict):
        raise _PublishGate(publish_pending.UNKNOWN, "任务未登记该仓库")
    task_branch = str(task_repo.get("branch") or "").strip()
    task_path = str(task_repo.get("path") or "").strip()
    if not task_branch or not task_path:
        raise _PublishGate(publish_pending.UNKNOWN, "任务仓库缺少 worktree 分支或路径记录")
    if task_branch in {"fxh", "fxh-dev", "dev", "main", "master"}:
        raise _PublishGate(publish_pending.PROTECTION, "任务分支不能是集成、远端发布或受保护分支")
    try:
        if Path(task_path).resolve() == Path(repo_cfg.path).resolve():
            raise _PublishGate(publish_pending.PROTECTION, "任务路径不能指向 direct 主工作区")
    except OSError as error:
        raise _PublishGate(publish_pending.UNKNOWN, "无法验证任务工作区路径") from error
    return task_repo


def _report(task_id, alias, operation_key, landed_sha, local_tip, *,
            remote_sha=None, attempted=False, published=False, state=None, failure_class=None,
            message="", events=()):
    current = state or {}
    return PublishReport(
        task_id=task_id,
        repo_alias=alias,
        operation_key=operation_key,
        landed_sha=landed_sha,
        local_tip=local_tip,
        remote_sha=remote_sha,
        attempted=attempted,
        published=published,
        status=current.get("status", "rejected"),
        failure_class=failure_class,
        message=message,
        events=tuple(events),
        state=current or None,
    )


def _persist_failure(store, operation_key, failure_class, message, now, policy=None):
    return store.record_failure(
        operation_key,
        failure_class,
        message,
        now=now,
        policy=policy,
    )


def _publish_task_branch_locked(cfg, reg, task, alias, landed_sha, *, now=None,
                                retry=False, store=None, git_ops=None):
    """Publish one landed task SHA through the sole permitted fxh-dev refspec.

    A repeated transient operation runs only when its persisted retry time is
    due. After the profile-configured blocked escalation, transient failures
    continue on the same hourly cadence without repeating the blocked event.
    Non-transient operations require explicit caller retry after remediation.
    Success is idempotent and its confirmed remote SHA is persisted before the
    caller writes the task row.
    The caller owns notification delivery for returned events.
    """
    git_ops = git_ops or git
    task_id = str((task or {}).get("id") or "").strip()
    repo_alias = str(alias or "").strip()
    expected_sha = str(landed_sha or "").strip().lower() or None
    operation_key = (
        f"{task_id}/{repo_alias}/{expected_sha}" if task_id and repo_alias and expected_sha else None
    )
    try:
        repo_cfg = cfg.repo(repo_alias)
        policy = publish_pending.resolve_policy(
            (getattr(repo_cfg, "automatic", None) or {}).get("publish_pending_policy")
        )
    except Exception:
        return _report(
            task_id, repo_alias, operation_key, expected_sha, None,
            failure_class=publish_pending.PROTECTION,
            message="发布重试策略无效，拒绝自动发布",
        )
    if store is None:
        store = publish_pending.PublishPendingStore(
            cfg.state_dir / "publish-pending", policy=policy,
        )

    if not task_id or not repo_alias:
        return _report(task_id, repo_alias, operation_key, expected_sha,
                       None, message="任务 ID 和仓库别名必填")
    if expected_sha is None:
        return _report(task_id, repo_alias, operation_key, None,
                       None, message="任务没有已落地 SHA，拒绝推送")
    if not _FULL_SHA.fullmatch(expected_sha):
        return _report(task_id, repo_alias, operation_key, expected_sha,
                       None, message="落地 SHA 格式无效，拒绝推送")
    try:
        if reg is None or not hasattr(reg, "load"):
            raise _PublishGate(publish_pending.UNKNOWN, "缺少任务登记簿，拒绝推送")
        latest_task = reg.load(task_id, must=False)
        if not isinstance(latest_task, dict):
            raise _PublishGate(publish_pending.UNKNOWN, "登记簿中不存在该任务，拒绝推送")
        task = latest_task
        task_repo = ((task.get("repos") or {}).get(repo_alias) or {})
        recorded_sha = str(task_repo.get("landed_sha") or "").strip().lower()
        if recorded_sha != expected_sha:
            raise _PublishGate(
                publish_pending.UNKNOWN,
                "调用 SHA 与登记簿中的 landed_sha 不一致，拒绝推送",
            )
    except _PublishGate as error:
        try:
            failed = _persist_failure(
                store, operation_key, error.failure_class, error.message, now, policy,
            )
            return _report(
                task_id, repo_alias, operation_key, expected_sha, None,
                state=failed.state, failure_class=error.failure_class,
                message=error.message, events=failed.events,
            )
        except Exception:
            return _report(task_id, repo_alias, operation_key, expected_sha, None,
                           failure_class=publish_pending.UNKNOWN,
                           message="登记簿校验失败且状态无法保存，拒绝推送")
    except Exception:
        try:
            failed = _persist_failure(
                store, operation_key, publish_pending.UNKNOWN, "读取任务登记簿失败", now,
                policy,
            )
            return _report(
                task_id, repo_alias, operation_key, expected_sha, None,
                state=failed.state, failure_class=publish_pending.UNKNOWN,
                message="读取任务登记簿失败，拒绝推送", events=failed.events,
            )
        except Exception:
            return _report(task_id, repo_alias, operation_key, expected_sha, None,
                           failure_class=publish_pending.UNKNOWN,
                           message="登记簿读取失败且状态无法保存，拒绝推送")

    prior_events = ()
    try:
        current_state = store.get(operation_key)
    except Exception:
        return _report(task_id, repo_alias, operation_key, expected_sha,
                       None, failure_class=publish_pending.UNKNOWN,
                       message="无法读取发布状态，拒绝推送")
    if current_state:
        if current_state.get("status") == publish_pending.PUBLISHED:
            return _report(task_id, repo_alias, operation_key, expected_sha,
                           None, remote_sha=(current_state.get("remote_sha")
                                             or task_repo.get("remote_sha")
                                             or task_repo.get("published_sha")),
                           published=True, state=current_state,
                           message="该任务 SHA 已发布")
        if current_state.get("status") in (
            publish_pending.PUSH_PENDING, publish_pending.NEEDS_ATTENTION,
            publish_pending.BLOCKED,
        ):
            try:
                advanced = store.advance(operation_key, now=now, policy=policy)
            except Exception:
                return _report(task_id, repo_alias, operation_key, expected_sha,
                               None, failure_class=publish_pending.UNKNOWN,
                               message="无法更新发布状态，拒绝推送")
            if advanced:
                current_state = advanced.state
                prior_events = advanced.events
            if (not retry
                    and not publish_pending.should_retry(current_state, now=now, policy=policy)):
                blocked = current_state.get("status") == publish_pending.BLOCKED
                return _report(task_id, repo_alias, operation_key, expected_sha,
                               None, state=current_state,
                               message=("发布已阻塞，需人工处理"
                                        if blocked else "发布重试尚未到期或需人工处理"),
                               events=prior_events)

    local_tip = None
    try:
        repo_cfg = cfg.repo(repo_alias)
        task_repo = _validate_task_repo(cfg, task, repo_alias, repo_cfg)
        integration_repo = cfg.integration_repo(repo_alias)

        if git_ops.current_branch(integration_repo) != INTEGRATION_BRANCH:
            raise _PublishGate(publish_pending.PROTECTION, "集成工作区当前分支不是 fxh")
        _validate_upstream(git_ops, integration_repo)
        origin_url = _resolved_urls(git_ops, integration_repo)
        expected_origin_url = (repo_cfg.automatic or {}).get("expected_origin_url")
        if origin_url != expected_origin_url:
            raise _PublishGate(
                publish_pending.PROTECTION,
                f"origin URL 与 xinhua.{repo_alias} 档案 automatic.expected_origin_url 不匹配，拒绝发布",
            )

        local_tip = git_ops.sha(integration_repo, "refs/heads/fxh")
        landed = git_ops.sha(integration_repo, expected_sha)
        if not local_tip or not landed or landed.lower() != expected_sha:
            raise _PublishGate(publish_pending.UNKNOWN, "无法确认 fxh 或 landed SHA 对应的提交")
        if not git_ops.is_ancestor(integration_repo, expected_sha, local_tip):
            raise _PublishGate(
                publish_pending.NON_FAST_FORWARD,
                "当前本地 fxh 不包含任务登记的 landed_sha",
            )
        # Re-resolve the effective fetch and push URLs at the write boundary.
        # The initial origin check above can become stale while SHA ancestry is
        # being verified; pushing the remote name "origin" would then use the
        # changed URL.
        origin_url = _resolved_urls(git_ops, integration_repo)
        if origin_url != expected_origin_url:
            raise _PublishGate(
                publish_pending.PROTECTION,
                f"推送前 origin URL 与 xinhua.{repo_alias} 档案 automatic.expected_origin_url 不匹配，拒绝发布",
            )
        # Recheck local branch and tip after URL resolution so these are the
        # final Git reads before the explicit push operation.
        if git_ops.current_branch(integration_repo) != INTEGRATION_BRANCH:
            raise _PublishGate(publish_pending.PROTECTION, "推送前集成工作区已离开 fxh")
        if git_ops.sha(integration_repo, "refs/heads/fxh") != local_tip:
            raise _PublishGate(publish_pending.UNKNOWN, "推送前 fxh 发生变化，请重新发布")
    except _PublishGate as error:
        try:
            failed = _persist_failure(
                store, operation_key, error.failure_class, error.message, now, policy,
            )
            return _report(
                task_id, repo_alias, operation_key, expected_sha, local_tip,
                state=failed.state, failure_class=error.failure_class,
                message=error.message, events=prior_events + failed.events,
            )
        except Exception:
            return _report(
                task_id, repo_alias, operation_key, expected_sha, local_tip,
                failure_class=publish_pending.UNKNOWN,
                message="发布前校验失败且状态无法保存，拒绝推送",
                events=prior_events,
            )
    except Exception:
        try:
            failed = _persist_failure(
                store, operation_key, publish_pending.UNKNOWN,
                "发布前验证失败", now, policy,
            )
            return _report(
                task_id, repo_alias, operation_key, expected_sha, local_tip,
                state=failed.state, failure_class=publish_pending.UNKNOWN,
                message="发布前验证失败，拒绝推送",
                events=prior_events + failed.events,
            )
        except Exception:
            return _report(
                task_id, repo_alias, operation_key, expected_sha, local_tip,
                failure_class=publish_pending.UNKNOWN,
                message="发布前验证失败且状态无法保存，拒绝推送",
                events=prior_events,
            )

    push_args = ["push", "--no-follow-tags", PUSH_REMOTE, PUSH_REFSPEC]
    try:
        pushed = git_ops.run(push_args, cwd=integration_repo, check=False)
        if pushed.returncode == 0:
            try:
                confirmed = git_ops.run(
                    ["ls-remote", "--heads", PUSH_REMOTE, "refs/heads/fxh-dev"],
                    cwd=integration_repo,
                    check=False,
                )
            except Exception:
                failure_class = publish_pending.UNKNOWN
                safe_message = "推送已返回成功，但读取远端 fxh-dev 失败"
                try:
                    failed = _persist_failure(
                        store, operation_key, failure_class, safe_message, now,
                        policy,
                    )
                    return _report(
                        task_id, repo_alias, operation_key, expected_sha, local_tip,
                        attempted=True, state=failed.state, failure_class=failure_class,
                        message=safe_message, events=prior_events + failed.events,
                    )
                except Exception:
                    return _report(
                        task_id, repo_alias, operation_key, expected_sha, local_tip,
                        attempted=True, failure_class=publish_pending.UNKNOWN,
                        message="推送已返回成功，但远端和发布状态均无法确认",
                        events=prior_events,
                    )
            lines = [line.split() for line in (confirmed.stdout or "").splitlines() if line.strip()]
            remote_sha = None
            if (confirmed.returncode == 0 and len(lines) == 1 and len(lines[0]) == 2
                    and lines[0][1] == "refs/heads/fxh-dev"
                    and _FULL_SHA.fullmatch(lines[0][0])):
                remote_sha = lines[0][0].lower()
            if remote_sha != str(local_tip).lower():
                failure_class = (
                    publish_pending.classify_failure(
                        None, (confirmed.stderr or confirmed.stdout or "").strip(),
                    )
                    if confirmed.returncode != 0 else publish_pending.UNKNOWN
                )
                safe_message = "推送已返回成功，但无法确认远端 fxh-dev 与本地 fxh 一致"
                try:
                    failed = _persist_failure(
                        store, operation_key, failure_class, safe_message, now,
                        policy,
                    )
                    return _report(
                        task_id, repo_alias, operation_key, expected_sha, local_tip,
                        attempted=True, state=failed.state, failure_class=failure_class,
                        message=safe_message, events=prior_events + failed.events,
                    )
                except Exception:
                    return _report(
                        task_id, repo_alias, operation_key, expected_sha, local_tip,
                        attempted=True, failure_class=publish_pending.UNKNOWN,
                        message="推送已返回成功，但远端和发布状态均无法确认",
                        events=prior_events,
                    )
            try:
                succeeded = store.record_success(
                    operation_key, now=now, remote_sha=remote_sha,
                )
                return _report(
                    task_id, repo_alias, operation_key, expected_sha, local_tip,
                    remote_sha=remote_sha,
                    attempted=True, published=True, state=succeeded.state,
                    message="已推送到 origin/fxh-dev",
                    events=prior_events + succeeded.events,
                )
            except Exception:
                return _report(
                    task_id, repo_alias, operation_key, expected_sha, local_tip,
                    remote_sha=remote_sha, attempted=True, published=True,
                    failure_class=publish_pending.UNKNOWN,
                    message="远端推送成功，但发布状态未能保存",
                    events=prior_events,
                )
        raw_error = (pushed.stderr or pushed.stdout or "").strip()
        failure_class = publish_pending.classify_failure(None, raw_error)
    except Exception:
        failure_class = publish_pending.UNKNOWN

    safe_message = f"推送 origin/fxh-dev 失败（{failure_class}）"
    try:
        failed = _persist_failure(
            store, operation_key, failure_class, safe_message, now, policy,
        )
        return _report(
            task_id, repo_alias, operation_key, expected_sha, local_tip,
            attempted=True, state=failed.state, failure_class=failure_class,
            message=safe_message, events=prior_events + failed.events,
        )
    except Exception:
        return _report(
            task_id, repo_alias, operation_key, expected_sha, local_tip,
            attempted=True, failure_class=publish_pending.UNKNOWN,
            message="推送结果或发布状态无法确认",
            events=prior_events,
        )


def publish_task_branch(cfg, reg, task, alias, landed_sha, *, now=None,
                        retry=False, store=None, git_ops=None):
    """Serialize publication per task/repo/SHA and run the guarded driver."""
    task_id = str((task or {}).get("id") or "").strip()
    repo_alias = str(alias or "").strip()
    expected_sha = str(landed_sha or "").strip().lower()
    operation_key = (
        f"{task_id}/{repo_alias}/{expected_sha}"
        if task_id and repo_alias and expected_sha else None
    )

    try:
        repo_cfg = cfg.repo(repo_alias)
        policy = publish_pending.resolve_policy(
            (getattr(repo_cfg, "automatic", None) or {}).get("publish_pending_policy")
        )
    except Exception:
        return _publish_task_branch_locked(
            cfg, reg, task, alias, landed_sha, now=now, retry=retry,
            store=store, git_ops=git_ops,
        )

    if store is None:
        store = publish_pending.PublishPendingStore(
            cfg.state_dir / "publish-pending", policy=policy,
        )
    if not operation_key or not hasattr(store, "operation_lock"):
        return _publish_task_branch_locked(
            cfg, reg, task, alias, landed_sha, now=now, retry=retry,
            store=store, git_ops=git_ops,
        )
    try:
        with store.operation_lock(operation_key):
            return _publish_task_branch_locked(
                cfg, reg, task, alias, landed_sha, now=now, retry=retry,
                store=store, git_ops=git_ops,
            )
    except Exception:
        return _report(
            task_id, repo_alias, operation_key,
            expected_sha if expected_sha else None, None,
            failure_class=publish_pending.UNKNOWN,
            message="无法获取发布单写者锁，拒绝并发发布",
        )
