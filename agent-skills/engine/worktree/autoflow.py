# -*- coding: utf-8 -*-
"""Explicit task-finish entry point for the authorized Xinhua code repos.

One invocation advances one task through task-branch commit, gate, ready,
local fxh land, recoverable archive, and fxh-dev-only publish. Every phase is
persisted before the next irreversible step so re-running the command resumes
instead of duplicating work.
"""
from __future__ import annotations

from types import SimpleNamespace

from . import gitops as git, integrate, publish_driver, publish_notify, tasks
from .config import WtConfig, WtError
from .registry import Registry, now_iso, say


def _covers(path, declarations):
    normalized = str(path).replace("\\", "/")
    for raw in declarations:
        candidate = str(raw).replace("\\", "/").rstrip("/")
        if (tasks.gates.path_match(normalized, [candidate, candidate + "/**"])
                or normalized == candidate or normalized.startswith(candidate + "/")):
            return True
    return False


def _assert_automatic_policy(cfg: WtConfig, task, alias):
    if cfg.profile_name != "xinhua" or alias not in ("be", "web"):
        raise WtError("自动完成流水线只允许新华后端 be 与前端 web 仓库")
    if list(task.get("repos") or {}) != [alias]:
        raise WtError("前后端必须各自使用一个仓库任务；请拆成 be/web 独立任务后分别完成")
    repo = cfg.repo(alias)
    auto = repo.automatic
    if (repo.workspace_mode != "task-worktree" or cfg.integration != "fxh"
            or auto.get("commit_task_branch") is not True
            or auto.get("land_to_local") != "fxh" or auto.get("push_only") != "fxh-dev"
            or auto.get("archive_after_verified_land") is not True):
        raise WtError(f"{alias} 未通过精确的 fxh 自动完成授权配置")
    if task.get("direct_checkout"):
        raise WtError("direct checkout 任务不能进入前后端 worktree 自动流水线")
    return repo


def _call_args(task_id, alias, args, *, paths=None, message=None):
    return SimpleNamespace(
        task=task_id, repos=alias,
        paths=list(paths if paths is not None else (getattr(args, "paths", []) or [])),
        all=False, message=(message if message is not None else getattr(args, "message", "")),
        autoflow=True, dry_run=False,
        tool=getattr(args, "tool", None), session=getattr(args, "session", None),
    )


def _assert_ready_checkpoint(task, alias):
    row = task["repos"][alias]
    ready_sha = row.get("ready_sha")
    result = ((task.get("check") or {}).get("results") or {}).get(alias) or {}
    if not ready_sha or not result.get("ok") or result.get("sha") != ready_sha:
        raise WtError(f"{alias} queued/ready 恢复缺少与 ready SHA 一致的通过门禁记录；需先按原任务状态修复，不会重新提交")


def _normalized_finish_paths(repo_path, requested, checkpoint=None):
    """Canonicalize paths before applying task scope and reject repo root."""
    paths = list(requested or [])
    if not paths and isinstance(checkpoint, dict):
        paths = list(checkpoint.get("paths") or [])
    if not paths:
        raise WtError("提交前必须逐个声明 --path，避免把任务外或生成文件带入提交")
    normalized = tasks._commit_paths(repo_path, paths, False)
    # _commit_paths resolves '..' and symlinks. Check the result, never the
    # caller's raw string, or e.g. src/.. can become a repository-wide add.
    if any(path in ("", ".") for path in normalized):
        raise WtError("finish 的 --path 规范化后指向整个仓库；拒绝提交")
    return list(dict.fromkeys(normalized))


def _checkpoint_for_resume(task_repo, repo_path, branch, requested_paths, requested_message):
    checkpoint = task_repo.get("autoflow_checkpoint")
    if checkpoint is None:
        return None
    if (not isinstance(checkpoint, dict) or checkpoint.get("version") != 1
            or checkpoint.get("branch") != branch
            or not isinstance(checkpoint.get("sha"), str)
            or not isinstance(checkpoint.get("paths"), list)
            or not checkpoint.get("paths")
            or not isinstance(checkpoint.get("message"), str)
            or not checkpoint.get("message")):
        raise WtError("自动完成 checkpoint 格式无效；保留现场并停止")
    if git.current_branch(repo_path) != branch or git.sha(repo_path, "HEAD") != checkpoint["sha"]:
        raise WtError("任务分支 HEAD 已偏离自动完成 checkpoint；保留现场并停止，请人工核对")
    dirty = bool(git.dirty(repo_path))
    if dirty:
        return None
    if requested_paths and list(requested_paths) != checkpoint["paths"]:
        raise WtError("工作区干净但 --path 与已提交 checkpoint 不同；请沿用原路径恢复门禁")
    if requested_message and requested_message != checkpoint["message"]:
        raise WtError("工作区干净但提交说明与已提交 checkpoint 不同；请沿用原说明恢复门禁")
    return checkpoint


def _notify_publish_events(cfg, task, alias, row, events):
    if not events:
        return False
    log_path = cfg.state_dir / "publish-notifications.json"
    operation_key = str(row.get("publish_operation_key") or "")
    failed = False
    for event_type in dict.fromkeys(str(event) for event in events):
        event = {
            "task_id": str(task["id"]),
            "alias": alias,
            "operation_key": operation_key,
            "type": event_type,
        }
        outcome = publish_notify.notify_publish_event(event, log_path=log_path)
        if outcome.status in ("notified", "duplicate"):
            if outcome.status == "notified":
                say("warn" if event_type != "resolved" else "ok",
                    f"桌面通知已发送：{task['id']} {alias} {event_type}")
        else:
            failed = True
            say("err" if outcome.status == "failed" else "warn",
                f"桌面通知未送达，事件已保留待重试：{task['id']} {alias} {event_type}"
                + (f"（{outcome.error}）" if outcome.error else ""))
    return failed


def cmd_finish(cfg: WtConfig, reg: Registry, args):
    task = reg.find_by_ref(args.task)
    alias = str(args.repo or "").strip()
    if not alias or alias not in (task.get("repos") or {}):
        raise WtError(f"任务 {task['id']} 不包含仓库 {alias or '（未指定）'}")
    repo_cfg = _assert_automatic_policy(cfg, task, alias)
    if getattr(args, "all", False):
        raise WtError("finish 不接受 --all；请逐个传入本任务明确允许提交的 --path")
    state = task.get("state")
    recover_ready = state in ("ready", "queued")
    if state == "rejected":
        try:
            _assert_ready_checkpoint(task, alias)
            recover_ready = True
        except WtError:
            # A rejected task without a valid checkpoint still needs a
            # path-scoped repair commit. A valid checkpoint can resume land
            # without inventing new paths or attempting an empty commit.
            recover_ready = False
    if state in ("active", "parked") or (state == "rejected" and not recover_ready):
        task_repo_path = task["repos"][alias]["path"]
        row = task["repos"][alias]
        checkpoint = row.get("autoflow_checkpoint")
        normalized_paths = _normalized_finish_paths(task_repo_path, args.paths, checkpoint)
        scope = task.get("scope") or []
        if scope and any(not _covers(path, scope) for path in normalized_paths):
            raise WtError("至少一个规范化后的 --path 超出任务声明 scope；拒绝提交")
        message = str(getattr(args, "message", "") or "").strip()
        resume_checkpoint = _checkpoint_for_resume(
            row, task_repo_path, row["branch"], normalized_paths, message,
        )
        if resume_checkpoint:
            message = resume_checkpoint["message"]
        elif isinstance(checkpoint, dict) and isinstance(checkpoint.get("message"), str):
            # A prior gate failure may have left new in-scope edits. Reuse the
            # original commit message when the caller only supplies paths.
            message = message or checkpoint["message"]
        staged = [p for p in git.out(["diff", "--cached", "--name-only", "-z"], cwd=task_repo_path).split("\0") if p]
        if any(not _covers(path, normalized_paths) for path in staged):
            raise WtError("任务 worktree 已暂存的文件超出本次 --path；拒绝把它们混入自动提交")
        tool, sessions = tasks.caller(args)
        tasks.refuse_if_foreign(cfg, task, tool, sessions, "自动完成")
        local = _call_args(task["id"], alias, args, paths=normalized_paths, message=message)
        if not resume_checkpoint:
            rc = tasks.cmd_commit(cfg, reg, local)
            if rc:
                return rc
        rc = tasks.cmd_check(cfg, reg, local)
        if rc:
            say("err", "门禁失败；任务 worktree 保留，修复后重跑 finish")
            return rc
        rc = tasks.cmd_ready(cfg, reg, local)
        if rc:
            return rc
        task = reg.load(task["id"])
        rc = integrate.land_automatic(cfg, reg, task, alias)
        if rc:
            say("err", "自动落地未完成；任务现场保留，按原因修复后重跑 finish")
            return rc
        task = reg.load(task["id"])
    elif recover_ready:
        if args.paths or getattr(args, "all", False):
            raise WtError(f"任务状态 {state} 已有可恢复的 ready 提交；重跑 finish 不接受新的 --path/--all/commit")
        _assert_ready_checkpoint(task, alias)
        rc = integrate.land_automatic(cfg, reg, task, alias)
        if rc:
            say("err", "自动落地未完成；任务现场保留，按原因修复后重跑 finish")
            return rc
        task = reg.load(task["id"])

    if task.get("state") == "landed":
        row = task["repos"][alias]
        sha = row.get("landed_sha") or row.get("ready_sha")
        repo = repo_cfg.path
        if not sha or not git.sha(repo, "fxh") or not git.is_ancestor(repo, sha, "fxh"):
            raise WtError("本地 fxh 尚未验证包含任务 SHA；保留 worktree，不执行回收")
        row["publish_operation_key"] = f"{task['id']}/{alias}/{sha}"
        row["automatic_publish_started_at"] = row.get("automatic_publish_started_at") or now_iso()
        reg.save(task)
        rc = tasks.cmd_archive(cfg, reg, SimpleNamespace(task=task["id"], force=True, abandon=False,
                                                           tool="human", session=None))
        if rc:
            return rc
        task = reg.load(task["id"])

    if task.get("state") == "archived":
        row = task["repos"][alias]
        sha = row.get("landed_sha") or row.get("ready_sha")
        if not sha:
            raise WtError("归档任务缺少目标 SHA；拒绝发布")
        if row.get("remote_sha") and row.get("publish_status") == "published":
            say("ok", f"{task['id']} 已发布并完成，无需重复处理")
            return 0
        result = publish_driver.publish_task_branch(cfg, reg, task, alias, sha)
        task = reg.load(task["id"])
        row = task["repos"][alias]
        status = getattr(result, "status", None)
        remote_sha = getattr(result, "remote_sha", None)
        for event in getattr(result, "events", ()):
            label = "已阻塞" if event == "blocked" else "需要人工关注" if event == "needs_attention" else "已恢复"
            say("warn" if event != "resolved" else "ok",
                f"{task['id']} {alias} 发布状态升级：{label}（fxh-dev）")
        notification_failed = _notify_publish_events(cfg, task, alias, row,
                                                       getattr(result, "events", ()))
        if remote_sha:
            row["remote_sha"] = remote_sha
        if status:
            row["publish_status"] = status
        if remote_sha and status == "published" and getattr(result, "published", False):
            row.pop("publish_error", None)
            reg.save(task)
            say("ok", f"{task['id']} 已确认 origin/fxh-dev 包含 {remote_sha[:9]}")
            tasks.refresh_board(cfg, reg)
            return 1 if notification_failed else 0
        detail = getattr(result, "message", "发布尚未确认")
        row["publish_error"] = str(detail)[:500]
        reg.save(task)
        say("warn", f"任务已安全落地并回收 worktree；origin/fxh-dev 仍待发布：{detail}")
        tasks.refresh_board(cfg, reg)
        return 1

    if task.get("state") not in ("landed", "archived"):
        raise WtError(f"自动流水线无法从任务状态 {task.get('state')} 恢复；任务记录与现场保持不动")
    return 0
