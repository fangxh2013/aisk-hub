# -*- coding: utf-8 -*-
"""Explicit task-finish entry point for the authorized Xinhua code repos.

One invocation advances one task through task-branch commit, gate, ready,
local fxh land, recoverable archive, and fxh-dev-only publish. Every phase is
persisted before the next irreversible step so re-running the command resumes
instead of duplicating work.
"""
from __future__ import annotations

from types import SimpleNamespace

from . import gitops as git, integrate, publish_driver, tasks
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


def _call_args(task_id, alias, args):
    return SimpleNamespace(
        task=task_id, repos=alias, paths=list(getattr(args, "paths", []) or []),
        all=False, message=getattr(args, "message", ""), dry_run=False,
        tool=getattr(args, "tool", None), session=getattr(args, "session", None),
    )


def _assert_ready_checkpoint(task, alias):
    row = task["repos"][alias]
    ready_sha = row.get("ready_sha")
    result = ((task.get("check") or {}).get("results") or {}).get(alias) or {}
    if not ready_sha or not result.get("ok") or result.get("sha") != ready_sha:
        raise WtError(f"{alias} queued/ready 恢复缺少与 ready SHA 一致的通过门禁记录；需先按原任务状态修复，不会重新提交")


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
        if not args.paths:
            raise WtError("提交前必须逐个声明 --path，避免把任务外或生成文件带入提交")
        if any(str(path).replace("\\", "/").rstrip("/") in ("", ".") for path in args.paths):
            raise WtError("finish 的 --path 不能指向整个仓库；请列出具体文件或目录")
        scope = task.get("scope") or []
        if scope and any(not _covers(path, scope) for path in args.paths):
            raise WtError("至少一个 --path 超出任务声明 scope；拒绝提交")
        task_repo_path = task["repos"][alias]["path"]
        staged = [p for p in git.out(["diff", "--cached", "--name-only", "-z"], cwd=task_repo_path).split("\0") if p]
        if any(not _covers(path, args.paths) for path in staged):
            raise WtError("任务 worktree 已暂存的文件超出本次 --path；拒绝把它们混入自动提交")
        tool, sessions = tasks.caller(args)
        tasks.refuse_if_foreign(cfg, task, tool, sessions, "自动完成")
        local = _call_args(task["id"], alias, args)
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
        if remote_sha:
            row["remote_sha"] = remote_sha
        if status:
            row["publish_status"] = status
        if remote_sha and status == "published" and getattr(result, "published", False):
            row.pop("publish_error", None)
            reg.save(task)
            say("ok", f"{task['id']} 已确认 origin/fxh-dev 包含 {remote_sha[:9]}")
            tasks.refresh_board(cfg, reg)
            return 0
        detail = getattr(result, "message", "发布尚未确认")
        row["publish_error"] = str(detail)[:500]
        reg.save(task)
        say("warn", f"任务已安全落地并回收 worktree；origin/fxh-dev 仍待发布：{detail}")
        tasks.refresh_board(cfg, reg)
        return 1

    if task.get("state") not in ("landed", "archived"):
        raise WtError(f"自动流水线无法从任务状态 {task.get('state')} 恢复；任务记录与现场保持不动")
    return 0
