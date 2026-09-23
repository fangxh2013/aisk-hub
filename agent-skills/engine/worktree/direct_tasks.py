# -*- coding: utf-8 -*-
"""Task lifecycle for explicitly configured ordinary Git checkouts.

Direct mode is intended for documentation and the two explicitly authorized
Aisk repositories. It never creates a worktree or branch. A persistent
repository lease and a clean branch/HEAD/scope baseline prevent cooperating
agents from writing the same ordinary checkout at once.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .. import direct_checkout
from . import gates, gitops as git, model, publish_pending, tasks
from .config import WtConfig, WtError
from .registry import Registry, now_iso, say


DIRECT_MASTER_PUSH_ALLOWLIST = {
    ("aisk-hub", "main"): "master",
    ("aisk-private", "main"): "master",
}


def _policy(cfg: WtConfig, alias: str):
    repo = cfg.repo(alias)
    if repo.workspace_mode != "direct":
        raise WtError(f"{alias} 不是 direct 仓库")
    auto = repo.automatic
    branch = str(auto.get("commit_branch") or "")
    if not branch or branch.lower() == "main":
        raise WtError(f"{alias} 未配置安全的 automatic.commit_branch，或目标命中 main")
    if repo.path.resolve() == Path("/").resolve() or not repo.path.exists():
        raise WtError(f"{alias} 普通检出不存在：{repo.path}")
    if git.current_branch(repo.path) != branch:
        raise WtError(f"{alias} 当前分支为 {git.current_branch(repo.path)}，要求保持在 {branch}；不会自动切分支")
    push_branch = auto.get("push_branch")
    if push_branch:
        if (cfg.profile_name, alias) not in DIRECT_MASTER_PUSH_ALLOWLIST or branch != "master" or push_branch != "master":
            raise WtError("direct 自动 push 只允许 aisk-hub/main 与 aisk-private/main 的 master → origin/master")
        expected_origin = auto.get("expected_origin_url")
        if not isinstance(expected_origin, str) or not expected_origin.strip():
            raise WtError("direct master 推送必须配置 automatic.expected_origin_url")
    return repo, branch, push_branch


def _direct_task(reg: Registry, ref):
    task = reg.find_by_ref(ref)
    if not task.get("direct_checkout"):
        raise WtError(f"{task['id']} 不是 direct-checkout 任务")
    return task


def _lease(cfg, task, alias):
    return direct_checkout.acquire_repo_lease(
        cfg.repo(alias).path, owner=task["id"], runtime_root=cfg.data_root,
    )


def _validate(cfg, task, alias, lease):
    row = task["repos"][alias]
    baseline = direct_checkout.RepoBaseline.from_dict(row["direct_baseline"])
    return direct_checkout.validate_task_changes(cfg.repo(alias).path, baseline, lease=lease)


def cmd_new(cfg: WtConfig, reg: Registry, args):
    alias = args.repo
    repo_cfg, branch, push_branch = _policy(cfg, alias)
    slug = model.validate_slug(cfg, args.slug)
    title = model.validate_title(args.title)
    goal, accept = (args.goal or "").strip(), (args.accept or "").strip()
    if not goal or not accept:
        raise WtError("direct 任务必须写明 --goal 与 --accept")
    scopes = list(args.scope or [])
    if not scopes:
        raise WtError("direct 任务必须至少声明一个 --scope 仓库相对路径")
    tool, sessions = tasks.caller(args)
    if tool and tool != "human" and not sessions:
        raise WtError("未识别到会话号，请设置 AISK_SESSION 或传 --session")

    with reg.lock():
        check = SimpleNamespace(paths=[], all=False)
        # Direct work consumes an active task slot but never a worktree slot.
        tasks.check_quota(cfg, reg, repos=[], materialize=[])
        tid = reg.next_id()
        name = f"{tid}-{slug}"
        td = cfg.tasks_dir / name
        if td.exists():
            raise WtError(f"任务目录已存在：{td}")
        lease = direct_checkout.acquire_repo_lease(repo_cfg.path, owner=tid, runtime_root=cfg.data_root)
        try:
            baseline = direct_checkout.capture_baseline(repo_cfg.path, scopes, lease=lease)
            td.mkdir(parents=True)
            row = {
                "branch": branch, "path": str(repo_cfg.path), "base_ref": branch,
                "base_sha": baseline.head, "start_ref": branch, "ready_sha": None,
                "landed_sha": None, "direct_baseline": baseline.to_dict(),
            }
            task = {
                "id": tid, "slug": slug, "name": name, "title": title, "goal": goal,
                "accept": accept, "os": cfg.os, "dir": str(td), "port_block": None,
                "ports": None, "base_task": None, "scope": scopes, "repos": {alias: row},
                "source_branches": {}, "state": "active", "owner": None,
                "created_at": now_iso(), "history": [], "direct_checkout": True,
                "direct_push_branch": push_branch,
            }
            task["owner"] = tasks.new_owner(cfg, tool, sessions) if tool else None
            if tool:
                tasks.append_progress(task, tasks.progress_line(tool, "开始 direct checkout 任务并取得仓库写租约"))
            reg.save(task)
            reg.event("direct-new", task=tid, title=title, alias=alias, branch=branch, scope=scopes)
        except BaseException as error:
            if lease.held:
                try:
                    lease.release(tid)
                except direct_checkout.DirectCheckoutError:
                    pass
                lease.close()
            shutil.rmtree(td, ignore_errors=True)
            if isinstance(error, direct_checkout.DirectCheckoutError):
                raise WtError(str(error)) from error
            raise
        finally:
            lease.close()
    say("ok", f"{tid} 已创建 direct 任务：{alias}@{branch}；没有创建 worktree。仓库写租约将保持到任务完成")
    say("info", f"允许改动：{'、'.join(scopes)}；完成时运行 aisk task direct-finish {tid} -m \"type: 说明\"")
    tasks.refresh_board(cfg, reg)
    return 0


def cmd_resume(cfg: WtConfig, reg: Registry, args):
    tool, sessions = tasks.caller(args)
    if not tool:
        raise WtError("请用 --tool 说明认领者")
    if tool != "human" and not sessions:
        raise WtError("direct 任务续做需要 --session")
    with reg.lock():
        task = _direct_task(reg, args.task)
        if task.get("state") not in ("active", "parked", "rejected"):
            raise WtError(f"任务状态 {task.get('state')} 不能续做")
        lease_state, _activity = tasks.lease_of(cfg, task)
        prev = task.get("owner")
        allowed, why = model.claim_decision(prev, lease_state, tool, sessions, getattr(args, "takeover", False))
        if not allowed:
            raise WtError(f"{task['id']} {why}：当前执行者 {tasks.owner_label(prev)}；"
                          f"超过 {cfg.claimable_minutes} 分钟可显式接手")
        if why == "接手" and lease_state == model.IDLE and not (getattr(args, "reason", "") or "").strip():
            raise WtError("接手空闲 direct 任务必须写 --reason")
        alias = next(iter(task["repos"]))
        _policy(cfg, alias)
        lease = _lease(cfg, task, alias)
        try:
            with lease:
                _validate(cfg, task, alias, lease).raise_if_invalid()
                lease.heartbeat(task["id"])
                task["owner"] = tasks.new_owner(cfg, tool, sessions)
                if why == "接手":
                    tasks.append_progress(task, tasks.progress_line(tool, f"接手 direct 任务 · 原因：{args.reason}"))
                reg.save(task)
                reg.event("direct-resume", task=task["id"], tool=tool)
        finally:
            lease.close()
    say("ok", f"{task['id']} 已续做；direct checkout 写租约仍由此任务持有")
    tasks.refresh_board(cfg, reg)
    return 0


def _pending_key(task, alias, sha):
    return f"{task['id']}/{alias}/master/{sha}"


def _release_terminal_lease(cfg, task, alias):
    """Release only this archived task's leftover lease after a crash."""
    lease = direct_checkout.RepoLease(
        cfg.repo(alias).path, owner=task["id"], runtime_root=cfg.data_root,
    )
    with direct_checkout.file_lock(lease.lock_path, blocking=False) as acquired:
        if not acquired:
            return False
        record = lease._read_record()
        if (record is None or record.get("owner") != task["id"]
                or record.get("repo_root") != str(lease.repo_root)):
            return False
        try:
            lease.record_path.unlink()
        except OSError as error:
            raise WtError(f"终态任务仍持有 direct 租约，且自动释放失败：{error}") from error
        return True


def _patch_digest(patch):
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _nul_paths(repo, args):
    output = git.run(args, cwd=repo).stdout
    return tuple(path for path in output.split("\0") if path)


def _worktree_patch_digest(repo, paths):
    """Hash the exact Git-clean-filtered worktree snapshot without staging it."""
    fd, index_name = tempfile.mkstemp(prefix="aisk-direct-index-")
    os.close(fd)
    index_path = Path(index_name)
    try:
        # Git expects an absent path for a fresh alternate index.
        index_path.unlink()
        env = {"GIT_INDEX_FILE": str(index_path)}
        git.run(["read-tree", "HEAD"], cwd=repo, env_extra=env)
        if paths:
            git.run(["--literal-pathspecs", "add", "-A", "--", *paths], cwd=repo, env_extra=env)
        patch = git.run([
            "diff", "--cached", "--binary", "--full-index", "--no-renames",
            "--no-ext-diff", "--no-textconv", "HEAD", "--",
        ], cwd=repo, env_extra=env).stdout
        return _patch_digest(patch)
    finally:
        for candidate in (index_path, Path(str(index_path) + ".lock")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def _cached_patch_digest(repo):
    patch = git.run([
        "diff", "--cached", "--binary", "--full-index", "--no-renames",
        "--no-ext-diff", "--no-textconv", "HEAD", "--",
    ], cwd=repo).stdout
    return _patch_digest(patch)


def _pending_paths(pending):
    paths = pending.get("paths")
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(path, str) or not path for path in paths)):
        raise WtError("direct pending commit 缺少精确路径列表；拒绝自动恢复")
    canonical = tuple(sorted(set(paths)))
    if tuple(paths) != canonical:
        raise WtError("direct pending commit 路径列表不规范；拒绝自动恢复")
    return canonical


def _validate_pending_intent(repo, pending, baseline, message, changed):
    intended_paths, expected_digest = _pending_metadata(pending, baseline, message)
    if intended_paths != tuple(sorted(changed)):
        raise WtError("direct pending commit 改动路径已变化；拒绝自动恢复")
    if _worktree_patch_digest(repo, intended_paths) != expected_digest:
        raise WtError("direct pending commit 工作区快照已变化；拒绝自动恢复")
    return intended_paths, expected_digest


def _pending_metadata(pending, baseline, message):
    if pending.get("version") != 2:
        raise WtError("direct pending commit 缺少快照版本；需人工核对暂存区后再完成")
    if pending.get("base_sha") != baseline.head or pending.get("message") != message:
        raise WtError("direct pending commit 的基线或提交说明与本次命令不一致；拒绝恢复")
    intended_paths = _pending_paths(pending)
    expected_digest = pending.get("worktree_patch_sha256")
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)):
        raise WtError("direct pending commit 快照摘要无效；拒绝自动恢复")
    if not all(direct_checkout._within_scope(path, baseline.allowed_paths) for path in intended_paths):
        raise WtError("direct pending commit 含范围外路径；拒绝自动恢复")
    return intended_paths, expected_digest


def _recover_committed_pending(repo, validation, baseline, pending, message):
    """Recognize only the exact single commit described by a v2 pending intent."""
    intended_paths, expected_digest = _pending_metadata(pending, baseline, message)
    only_head_advanced = (
        validation.current_branch == baseline.branch
        and len(validation.baseline_mismatches) == 1
        and validation.baseline_mismatches[0].startswith("当前 HEAD 从")
        and not validation.out_of_scope
        and not git.dirty(repo)
    )
    if not only_head_advanced:
        raise direct_checkout.BaselineViolation(validation)
    parents = git.out(["show", "-s", "--format=%P", validation.current_head], cwd=repo).split()
    subject = git.out(["show", "-s", "--format=%B", validation.current_head], cwd=repo).strip()
    committed_paths = tuple(sorted(_nul_paths(repo, [
        "diff", "--no-renames", "--name-only", "-z", baseline.head,
        validation.current_head, "--",
    ])))
    committed_patch = git.run([
        "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
        "--no-textconv", baseline.head, validation.current_head, "--",
    ], cwd=repo).stdout
    allowed = tuple(baseline.allowed_paths)
    if (parents != [baseline.head] or subject != message
            or committed_paths != intended_paths
            or _patch_digest(committed_patch) != expected_digest
            or not all(direct_checkout._within_scope(path, allowed) for path in committed_paths)):
        raise direct_checkout.BaselineViolation(validation)
    return True


def _assert_expected_origin(repo, expected_origin):
    """Fail closed unless both effective origin URLs still match policy."""
    try:
        fetch_urls = git.out(["remote", "get-url", "--all", "origin"], cwd=repo).splitlines()
        push_urls = git.out(["remote", "get-url", "--push", "--all", "origin"], cwd=repo).splitlines()
    except WtError as error:
        raise WtError(f"无法核实 origin fetch/push URL；拒绝远端操作：{error}") from error
    if fetch_urls != [expected_origin] or push_urls != [expected_origin]:
        raise WtError(
            "origin fetch/push URL 必须各自且精确匹配 automatic.expected_origin_url；拒绝远端操作"
        )


def _push_master(cfg, task, alias, sha):
    repo_cfg, branch, push_branch = _policy(cfg, alias)
    if push_branch != "master" or (cfg.profile_name, alias) not in DIRECT_MASTER_PUSH_ALLOWLIST:
        raise WtError("此 direct 仓库没有精确的 aisk master 推送授权")
    repo = repo_cfg.path
    # The explicit destination refspec guarantees this path cannot write a
    # branch chosen by ambient push.default or an upstream misconfiguration.
    expected_origin = repo_cfg.automatic.get("expected_origin_url")
    if not isinstance(expected_origin, str) or not expected_origin:
        raise WtError("direct master 推送缺少 automatic.expected_origin_url；拒绝访问远端")
    _assert_expected_origin(repo, expected_origin)
    upstream_remote = git.out(["config", "--get", f"branch.{branch}.remote"], cwd=repo, check=False).strip()
    upstream_merge = git.out(["config", "--get", f"branch.{branch}.merge"], cwd=repo, check=False).strip()
    if upstream_remote != "origin" or upstream_merge != "refs/heads/master":
        raise WtError(f"{branch} upstream 必须精确为 origin/master（当前 {upstream_remote or '未设'}/{upstream_merge or '未设'}）")
    remote_ref = "refs/remotes/origin/master"
    git.run(["fetch", "--no-tags", expected_origin, "refs/heads/master:" + remote_ref], cwd=repo)
    _assert_expected_origin(repo, expected_origin)
    remote_sha = git.sha(repo, remote_ref)
    local_sha = git.sha(repo, "refs/heads/master")
    if not remote_sha or not local_sha or not git.is_ancestor(repo, remote_sha, local_sha):
        raise WtError("本地 master 不是 origin/master 的快进后继；保留本地提交并阻断推送")
    if sha != local_sha and not git.is_ancestor(repo, sha, local_sha):
        raise WtError("待发布任务提交不在本地 master 历史中")
    _assert_expected_origin(repo, expected_origin)
    actual = git.out(["ls-remote", "--heads", expected_origin, "refs/heads/master"], cwd=repo).split()
    if actual and actual[0] == local_sha:
        _assert_expected_origin(repo, expected_origin)
        return local_sha
    if actual and actual[0] != remote_sha:
        raise WtError("origin/master 在预检期间发生变化；拒绝继续推送")
    # Keep both checks: the first validates the completed ls-remote preflight;
    # the second is the final guard immediately before the remote write.
    _assert_expected_origin(repo, expected_origin)
    _assert_expected_origin(repo, expected_origin)
    git.run(["push", expected_origin, "refs/heads/master:refs/heads/master"], cwd=repo)
    _assert_expected_origin(repo, expected_origin)
    after = git.out(["ls-remote", "--heads", expected_origin, "refs/heads/master"], cwd=repo).split()
    if not after or after[0] != local_sha:
        raise WtError("推送命令返回成功，但 origin/master 未精确确认目标 SHA")
    return local_sha


def cmd_finish(cfg: WtConfig, reg: Registry, args):
    tool, sessions = tasks.caller(args)
    task = _direct_task(reg, args.task)
    if task.get("state") not in ("active", "parked", "queued", "rejected"):
        if task.get("state") == "archived":
            alias = next(iter(task["repos"]))
            _release_terminal_lease(cfg, task, alias)
            say("ok", f"{task['id']} 已完成，无需重复处理")
            return 0
        raise WtError(f"任务状态 {task.get('state')} 不能完成")
    alias = next(iter(task["repos"]))
    repo_cfg, branch, push_branch = _policy(cfg, alias)
    tasks.refuse_if_foreign(cfg, task, tool, tasks.caller(args)[1], "完成 direct 任务")
    message = (args.message or "").strip()
    err = model.check_message(cfg, message)
    if err:
        raise WtError(err)
    lease = _lease(cfg, task, alias)
    store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
    operation_key = None
    try:
        with lease:
            row = task["repos"][alias]
            validation = _validate(cfg, task, alias, lease)
            current_sha = git.sha(repo_cfg.path, "HEAD")
            pending_commit = row.get("pending_commit") or {}
            recovered = False
            baseline = direct_checkout.RepoBaseline.from_dict(row["direct_baseline"])
            if pending_commit and current_sha != baseline.head:
                if not _recover_committed_pending(
                        repo_cfg.path, validation, baseline, pending_commit, message):
                    raise direct_checkout.BaselineViolation(validation)
                recovered = True
            elif not validation.ok:
                raise direct_checkout.BaselineViolation(validation)

            changed = list(validation.changed_paths)
            if changed:
                if pending_commit:
                    intended_paths, expected_digest = _validate_pending_intent(
                        repo_cfg.path, pending_commit, baseline, message, changed,
                    )
                else:
                    staged = _nul_paths(repo_cfg.path, ["diff", "--cached", "--name-only", "-z"])
                    unmerged = git.run(["ls-files", "-u", "-z"], cwd=repo_cfg.path).stdout
                    if staged or unmerged:
                        raise WtError("direct checkout 存在未由本任务暂存或冲突的内容；拒绝混入提交")
                    intended_paths = tuple(sorted(changed))
                    expected_digest = _worktree_patch_digest(repo_cfg.path, intended_paths)
                    row["pending_commit"] = {
                        "version": 2, "base_sha": baseline.head, "message": message,
                        "paths": list(intended_paths), "worktree_patch_sha256": expected_digest,
                        "started_at": now_iso(),
                    }
                    reg.save(task)

                staged = _nul_paths(repo_cfg.path, ["diff", "--cached", "--name-only", "-z"])
                unmerged = git.run(["ls-files", "-u", "-z"], cwd=repo_cfg.path).stdout
                if unmerged:
                    raise WtError("direct checkout 暂存区存在冲突项；保留现场并阻断")
                if not staged:
                    git.run(["--literal-pathspecs", "add", "-A", "--", *intended_paths], cwd=repo_cfg.path)
                    staged = _nul_paths(repo_cfg.path, ["diff", "--cached", "--name-only", "-z"])
                if tuple(sorted(staged)) != intended_paths:
                    raise WtError("暂存范围与任务意图不一致；保留工作区和暂存区并阻断")
                if (_cached_patch_digest(repo_cfg.path) != expected_digest
                        or _worktree_patch_digest(repo_cfg.path, intended_paths) != expected_digest):
                    raise WtError("暂存区或工作区内容与已记录任务快照不一致；拒绝提交")
                validation_before_commit = _validate(cfg, task, alias, lease)
                if (not validation_before_commit.ok
                        or validation_before_commit.current_head != baseline.head
                        or tuple(sorted(validation_before_commit.changed_paths)) != intended_paths):
                    raise direct_checkout.BaselineViolation(validation_before_commit)
                # --only limits the commit to the persisted intent even if an
                # unrelated actor races a new staged path into the real index.
                git.run(["--literal-pathspecs", "commit", "--only", "-m", message,
                         "--", *intended_paths], cwd=repo_cfg.path)
                current_sha = git.sha(repo_cfg.path, "HEAD")
                parents = git.out(["show", "-s", "--format=%P", current_sha], cwd=repo_cfg.path).split()
                committed_message = git.out(["show", "-s", "--format=%B", current_sha], cwd=repo_cfg.path).strip()
                committed_paths = tuple(sorted(_nul_paths(repo_cfg.path, [
                    "diff", "--no-renames", "--name-only", "-z", baseline.head, current_sha, "--",
                ])))
                committed_patch = git.run([
                    "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
                    "--no-textconv", baseline.head, current_sha, "--",
                ], cwd=repo_cfg.path).stdout
                if (parents != [baseline.head] or committed_message != message
                        or committed_paths != intended_paths
                        or _patch_digest(committed_patch) != expected_digest
                        or git.dirty(repo_cfg.path)):
                    raise WtError("提交后的快照与 pending intent 不一致；保留现场并阻断自动完成")
                recovered = True
            elif not recovered and not row.get("ready_sha"):
                raise WtError("direct 任务没有待提交改动或已记录提交，不能标记完成")

            if recovered or changed:
                row["ready_sha"] = current_sha
                row.pop("pending_commit", None)
                new_baseline = direct_checkout.capture_baseline(
                    repo_cfg.path, baseline.allowed_paths, lease=lease,
                )
                row["direct_baseline"] = new_baseline.to_dict()
                reg.save(task)

            candidate = row.get("ready_sha")
            if not candidate or git.sha(repo_cfg.path, "HEAD") != candidate or git.dirty(repo_cfg.path):
                raise WtError("门禁前提交头变化或工作区变脏；拒绝发布")
            log = cfg.logs_dir / task["id"] / f"direct-check-{alias}-{candidate[:9]}.log"
            ok, summary = gates.run_gate(cfg, alias, repo_cfg.path, changed or task.get("scope", []), log, clean=True)
            if not ok:
                raise WtError(f"{alias} 门禁失败：{summary}（日志 {log}）")
            if git.sha(repo_cfg.path, "HEAD") != candidate or git.dirty(repo_cfg.path):
                raise WtError("门禁期间提交或工作区发生变化；拒绝完成")
            row["direct_gate"] = {"at": now_iso(), "ok": True, "summary": summary, "sha": candidate,
                                  "log": str(log)}

            if push_branch:
                operation_key = _pending_key(task, alias, candidate)
                row["publish_operation_key"] = operation_key
                try:
                    remote_sha = _push_master(cfg, task, alias, candidate)
                except (WtError, OSError) as error:
                    transition = store.record_failure(operation_key, message=str(error))
                    row["publish_pending_status"] = transition.state["status"]
                    row["publish_error"] = str(error)[:500]
                    reg.save(task)
                    reg.event("publish-pending", task=task["id"], alias=alias, sha=candidate,
                              status=transition.state["status"], events=list(transition.events))
                    raise WtError(f"本地提交 {candidate[:9]} 已保留，但 origin/master 发布待处理：{error}") from error
                transition = store.record_success(operation_key)
                row["remote_sha"] = remote_sha
                row["publish_pending_status"] = transition.state["status"]
                row.pop("publish_error", None)
            row["landed_sha"] = candidate
            row["direct_completed_at"] = now_iso()
            task["owner"] = None
            reg.save(task)
            reg.event("direct-finish", task=task["id"], alias=alias, sha=candidate,
                      remote_sha=row.get("remote_sha"), gate=summary)
            with reg.lock():
                reg.set_state(task, "archived", note=f"direct commit {candidate[:9]}" +
                              (f"; published {row['remote_sha'][:9]}" if row.get("remote_sha") else ""))
                lease.release(task["id"])
    except direct_checkout.DirectCheckoutError as error:
        raise WtError(str(error)) from error
    finally:
        lease.close()
    say("ok", f"{task['id']} 已提交 {branch}@{task['repos'][alias]['ready_sha'][:9]} 并完成 direct 任务；无 worktree 需回收")
    if task["repos"][alias].get("remote_sha"):
        say("ok", f"已确认 origin/{push_branch} 包含 {task['repos'][alias]['remote_sha'][:9]}")
    tasks.refresh_board(cfg, reg)
    return 0


def cmd_abort(cfg: WtConfig, reg: Registry, args):
    task = _direct_task(reg, args.task)
    if task.get("state") not in ("active", "parked", "rejected"):
        if task.get("state") == "archived":
            alias = next(iter(task["repos"]))
            _release_terminal_lease(cfg, task, alias)
            say("ok", f"{task['id']} 已归档，无需重复放弃")
            return 0
        raise WtError(f"任务状态 {task.get('state')} 不能放弃")
    alias = next(iter(task["repos"]))
    repo_cfg, _branch, _push = _policy(cfg, alias)
    lease = _lease(cfg, task, alias)
    try:
        with lease:
            validation = _validate(cfg, task, alias, lease)
            if not validation.ok or validation.changed_paths or task["repos"][alias].get("ready_sha"):
                raise WtError("direct checkout 有任务改动或提交；为避免丢失，只能先完成任务，不能放弃并释放租约")
            task["owner"] = None
            with reg.lock():
                reg.set_state(task, "archived", note="direct task aborted with clean baseline")
                lease.release(task["id"])
                reg.event("direct-abort", task=task["id"], alias=alias)
    except direct_checkout.DirectCheckoutError as error:
        raise WtError(str(error)) from error
    finally:
        lease.close()
    say("ok", f"{task['id']} 已在干净基线下放弃并释放 direct 仓库租约")
    tasks.refresh_board(cfg, reg)
    return 0
