# -*- coding: utf-8 -*-
"""任务生命周期：new / find / claim / release / heartbeat / note / pause / resume / status / open /
check / commit / ready / restack / archive / restore / salvage / merge-commit / adopt / adopt-branch / overlap。

锁的约定：登记簿锁（registry）不可重入，本模块内任何持锁代码都不再调用会取锁的函数；
耗时的 git worktree add 不在登记簿锁内执行——先写"创建中"占位记录保留编号、端口与标题，再建目录。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import actor, bind, gates, gitops as git, model, publish_pending, registry
from . import names
from .config import WtConfig, WtError
from ..action_context import ActionContext, ActionContextError
from .registry import (LIVE_STATES, WORKING_STATES, Registry, atomic_json, atomic_text, file_lock, now_iso,
                       parse_iso, remove_tree, say, stamp)

CREATING_STALE_SECONDS = 600
# 这里要覆盖 `bind.py::write_task_files()` 往任务目录写的**每一个**生成目录。
# 常规 `pause` 的快照根是 `td/<alias>`，任务目录本身不在其中；但
# `salvage --dir <任务目录>` 与旧 v1 布局（worktree 就是任务目录）会让它成为快照根——
# 那时生成的端级配置（含内核绝对路径）会被提交进 refs/aisk/wip。
# `.workbuddy-ai/` 与 `.aisk/` 都曾漏掉：桌面版 `.workbuddy/` 排了、AI 版没排，
# 而 `.aisk/`（env / env.cmd / task.json）同样是机器相关生成物。
SNAPSHOT_EXCLUDES = [".git", ".git.*", ".aisk/", ".claude/", ".codex/", ".cursor/", ".workbuddy/",
                     ".workbuddy-ai/", ".gemini/", ".codebuddy/", ".agents/", "AGENT-WORKTREE.md",
                     "node_modules/", "target/", ".DS_Store"]


def base_ref(cfg: WtConfig):
    return f"refs/remotes/mac/{cfg.integration}" if cfg.os == "windows" else cfg.integration


def is_local(cfg: WtConfig, task):
    return task.get("os") == cfg.os and not task.get("_from_hub")


def legacy_lanes(cfg: WtConfig):
    return cfg.legacy_root / "lanes" if cfg.legacy_root else None


def require_local(cfg: WtConfig, task, states=None):
    if task.get("direct_checkout"):
        raise WtError(f"任务 {task['id']} 使用 direct checkout；请使用 aisk task direct-resume/direct-finish")
    if not is_local(cfg, task):
        raise WtError(f"任务 {task['id']} 属于 {task.get('os')}，只能在该系统上操作")
    if task.get("creating"):
        raise WtError(f"任务 {task['id']} 正在创建中")
    if task.get("archiving"):
        raise WtError(f"任务 {task['id']} 正在归档（{task['archiving']}）；重跑 {names.CLI} archive {task['id']} 完成归档")
    if states is not None and task["state"] not in states:
        raise WtError(f"任务 {task['id']} 状态 {task['state']} 不允许此操作")
    td = Path(task["dir"]).resolve()
    if task.get("legacy"):
        lanes = legacy_lanes(cfg)
        inside = lanes is not None and td.parent.parent == lanes.resolve()
    else:
        inside = td.parent == cfg.tasks_dir.resolve()
    if not inside:
        raise WtError("登记目录不属于当前数据根")
    for alias, r in task["repos"].items():
        path = Path(r["path"])
        if path.resolve() != (td / alias).resolve():
            raise WtError(f"{alias}: 登记路径越界")
        good, info = git.backlink_ok(path)
        if not good:
            raise WtError(f"{alias}: {info}，停止操作并先抢救")
        if git.current_branch(path) != r["branch"]:
            raise WtError(f"{alias}: 当前分支不是 {r['branch']}")


def land_lock_path(cfg: WtConfig, alias):
    """落地 / 上主干 / 自动落地共用的仓库锁。`recover_stale_queued` 靠它判断落地进程是否还活着，
    所以所有持锁点都必须经由这里取路径，不能各自拼字符串。"""
    return cfg.locks_dir / f"land-{alias}.lock"


def recover_stale_queued(cfg: WtConfig, reg: Registry, task):
    """把没有落地进程在处理的 queued 任务恢复为 rejected，返回最新任务记录。

    land 在等确认弹窗（最长 600 秒）之前就把任务置为 queued；如果进程在这期间被杀
    （AI 工具命令超时、会话结束、中断），退回 rejected 的清理代码不会执行，任务就永久
    卡在 queued：commit / ready / pause 全都拒绝，也没有恢复命令，只能手改登记簿，
    还一直占着活跃槽位。2026-09-24 T028 就这样冻结了 10 小时。

    land 与自动落地都是先拿到本仓库的落地锁、再置 queued，并持锁到状态改走为止；
    flock 在进程退出时由内核自动释放。所以能非阻塞地拿到本任务**全部**仓库的落地锁，
    就证明此刻没有进程在落地它——queued 只能是遗留状态。持锁期间重读并改为 rejected
    （与确认超时的正常失败路径同一终态），land / 自动落地 / finish 都能从 rejected 续跑，
    已快进的部分由重新 land 的既有核对逻辑处理。任何一把锁拿不到就原样返回：可能正有
    落地在进行，宁可下次再判，也不在进行中的落地底下改状态。"""
    if task.get("state") != "queued" or task.get("direct_checkout") or not is_local(cfg, task):
        return task
    aliases = [a for a in (task.get("repos") or {}) if a in cfg.repos]
    if not aliases:
        return task
    with contextlib.ExitStack() as stack:
        for alias in aliases:
            if not stack.enter_context(file_lock(land_lock_path(cfg, alias), blocking=False)):
                return task
        current = reg.load(task["id"])
        if current.get("state") != "queued":
            return current
        reason = "上次落地进程已中断（落地锁无人持有），queued 自动恢复为 rejected；重新 land 会核对已落地部分"
        current["last_reject"] = {"at": now_iso(), "reason": reason}
        reg.set_state(current, "rejected", note=reason)
        say("warn", f"{current['id']}: {reason}")
        return current


# ------------------------------------------------------------------ 活动与租约
def activity_ts(cfg: WtConfig, task, quick=False):
    """最近一次活动：心跳、任务分支上的新提交、未提交文件的修改时间、PROGRESS.md 修改时间，取最大。
    quick=True 跳过未提交文件扫描（看板刷新用；认领判定一律用完整计算）。"""
    stamps = []
    owner = task.get("owner") or {}
    hb = parse_iso(owner.get("heartbeat_at"))
    if hb:
        stamps.append(hb.timestamp())
    if is_local(cfg, task) and not task.get("creating"):
        for r in task["repos"].values():
            path = Path(r["path"])
            if not path.exists():
                continue
            head = git.sha(path, "HEAD")
            if head and head != r.get("base_sha"):
                ct = git.commit_time(path, "HEAD")
                if ct:
                    stamps.append(ct)
            if not quick:
                try:
                    dirty = git.dirty_paths(path)[:200]
                except (OSError, WtError):
                    # 旧任务目录可能被外部清理；看板必须报告异常而不是拖垮
                    # 新任务的创建、认领和提交。
                    dirty = []
                for p in dirty:
                    try:
                        stamps.append((path / p).stat().st_mtime)
                    except OSError:
                        pass
        prog = Path(task["dir"]) / "PROGRESS.md"
        if prog.exists():
            stamps.append(prog.stat().st_mtime)
    else:
        upd = parse_iso(task.get("updated_at"))
        if upd:
            stamps.append(upd.timestamp())
    return max(stamps) if stamps else None


def lease_of(cfg: WtConfig, task, now_ts=None, quick=False):
    now_ts = now_ts or time.time()
    act = activity_ts(cfg, task, quick=quick)
    return model.lease_state(task.get("owner"), act, now_ts, cfg), act


def new_owner(cfg: WtConfig, tool, sessions):
    t = now_iso()
    return {"tool": tool, "sessions": list(sessions or []), "os": cfg.os, "host": socket.gethostname(),
            "claimed_at": t, "heartbeat_at": t}


def owner_label(owner):
    return owner.get("tool", "?") if owner else "无人"


def progress_line(who, text):
    return f"- {now_iso()[:16].replace('T', ' ')} · {who} · {text}"


def append_progress(task, line):
    prog = Path(task["dir"]) / "PROGRESS.md"
    text = prog.read_text(encoding="utf-8") if prog.exists() else f"# 进度：{task['id']} {task['title']}\n\n"
    if not text.endswith("\n"):
        text += "\n"
    atomic_text(prog, text + line.rstrip("\n") + "\n")


def last_next(task):
    if task.get("_summary"):
        return task.get("next_step") or ""
    prog = Path(task["dir"]) / "PROGRESS.md"
    if not prog.exists():
        return ""
    for line in reversed(prog.read_text(encoding="utf-8").splitlines()):
        m = re.search(r"下一步：([^·]+)", line)
        if m:
            return m.group(1).strip()
    return ""


def caller(args):
    tool = getattr(args, "tool", None) or actor.detect_tool()
    return tool, (actor.session_ids(tool, getattr(args, "session", None)) if tool else [])


def refuse_if_foreign(cfg: WtConfig, task, tool, sessions, what):
    """AI 调用者不能对别人持有中的任务做写操作；操作者（识别不到 AI 工具）不受限。"""
    owner = task.get("owner")
    if not owner or not tool or tool == "human" or model.same_actor(owner, tool, sessions):
        return
    lease, act = lease_of(cfg, task)
    if lease == model.HELD:
        raise WtError(f"{task['id']} 由 {owner.get('tool')} 持有中（{model.age_text(act, time.time())}活动），"
                      f"不能{what}；只读查看，或等它空闲后 {names.CLI} claim {task['id']} --takeover --reason …")


def heartbeat(reg: Registry, task, tool, sessions):
    owner = task.get("owner")
    if not owner or not model.same_actor(owner, tool, sessions):
        return False
    owner["heartbeat_at"] = now_iso()
    reg.save(task)
    return True


# ------------------------------------------------------------------ new
def parse_refs(items, repos, flag):
    refs = {}
    for item in items or []:
        if "=" not in item:
            raise WtError(f"{flag} 写法：<仓库别名>=<引用>")
        k, v = item.split("=", 1)
        if k not in repos or not v or v.startswith("-"):
            raise WtError(f"{flag} 必须指定本任务包含的仓库与有效引用")
        refs[k] = v
    return refs


def free_port_block(reg: Registry):
    used = {t.get("port_block") for t in reg.all(include_hub=False)}
    for n in range(1, 900):
        if n not in used:
            return n
    raise WtError("端口块已用尽，请归档不用的任务")


ACTIVE_WORKTREE_STATES = ("active", "ready", "queued", "rejected")
# 槽位按任务状态计，不看会话死活：只停掉 AI 会话，任务仍是 active、仍占槽位。
FREE_SLOT_HINT = "暂停（aisk task pause）、落地或归档其中一个任务才会释放槽位；只停掉 AI 会话不会释放"


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def worktree_quota_snapshot(cfg: WtConfig, reg: Registry):
    """Count task worktrees independently of task state and legacy task quotas.

    Git registrations under tasks_dir are the physical source of truth. Creation
    reservations cover the short interval between registry reservation and
    `git worktree add`; this prevents two concurrent creators from taking the
    same last slot. Anchors and ordinary checkouts live outside tasks_dir.
    """
    physical = {alias: set() for alias in cfg.repo_order}
    active = {alias: set() for alias in cfg.repo_order}
    holders = {alias: set() for alias in cfg.repo_order}
    for alias in cfg.repo_order:
        rc = cfg.repo(alias)
        try:
            if not rc.path.is_dir() or not git.ok(["rev-parse", "--git-dir"], cwd=rc.path):
                continue
        except OSError:
            # A profile can outlive a missing checkout or point at an
            # incomplete clone. Quota accounting is read-only and must not
            # turn that unrelated repository problem into a task-creation
            # crash; the command that targets that repo still fails closed.
            continue
        for wt in git.worktree_list(rc.path):
            path = Path(wt["path"])
            if _inside(path, cfg.tasks_dir):
                physical.setdefault(alias, set()).add(str(path.resolve()))

    tasks_local = [t for t in reg.all(include_archived=True, include_hub=False) if is_local(cfg, t)]
    for task in tasks_local:
        is_active = task.get("state") in ACTIVE_WORKTREE_STATES
        reservations = set(task.get("creating_repos") or []) if task.get("creating") else set()
        for alias, row in (task.get("repos") or {}).items():
            if alias not in cfg.repos or cfg.repo(alias).workspace_mode != "task-worktree":
                continue
            path = str(Path(row.get("path") or "").resolve())
            if path and _inside(path, cfg.tasks_dir) and Path(path).exists():
                physical.setdefault(alias, set()).add(path)
                if is_active:
                    active.setdefault(alias, set()).add(path)
                    holders.setdefault(alias, set()).add(f"{task['id']}({task.get('state')})")
        for alias in reservations:
            if alias not in cfg.repos or cfg.repo(alias).workspace_mode != "task-worktree":
                continue
            path = str((Path(task["dir"]) / alias).resolve())
            physical.setdefault(alias, set()).add(path)
            if is_active:
                active.setdefault(alias, set()).add(path)
                holders.setdefault(alias, set()).add(f"{task['id']}(创建中)")
    return {
        "physical_by_repo": {alias: len(paths) for alias, paths in physical.items()},
        "active_by_repo": {alias: len(paths) for alias, paths in active.items()},
        "active_holders_by_repo": {alias: sorted(ids) for alias, ids in holders.items()},
        "physical_total": sum(len(paths) for paths in physical.values()),
        "active_total": sum(len(paths) for paths in active.values()),
    }


def check_quota(cfg: WtConfig, reg: Registry, exclude=None, repos=None, materialize=None):
    """Enforce task-record quota plus optional per-repo physical worktree caps.

    `repos` are new active slots requested by task creation/resume. `materialize`
    counts a new physical checkout; it differs during resume because a paused
    task already owns its worktree. Callers hold the registry lock.
    """
    working = [t for t in reg.all(include_hub=False)
               if t.get("state") in WORKING_STATES and t["id"] != exclude and is_local(cfg, t)]
    if len(working) >= cfg.quota_active:
        raise WtError(f"本机进行中的任务已达上限 {cfg.quota_active}（档案 worktrees.quotas.active），请先 aisk task pause 或归档")
    snapshot = worktree_quota_snapshot(cfg, reg)
    holders = snapshot["active_holders_by_repo"]
    wanted = [a for a in (repos or []) if a in cfg.repos and cfg.repo(a).workspace_mode == "task-worktree"]
    projected_active = snapshot["active_total"] + len(wanted)
    if cfg.quota_active_worktrees and projected_active > cfg.quota_active_worktrees:
        occupied = "、".join(f"{a}:{','.join(ids)}" for a, ids in holders.items() if ids) or "无"
        raise WtError(f"活跃任务 worktree 已占 {snapshot['active_total']}/{cfg.quota_active_worktrees} 个"
                      f"（{occupied}）；本次需要 {len(wanted)} 个。{FREE_SLOT_HINT}")
    for alias in wanted:
        limit = cfg.repo(alias).limits.get("active_worktrees")
        used = snapshot["active_by_repo"].get(alias, 0)
        if limit is not None and used + 1 > limit:
            occupied = "、".join(holders.get(alias) or []) or "无"
            raise WtError(f"仓库 {alias} 活跃 worktree 已占 {used}/{limit} 个（{occupied}）；{FREE_SLOT_HINT}")
    materializing = [a for a in (materialize if materialize is not None else repos or [])
                     if a in cfg.repos and cfg.repo(a).workspace_mode == "task-worktree"]
    if materializing and cfg.tasks_dir.is_dir():
        used_bytes = dir_size(cfg.tasks_dir)
        budget_bytes = int(float(cfg.retention.get("disk_budget_gb", 10)) * (1 << 30))
        if used_bytes >= budget_bytes:
            used_gb = used_bytes / (1 << 30)
            budget_gb = budget_bytes / (1 << 30)
            raise WtError(
                f"任务目录磁盘占用已达 {used_gb:.2f}/{budget_gb:.2f}GB，拒绝新建物化 worktree；"
                "不会自动清理，请先运行 aisk task gc 预览可回收项"
            )
    projected_physical = snapshot["physical_total"] + len(materializing)
    if cfg.quota_materialized and projected_physical > cfg.quota_materialized:
        raise WtError(f"任务 worktree 物理槽位已占 {snapshot['physical_total']}/{cfg.quota_materialized} 个；"
                      f"本次需要 {len(materializing)} 个。不会自动驱逐暂停任务；请查看 aisk task gc 预览")
    for alias in materializing:
        limit = cfg.repo(alias).limits.get("materialized_worktrees")
        used = snapshot["physical_by_repo"].get(alias, 0)
        if limit is not None and used + 1 > limit:
            raise WtError(f"仓库 {alias} 物理 worktree 已占 {used}/{limit} 个；不会自动清理现场")


def java_home(cfg: WtConfig, aliases):
    for alias in aliases:
        gate = cfg.repo(alias).gate
        if gate.get("jdk") or gate.get("jdk_home"):
            return gates.java_home_for(gate)
    return ""


def scopes_overlap(a, b):
    ra, rb = a.rstrip("*").rstrip("/"), b.rstrip("*").rstrip("/")
    if not ra or not rb:
        return True
    return (ra == rb or ra.startswith(rb + "/") or rb.startswith(ra + "/")
            or gates.path_match(ra, [b]) or gates.path_match(rb, [a]))


def scope_conflicts(cfg: WtConfig, reg: Registry, repos, scope):
    """新任务声明的范围 vs 进行中任务声明的范围与实际改动（验收 B3：改同批文件要在开任务时就拦下）。"""
    out = []
    for t in reg.all(include_hub=False):
        if t.get("state") not in LIVE_STATES or not is_local(cfg, t) or t.get("creating"):
            continue
        shared = [a for a in repos if a in (t.get("repos") or {})]
        if not shared:
            continue
        theirs = t.get("scope") or []
        if any(scopes_overlap(mine, other) for mine in scope for other in theirs):
            out.append(f"{t['id']} {t['title']}：声明范围 {'、'.join(theirs)}")
            continue
        for alias in shared:
            touched = sorted(f for f in task_changes(cfg, t, alias) if gates.path_match(f, scope))
            if touched:
                out.append(f"{t['id']} {t['title']}：已改 {alias}:{'、'.join(touched[:5])}")
                break
    return out


def create_task(cfg: WtConfig, reg: Registry, *, slug, title, repos, goal="", accept="", base=None, scope=None,
                from_refs=None, source_branches=None, new_anyway="", tool=None, sessions=None, draft=False,
                extra=None):
    model.validate_slug(cfg, slug)
    title = model.validate_title(title)
    goal, accept = (goal or "").strip(), (accept or "").strip()
    if not draft and (not goal or not accept):
        raise WtError("开任务必须写清 --goal（目标）与 --accept（验收标准）")
    if tool and tool != "human" and not sessions:
        raise WtError("未识别到会话号，请设置 AISK_SESSION 或传 --session 后再开任务。")
    repos = [a.strip() for a in repos if a and a.strip()]
    if not repos or len(repos) != len(set(repos)):
        raise WtError("仓库列表不能为空或重复")
    if cfg.profile_name == "xinhua" and len(repos) > 1 and set(repos) & {"be", "web"}:
        raise WtError("新华前端 web 与后端 be 必须拆成独立任务，分别提交、门禁、落地和发布")
    for alias in repos:
        rc = cfg.repo(alias)
        if rc.workspace_mode == "direct":
            raise WtError(f"{alias} 配置为 direct，不创建任务 worktree；请使用 aisk task direct-new")
    from_refs, source_branches = dict(from_refs or {}), dict(source_branches or {})
    if scope and not new_anyway:
        conflicts = scope_conflicts(cfg, reg, repos, scope)
        if conflicts:
            raise WtError("声明的改动范围与进行中的任务重叠，落地时会冲突：\n  " + "\n  ".join(conflicts[:8])
                          + "\n先协调、用 --base 叠放，或确需并行时加 --new-anyway \"理由\"")

    with reg.lock():
        tasks = reg.all()
        for alias, br in source_branches.items():
            for t in tasks:
                if t.get("state") in LIVE_STATES + ("landed", "promoted") and \
                        br in (t.get("source_branches") or {}).values():
                    raise WtError(f"分支 {br} 已由任务 {t['id']} {t['title']} 接手，请 aisk task claim {t['id']}")
        dups = model.duplicate_candidates([t for t in tasks if t.get("state") in LIVE_STATES], title, slug)
        if dups and not new_anyway:
            lines = [f"{t['id']} {t['title']}（{t['state']}，相似度 {s}）" for s, _c, t in dups[:5]]
            raise WtError("疑似重复任务，请先 aisk task claim 续做：\n  " + "\n  ".join(lines)
                          + "\n确需另开：加 --new-anyway \"理由\"")
        check_quota(cfg, reg, repos=repos, materialize=repos)
        parent = None
        if base:
            parent = reg.find_by_ref(base)
            require_local(cfg, parent, LIVE_STATES)
            missing = [a for a in repos if a not in parent["repos"]]
            if missing:
                raise WtError(f"父任务 {parent['id']} 不含仓库 {missing}，无法叠放")
        tid = reg.next_id()
        name = f"{tid}-{slug}"
        td = cfg.tasks_dir / name
        if td.exists():
            raise WtError(f"任务目录已存在：{td}")
        block = free_port_block(reg)
        task = {
            "id": tid, "slug": slug, "name": name, "title": title, "goal": goal, "accept": accept,
            "os": cfg.os, "dir": str(td), "port_block": block, "ports": cfg.ports_for(block),
            "base_task": parent["id"] if parent else None, "scope": list(scope or []), "repos": {},
            "source_branches": source_branches, "state": "active", "owner": None, "created_at": now_iso(),
            "history": [], "new_anyway": new_anyway or None, "creating": now_iso(),
            "creating_repos": [a for a in repos if cfg.repo(a).workspace_mode == "task-worktree"],
        }
        task.update(extra or {})
        reg.save(task)

    created = []
    try:
        for alias in repos:
            repo = cfg.repo(alias).path
            if not repo.exists():
                raise WtError(f"本机没有仓库 {alias}：{repo}")
            branch = cfg.task_branch(name)
            if git.branch_exists(repo, branch):
                raise WtError(f"{alias} 已存在分支 {branch}")
            explicit = from_refs.get(alias) or source_branches.get(alias)
            upstream = parent["repos"][alias]["branch"] if parent else base_ref(cfg)
            start = explicit or upstream
            start_sha = git.sha(repo, start)
            if not start_sha:
                raise WtError(f"{alias} 找不到基线 {start}")
            # 显式起点（接手分支、恢复归档）上已有的提交也属于本任务，基线取它与上游的分叉点
            base_sha = start_sha if not explicit else (git.merge_base(repo, upstream, start_sha) or start_sha)
            path = td / alias
            git.worktree_add_unique(repo, path, f"{names.TASK_ADMIN_PREFIX}{tid}-{alias}", start_sha, branch=branch,
                                    reason=f"{names.CLI} {tid} os={cfg.os}")
            created.append((repo, path, branch))
            task["repos"][alias] = {"branch": branch, "path": str(path), "admin": f"{names.TASK_ADMIN_PREFIX}{tid}-{alias}",
                                    "base_ref": upstream, "base_sha": base_sha, "start_ref": start,
                                    "ready_sha": None, "landed_sha": None}
        task["base_short"] = next(iter(task["repos"].values()))["base_sha"][:9]
        bind.write_task_files(cfg, task, java_home(cfg, repos))
        task.pop("creating", None)
        task.pop("creating_repos", None)
        if tool:
            task["owner"] = new_owner(cfg, tool, sessions)
            append_progress(task, progress_line(tool, "开始任务并认领"))
        with reg.lock():
            reg.save(task)
            reg.event("new", task=tid, title=title, repos=repos, base=task["base_task"],
                      source_branches=source_branches or None, new_anyway=new_anyway or None)
    except BaseException:
        for repo, path, branch in reversed(created):
            git.run(["worktree", "unlock", str(path)], cwd=repo, check=False)
            git.run(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
            git.run(["branch", "-D", branch], cwd=repo, check=False)
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)
        with reg.lock():
            reg.task_file(tid).unlink(missing_ok=True)
        raise
    return task


def cmd_new(cfg, reg, args):
    repos = (args.repos or cfg.repo_order[0]).split(",")
    tool, sessions = caller(args)
    task = create_task(cfg, reg, slug=args.slug, title=args.title, repos=repos, goal=args.goal, accept=args.accept,
                       base=args.base, scope=args.scope, from_refs=parse_refs(args.from_ref, repos, "--from-ref"),
                       new_anyway=args.new_anyway, tool=tool, sessions=sessions, draft=args.draft)
    say("ok", f"任务 {task['id']} {task['title']} 已创建：{task['dir']}")
    for alias, r in task["repos"].items():
        say("info", f"{alias}: {r['branch']}  基线 {r['base_ref']}@{r['base_sha'][:9]}")
    say("info", f"已由 {tool} 认领" if tool else f"下一步：在任务目录打开工具后 aisk task claim {task['id']} --tool <工具>")
    refresh_board(cfg, reg)
    if args.print_path:
        print(task["repos"][repos[0].strip()]["path"])
    return 0


# ------------------------------------------------------------------ find / claim / release / heartbeat / note
def cmd_find(cfg, reg, args):
    now_ts = time.time()
    query = " ".join(args.query)
    hits = [(model.search_score(t, query), t) for t in reg.all(include_archived=args.all)]
    hits = [h for h in hits if h[0] >= 0.35]
    if not hits:
        say("info", "没有匹配的任务。确认没有同一件事后再 aisk task new")
        return 1
    for score, t in sorted(hits, key=lambda h: (-h[0], h[1]["id"])):
        lease, act = lease_of(cfg, t, now_ts, quick=True)
        print(f"{t['id']}  {t['title']}  [{t['state']}·{model.LEASE_LABEL[lease]}·{owner_label(t.get('owner'))}"
              f"·{model.age_text(act, now_ts)}]  下一步：{last_next(t) or '-'}")
    return 0


def cmd_claim(cfg, reg, args):
    tool, sessions = caller(args)
    if not tool:
        raise WtError("请用 --tool 说明你是谁"
                      "（claude/codex/antigravity/workbuddy/workbuddy-ai/cursor/human）")
    if not sessions and tool != "human":
        raise WtError("未识别到会话号。请加 --session <本会话唯一标识>，后续命令沿用；不能只凭工具名认领。")
    with reg.lock():
        task = reg.find_by_ref(args.task)
        require_local(cfg, task, LIVE_STATES)
        lease, act = lease_of(cfg, task)
        prev = task.get("owner")
        allowed, why = model.claim_decision(prev, lease, tool, sessions, args.takeover)
        if not allowed:
            raise WtError(f"{task['id']} {why}：执行者 {owner_label(prev)}（{model.age_text(act, time.time())}活动）。"
                          f"只读查看，不要修改；空闲（{cfg.idle_minutes} 分钟）后可 --takeover --reason 接手，"
                          f"超过 {cfg.claimable_minutes} 分钟可直接认领")
        if why == "接手" and lease == model.IDLE and not (args.reason or "").strip():
            raise WtError("接手空闲任务必须写 --reason（为什么判断原执行者不会回来）")
        if task["state"] == "parked":
            # A parked task keeps its materialized checkout. Claiming it
            # consumes an active slot, not a second physical-worktree slot.
            check_quota(cfg, reg, exclude=task["id"], repos=list(task["repos"]), materialize=[])
        task["owner"] = new_owner(cfg, tool, sessions)
        if why == "接手":
            note = f"接手（原执行者 {owner_label(prev)}，{model.LEASE_LABEL[lease]}）"
            append_progress(task, progress_line(tool, note + (f" · 原因：{args.reason}" if args.reason else "")))
        elif why == "认领":
            append_progress(task, progress_line(tool, "认领"))
        if task["state"] == "parked":
            reg.set_state(task, "active", note=f"{tool} 认领")
        else:
            reg.save(task)
        reg.event("claim", task=task["id"], tool=tool, prev=owner_label(prev), lease=lease,
                  reason=args.reason or None)
    say("ok", f"{task['id']} {task['title']} 已由 {tool} {why}。上次下一步：{last_next(task) or '（无记录）'}")
    refresh_board(cfg, reg)
    return 0


def release_task(cfg, reg, task, who, note):
    prev = task.get("owner")
    task["owner"] = None
    append_progress(task, progress_line(who, "交还" + (f" · {note}" if note else "")))
    reg.save(task)
    reg.event("release", task=task["id"], prev=owner_label(prev))


def cmd_release(cfg, reg, args):
    tool, sessions = caller(args)
    with reg.lock():
        task = reg.find_by_ref(args.task)
        if not is_local(cfg, task):
            raise WtError("只能在任务所属系统上交还")
        if not task.get("owner"):
            say("info", f"{task['id']} 当前没有执行者")
            return 0
        refuse_if_foreign(cfg, task, tool, sessions, "交还")
        release_task(cfg, reg, task, tool or "操作者", args.note)
    say("ok", f"{task['id']} 已交还，任何 AI 可认领")
    refresh_board(cfg, reg)
    return 0


def cmd_heartbeat(cfg, reg, args):
    tool, sessions = caller(args)
    with reg.lock():
        task = reg.find_by_ref(args.task)
        ok = heartbeat(reg, task, tool, sessions)
    return 0 if ok else 1


def cmd_add_repo(cfg, reg, args):
    """给进行中的任务补挂一个仓库。

    **为什么必须有**：开工时未必知道改动会落到几个仓库（2026-09-16 现场：一个任务只挂了后端，
    做到一半发现要同时删前端的兼容开关）。没有这条命令，只能另开任务或越界改主工作区——
    前者丢上下文，后者正是守卫要拦的事。
    """
    alias = args.alias
    tool, sessions = caller(args)
    task = reg.find_by_ref(args.task)
    require_local(cfg, task, LIVE_STATES)
    refuse_if_foreign(cfg, task, tool, sessions, "补挂仓库")
    if task.get("creating") or task.get("archiving"):
        raise WtError(f"{task['id']} 正在创建或归档中，先处理完再补挂仓库")
    if alias in task["repos"]:
        raise WtError(f"{task['id']} 已经挂了 {alias}：{task['repos'][alias]['path']}")
    rc = cfg.repo(alias)
    if rc.workspace_mode == "direct":
        raise WtError(f"{alias} 配置为 direct，不能补挂到 worktree 任务；请创建独立 direct 任务")
    repo = rc.path
    if not repo.exists():
        raise WtError(f"本机没有仓库 {alias}：{repo}")
    branch = cfg.task_branch(task["name"])
    if git.branch_exists(repo, branch):
        raise WtError(f"{alias} 已存在分支 {branch}：先确认它是不是同一件事的旧分支")
    upstream = base_ref(cfg)
    start_sha = git.sha(repo, upstream)
    if not start_sha:
        raise WtError(f"{alias} 找不到基线 {upstream}")
    path = Path(task["dir"]) / alias
    admin = f"{names.TASK_ADMIN_PREFIX}{task['id']}-{alias}"
    git.worktree_add_unique(repo, path, admin, start_sha, branch=branch,
                            reason=f"{names.CLI} {task['id']} os={cfg.os}")
    try:
        with reg.lock():
            fresh = reg.load(task["id"])
            fresh["repos"][alias] = {"branch": branch, "path": str(path), "admin": admin, "base_ref": upstream,
                                     "base_sha": start_sha, "start_ref": upstream, "ready_sha": None,
                                     "landed_sha": None}
            append_progress(fresh, progress_line(tool, f"补挂仓库 {alias}（基线 {upstream}@{start_sha[:9]}）"))
            bind.write_task_files(cfg, fresh, java_home(cfg, list(fresh["repos"])), keep_notes=True)
            reg.save(fresh)
            reg.event("add-repo", task=fresh["id"], tool=tool, alias=alias, base=start_sha)
            task = fresh
    except BaseException:
        git.run(["worktree", "unlock", str(path)], cwd=repo, check=False)
        git.run(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
        git.run(["branch", "-D", branch], cwd=repo, check=False)
        raise
    say("ok", f"{task['id']} 已补挂 {alias}：{path}（分支 {branch}，基线 {upstream}@{start_sha[:9]}）")
    say("info", f"任务须知与四端钩子已重写；{names.CLI} check {task['id']} 会一并跑 {alias} 的门禁")
    return 0


def cmd_note(cfg, reg, args):
    parts = []
    if args.text:
        parts.append(" ".join(args.text))
    if args.done:
        parts.append(f"做了：{args.done}")
    if args.next:
        parts.append(f"下一步：{args.next}")
    if args.blocked:
        parts.append(f"阻塞：{args.blocked}")
    if not parts:
        raise WtError("note 至少写一项：文本、--done、--next 或 --blocked")
    tool, sessions = caller(args)
    with reg.lock():
        task = reg.find_by_ref(args.task)
        if not is_local(cfg, task):
            raise WtError("只能在任务所属系统上记录进度")
        refuse_if_foreign(cfg, task, tool, sessions, "记录进度")
        if args.next:
            task["next_step"] = args.next
        append_progress(task, progress_line(tool or "操作者", " · ".join(parts)))
        if not heartbeat(reg, task, tool, sessions):
            reg.save(task)
    say("ok", f"{task['id']} 进度已记录")
    refresh_board(cfg, reg)
    return 0


# ------------------------------------------------------------------ 在制品快照 / pause
def snapshot_worktree(cfg: WtConfig, alias, wt, base_sha, message):
    """用临时索引把目录内容（含未跟踪文件，遵守 .gitignore）快照成提交，不碰该目录的索引与文件。
    内容与基线相同时返回 None。"""
    repo = cfg.repo(alias).path
    tmp = Path(tempfile.mkdtemp(prefix="aisk-snap-"))
    try:
        idx, excl = tmp / "index", tmp / "exclude"
        excl.write_text("\n".join(SNAPSHOT_EXCLUDES) + "\n", encoding="utf-8")
        env = {"GIT_INDEX_FILE": str(idx)}
        git.run(["read-tree", base_sha], cwd=repo, env_extra=env)
        git.run(["-c", f"core.excludesFile={excl}", f"--work-tree={wt}", "add", "-A", "."], cwd=repo, env_extra=env)
        tree = git.out(["write-tree"], cwd=repo, env_extra=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if tree == git.out(["rev-parse", f"{base_sha}^{{tree}}"], cwd=repo):
        return None
    return git.commit_tree(repo, tree, [base_sha], message)


def cmd_pause(cfg, reg, args):
    tool, sessions = caller(args)
    task = recover_stale_queued(cfg, reg, reg.find_by_ref(args.task))
    require_local(cfg, task, ("active", "rejected", "parked", "ready"))
    refuse_if_foreign(cfg, task, tool, sessions, "暂停")
    kept = []
    for alias, r in task["repos"].items():  # 快照可能要几秒（大仓库），不占登记簿锁
        path = Path(r["path"])
        if not git.dirty(path):
            continue
        snap = snapshot_worktree(cfg, alias, path, git.sha(path, "HEAD"), f"wip: 暂停快照({task['id']})")
        if snap:
            git.run(["update-ref", "-m", f"{names.CLI} pause", f"{names.REF_NS}/wip/{task['name']}/{alias}", snap],
                    cwd=cfg.repo(alias).path)
            kept.append(f"{alias} 未提交改动已快照到 {names.REF_NS}/wip/{task['name']}/{alias}（工作区原样保留）")
    with reg.lock():
        task = reg.load(task["id"])
        if task["state"] not in ("active", "rejected", "parked", "ready"):
            raise WtError(f"任务 {task['id']} 状态已变为 {task['state']}，未暂停")
        refuse_if_foreign(cfg, task, tool, sessions, "暂停")
        prev = task.get("owner")
        text = "暂停" + (f" · 下一步：{args.next}" if args.next else "") + (f" · {'；'.join(kept)}" if kept else "")
        append_progress(task, progress_line(tool or owner_label(prev), text))
        if args.next:
            task["next_step"] = args.next
        task["owner"] = None
        if task["state"] in ("active", "rejected"):
            reg.set_state(task, "parked", note=args.next or "")
        else:
            reg.save(task)
        reg.event("pause", task=task["id"], prev=owner_label(prev))
    moved = harvest_workbuddy(cfg, task)
    say("ok", f"{task['id']} 已暂停并交还，任何 AI 可认领续做" + (f"（{'；'.join(kept)}）" if kept else ""))
    if moved:
        say("info", f"WorkBuddy 记忆已收割：{', '.join(moved)}")
    refresh_board(cfg, reg)
    return 0


def cmd_resume(cfg, reg, args):
    args.takeover, args.reason = False, None
    return cmd_claim(cfg, reg, args)


# ------------------------------------------------------------------ 看板
def anomalies(cfg: WtConfig, reg: Registry, quick=False):
    out = []
    known = {Path(t["dir"]).resolve() for t in reg.all(include_archived=True, include_hub=False)}
    if cfg.tasks_dir.exists():
        for d in sorted(cfg.tasks_dir.iterdir()):
            if d.is_dir() and d.resolve() not in known:
                out.append(f"未登记目录 {d}（aisk task adopt 收编或由创建者清理）")
    for t in reg.all(include_hub=False):
        started = parse_iso(t.get("creating"))
        if started and time.time() - started.timestamp() > CREATING_STALE_SECONDS:
            out.append(f"{t['id']} 创建中断（{t['creating']}），aisk task doctor 查看后清理")
    if cfg.os == "mac":
        for alias in cfg.repo_order:
            repo = cfg.repo(alias).path
            if not repo.exists():
                continue
            cur = git.current_branch(repo)
            if cur != cfg.integration:
                out.append(f"{alias} 主工作区在 {cur}，不在 {cfg.integration}（上面的在制品可 aisk task adopt-branch）")
            elif not quick and git.dirty(repo):
                out.append(f"{alias} 主工作区有未提交改动（会挡住落地）")
    if (cfg.data_root / ".git").exists():
        out.append("数据根是 git 仓库（误执行 git clean 会删登记簿与 hub），应移除 .git")
    return out


def board_text(cfg: WtConfig, reg: Registry, quick=False):
    now_ts = time.time()
    groups = {k: [] for k, _ in model.GROUPS}
    for t in reg.all():
        lease, act = lease_of(cfg, t, now_ts, quick=quick)
        o = t.get("owner")
        who = "创建中" if t.get("creating") else (f"{o['tool']}·{model.LEASE_LABEL[lease]}" if o else "无人")
        line = (f"- **{t['id']}** {t['title']} `[{','.join(t['repos'])}]` · {who} · {model.age_text(act, now_ts)}"
                f" · 下一步：{last_next(t) or '-'}")
        if t.get("legacy_id"):
            line += f" · 旧编号 {t['legacy_id']}"
        if t.get("os") != cfg.os:
            line += f" · {t.get('os')}"
        groups[model.board_group(t, lease)].append(line)
    lines = [f"# 任务看板（{cfg.profile_name}）", "",
             f"> 生成于 {now_iso()[:16].replace('T', ' ')}；无动静 {cfg.idle_minutes} 分钟显示空闲、"
             f"{cfg.claimable_minutes} 分钟可接手（档案 worktrees.lease 可调）。", ""]
    quota = worktree_quota_snapshot(cfg, reg)
    active_limit = cfg.quota_active_worktrees or "未配置"
    physical_limit = cfg.quota_materialized or "未配置"
    per_repo = "、".join(
        f"{alias} {quota['active_by_repo'].get(alias, 0)}/{cfg.repo(alias).limits.get('active_worktrees', '未配置')}活跃"
        f"·{quota['physical_by_repo'].get(alias, 0)}/{cfg.repo(alias).limits.get('materialized_worktrees', '未配置')}物理"
        for alias in cfg.repo_order
        if cfg.repo(alias).workspace_mode == "task-worktree" and cfg.repo(alias).limits
    )
    lines.append(f"## worktree 槽位\n- 活跃 {quota['active_total']}/{active_limit}；物理 {quota['physical_total']}/{physical_limit}"
                 + (f"；{per_repo}" if per_repo else "") + "\n")
    for key, label in model.GROUPS:
        lines.append(f"## {label}（{len(groups[key])}）")
        lines += groups[key] or ["- 无"]
        lines.append("")
    problems = anomalies(cfg, reg, quick=quick)
    lines.append(f"## 异常（{len(problems)}）")
    lines += [f"- {p}" for p in problems] or ["- 无"]
    return "\n".join(lines) + "\n"


def refresh_board(cfg: WtConfig, reg: Registry, quick=True):
    try:
        text = board_text(cfg, reg, quick=quick)
        atomic_text(cfg.board_file, text)
        return text
    except (OSError, WtError):
        return ""


def cmd_status(cfg, reg, args):
    if getattr(args, "brief", False):
        for task in reg.all(include_archived=args.all):
            print(f"{task['id']} | {task['title']} | {task.get('os')} | {task['state']} | "
                  f"{owner_label(task.get('owner'))} | {task.get('next_step') or '-'}")
        return 0
    if args.json:
        now_ts = time.time()
        rows = []
        for t in reg.all(include_archived=args.all):
            lease, _ = lease_of(cfg, t, now_ts)
            rows.append({"id": t["id"], "title": t["title"], "state": t["state"], "lease": lease,
                         "owner": t.get("owner"), "next": last_next(t), "repos": list(t["repos"]),
                         "legacy_id": t.get("legacy_id")})
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(refresh_board(cfg, reg, quick=False), end="")
    return 0


def cmd_context(cfg, reg, args):
    """接手时的一次性摘要：只读本任务记录与末尾进度，不扫描全部仓库。"""
    task = reg.find_by_ref(args.task)
    print(f"{task['id']} {task['title']} | {task.get('os')} | {task['state']} | {owner_label(task.get('owner'))}")
    print(f"目标：{task.get('goal') or '见 TASK.md'}")
    print(f"验收：{task.get('accept') or '见 TASK.md'}")
    print(f"下一步：{last_next(task) or '-'}")
    if task.get('_summary'):
        print(f"另一端的只读摘要，更新时间：{task.get('updated_at')}；请在任务所属系统续做。")
        return 0
    print(f"任务目录：{task['dir']}")
    for alias, repo in task['repos'].items():
        print(f"{alias}：{repo['branch']}")
    if is_local(cfg, task):
        progress = Path(task['dir']) / 'PROGRESS.md'
        if progress.is_file():
            lines = [line for line in progress.read_text(encoding='utf-8').splitlines() if line.startswith('- ')]
            for line in lines[-5:]:
                print(line[:400])
    return 0


# ------------------------------------------------------------------ open / env / overlap
def cmd_open(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    require_local(cfg, task, LIVE_STATES)
    td, tid, tool = task["dir"], task["id"], args.tool
    lines = []
    if tool == "codex":
        roots = sorted({str(git.common_dir(r["path"])) for r in task["repos"].values()})
        lines.append(shlex.join(["codex", "-p", names.CODEX_PROFILE, "-C", td, "-c",
                                 "sandbox_workspace_write.writable_roots=" + json.dumps(roots)]))
        lines.append("首次在该目录启动时确认信任 .codex/hooks.json 里的钩子，守卫与会话事件才会生效")
    elif tool == "claude":
        lines.append(f"cd {shlex.quote(td)} && claude")
        lines.append("会话开始时钩子自动认领；桌面端打开该目录即可，不要勾选 worktree 模式")
    elif tool in ("workbuddy", "workbuddy-ai"):
        app = "WorkBuddy AI" if tool == "workbuddy-ai" else "WorkBuddy"
        lines.append(f"在 {app} 里打开目录 {td}")
        lines.append("钩子在 .codebuddy/settings.json（两个入口的项目级设置都读这一份；"
                     ".workbuddy-ai/settings.json 当前引擎不读，是死文件）；"
                     f"执行任务命令时带 --tool {tool} --session <本会话标识>")
    elif tool == "antigravity":
        lines.append(f"在 Antigravity 里打开目录 {td}")
        lines.append("钩子在 .agents/hooks.json；每轮开始提示认领状态，写入前由守卫校验持有者")
    else:
        lines.append(f"cd {shlex.quote(td)}")
    lines.append(f"步骤：{names.CLI} claim {tid} --tool <工具> --session <本会话标识>（钩子已认领可跳过）→ "
                 f"{names.CLI} note {tid} --done … --next … → 离开前 {names.CLI} pause {tid} --next …")
    if cfg.os == "windows":
        if cfg.windows_integration:
            lines.append(f"本系统已显式开启集成：{names.CLI} check {tid} → 填 HANDOFF.md → {names.CLI} ready {tid} → "
                         f"{names.CLI} land {tid}；仅写入档案声明的 integration_repo，系统确认框不可用即拒绝")
        else:
            lines.append(f"本系统不能 land/promote：{names.CLI} check {tid} → 填 HANDOFF.md → {names.CLI} ready {tid}，由 mac 集成端落地")
    for line in lines:
        say("info", line)
    return 0


def cmd_env(cfg, reg, args):
    print(str(Path(reg.find_by_ref(args.task)["dir"]) / (names.ENV_CMD if cfg.os == "windows" else names.ENV_FILE)))
    return 0


def task_changes(cfg, task, alias):
    path = Path(task["repos"][alias]["path"])
    if not path.exists():
        return set()
    try:
        base = git.merge_base(path, base_ref(cfg), "HEAD") or task["repos"][alias]["base_sha"]
        files = set(git.diff_names(path, base, "HEAD"))
        files.update(git.dirty_paths(path))
        return files
    except (OSError, WtError):
        # 失效/被外部删除的旧 worktree 由 doctor 负责修复；它不能阻塞
        # 与之无关的新任务，也不能被错误地当成有改动而制造假冲突。
        return set()


def overlaps(cfg, reg):
    by_file = {}
    for t in reg.all(include_hub=False):
        if t["state"] not in LIVE_STATES or not is_local(cfg, t) or t.get("creating"):
            continue
        for alias in t["repos"]:
            for f in task_changes(cfg, t, alias):
                by_file.setdefault((alias, f), []).append(t["id"])
    return {k: v for k, v in by_file.items() if len(v) > 1}


def cmd_overlap(cfg, reg, args):
    ov = overlaps(cfg, reg)
    if not ov:
        say("ok", "进行中的任务之间没有重叠改动")
        return 0
    for (alias, f), ids in sorted(ov.items()):
        say("warn", f"{alias}:{f}  ← {', '.join(ids)}")
    return 1


# ------------------------------------------------------------------ check / commit / ready / restack
def _commit_paths(repo, paths, all_paths):
    """把提交范围限制在任务仓库内；不让 `aisk task commit` 退化成跨目录 git add。"""
    if all_paths and paths:
        raise WtError("--all 与显式路径不能同时使用")
    if not all_paths and not paths:
        raise WtError("请指定提交路径，或使用 --all 提交当前任务仓库的全部改动")
    if all_paths:
        return ["."]
    root = Path(repo).resolve()
    result = []
    for raw in paths:
        value = str(raw)
        candidate = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if candidate != root and root not in candidate.parents:
            raise WtError(f"提交路径越界：{raw}")
        # 已删除的 tracked path 也允许传入；Git 负责判断是否存在。
        result.append(candidate.relative_to(root).as_posix() if candidate != root else ".")
    return result


def _commit_path_in_scope(path, paths):
    return any(scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/") for scope in paths)


def _commit_intent_matches(intent, *, branch, message, paths, all_paths):
    return (isinstance(intent, dict) and intent.get("version") == 1
            and intent.get("branch") == branch and intent.get("message") == message
            and intent.get("paths") == list(paths) and bool(intent.get("all_paths")) == bool(all_paths))


def _recover_intended_commit(repo, intent):
    """Recognize a commit completed after durable intent but before task save."""
    head = git.sha(repo, "HEAD")
    base = intent.get("base_sha")
    tree = intent.get("tree_sha")
    if not head or not base or not tree or head == base:
        return None
    parents = git.out(["show", "-s", "--format=%P", head], cwd=repo).split()
    actual_tree = git.out(["rev-parse", "--verify", f"{head}^{{tree}}"], cwd=repo).strip()
    message = git.out(["show", "-s", "--format=%B", head], cwd=repo).strip()
    changed = git.diff_names(repo, base, head)
    paths = intent.get("paths") or []
    if (parents != [base] or actual_tree != tree or message != intent.get("message") or not changed
            or any(not _commit_path_in_scope(path, paths) for path in changed)):
        raise WtError("检测到 commit intent 后分支头已变化，但提交与持久意图不完全匹配；保留意图并停止，请人工核对")
    return head


def cmd_commit(cfg, reg, args):
    """在任务分支本地提交，完全不读取或修改 fxh 主工作区，也不推送远端。"""
    tool, sessions = caller(args)
    message = (args.message or "").strip()
    if not message:
        raise WtError("提交说明不能为空")
    with reg.lock():
        task = recover_stale_queued(cfg, reg, reg.find_by_ref(args.task))
        require_local(cfg, task, ("active", "rejected", "parked", "ready"))
        refuse_if_foreign(cfg, task, tool, sessions, "提交")
        aliases = select_repos(cfg, task, getattr(args, "repos", None))
        committed = []
        for alias in aliases:
            row = task["repos"][alias]
            path = Path(row["path"])
            branch = row["branch"]
            if git.current_branch(path) != branch:
                raise WtError(f"{alias}: 当前分支不是任务分支 {branch}")
            err = model.check_message(cfg, message)
            if err:
                raise WtError(err)
            all_paths = bool(getattr(args, "all", False))
            add_paths = _commit_paths(path, args.paths, all_paths)
            intent = row.get("commit_intent")
            if intent:
                if not _commit_intent_matches(intent, branch=branch, message=message,
                                              paths=add_paths, all_paths=all_paths):
                    raise WtError(f"{alias}: 存在未完成的 commit intent；必须用原提交说明和原路径重试")
                recovered = _recover_intended_commit(path, intent)
                if intent.get("status") == "committed":
                    if not recovered or recovered != intent.get("post_sha"):
                        raise WtError(f"{alias}: 已记录完成的 commit intent 与当前 HEAD 不匹配；停止自动恢复")
                    sha = recovered
                elif recovered:
                    sha = recovered
                    intent["status"] = "committed"
                    intent["post_sha"] = sha
                    reg.save(task)
                else:
                    if git.sha(path, "HEAD") != intent.get("base_sha"):
                        raise WtError(f"{alias}: commit intent 基线已变化；停止自动恢复")
                    if intent.get("tree_sha"):
                        tree = git.out(["write-tree"], cwd=path).strip()
                        unstaged = set(git.out(["diff", "--name-only", "--"], cwd=path).splitlines())
                        untracked = set(git.out(["ls-files", "--others", "--exclude-standard"], cwd=path).splitlines())
                        if (tree != intent["tree_sha"]
                                or any(_commit_path_in_scope(p, add_paths) for p in unstaged | untracked)):
                            raise WtError(f"{alias}: commit intent 建立后 index 或范围内工作区发生变化；保留现场并停止")
                    else:
                        git.run(["add", "-A", "--", *add_paths], cwd=path)
                        staged = {p for p in git.out(["diff", "--cached", "--name-only", "-z"], cwd=path).split("\0") if p}
                        outside = sorted(p for p in staged if not _commit_path_in_scope(p, add_paths))
                        if outside:
                            raise WtError(f"{alias}: 暂存区包含未请求路径：{'、'.join(outside[:12])}；未提交")
                        if not staged:
                            raise WtError(f"{alias}: 没有可提交的暂存改动（路径可能为空）")
                        intent["tree_sha"] = git.out(["write-tree"], cwd=path).strip()
                        reg.save(task)
                    git.run(["commit", "-m", message], cwd=path)
                    sha = git.sha(path, "HEAD")
                    parents = git.out(["show", "-s", "--format=%P", sha], cwd=path).split()
                    tree = git.out(["rev-parse", "--verify", f"{sha}^{{tree}}"], cwd=path).strip()
                    if parents != [intent["base_sha"]] or tree != intent.get("tree_sha"):
                        raise WtError(f"{alias}: 提交结果未匹配持久 intent；保留意图并停止")
                    intent["status"] = "committed"
                    intent["post_sha"] = sha
                    reg.save(task)
            else:
                base_sha = git.sha(path, "HEAD")
                intent = {"version": 1, "branch": branch, "base_sha": base_sha, "message": message,
                          "paths": list(add_paths), "all_paths": all_paths, "tree_sha": None,
                          "post_sha": None, "status": "pending", "created_at": now_iso()}
                row["commit_intent"] = intent
                reg.save(task)
                # Process the new intent in the same path as a retry. Keeping
                # the intent in registry before `git add` makes every later
                # interruption discoverable and replayable.
                recovered = None
                git.run(["add", "-A", "--", *add_paths], cwd=path)
                staged = {p for p in git.out(["diff", "--cached", "--name-only", "-z"], cwd=path).split("\0") if p}
                outside = sorted(p for p in staged if not _commit_path_in_scope(p, add_paths))
                if outside:
                    raise WtError(f"{alias}: 暂存区包含未请求路径：{'、'.join(outside[:12])}；未提交")
                if not staged:
                    raise WtError(f"{alias}: 没有可提交的暂存改动（路径可能为空）")
                intent["tree_sha"] = git.out(["write-tree"], cwd=path).strip()
                reg.save(task)
                git.run(["commit", "-m", message], cwd=path)
                sha = git.sha(path, "HEAD")
                parents = git.out(["show", "-s", "--format=%P", sha], cwd=path).split()
                tree = git.out(["rev-parse", "--verify", f"{sha}^{{tree}}"], cwd=path).strip()
                if parents != [intent["base_sha"]] or tree != intent.get("tree_sha"):
                    raise WtError(f"{alias}: 提交结果未匹配持久 intent；保留意图并停止")
                intent["status"] = "committed"
                intent["post_sha"] = sha
                reg.save(task)
            task["repos"][alias]["ready_sha"] = None
            committed.append(f"{alias}@{sha[:9] if sha else 'unknown'}")
        # Keep per-repo committed intents durable until every selected repo
        # has completed; a later repo failure must not make an earlier commit
        # look like a fresh no-op when the multi-repo command is retried.
        for alias in aliases:
            intent = task["repos"][alias].get("commit_intent")
            if intent and intent.get("status") == "committed":
                if getattr(args, "autoflow", False):
                    # Persist the orchestration checkpoint in the same registry
                    # write that clears commit_intent. A crash before this save
                    # remains recoverable through the committed intent above;
                    # after it, finish can safely resume gates without making
                    # a duplicate/empty commit.
                    task["repos"][alias]["autoflow_checkpoint"] = {
                        "version": 1,
                        "branch": intent.get("branch"),
                        "sha": intent.get("post_sha"),
                        "paths": list(intent.get("paths") or []),
                        "message": intent.get("message"),
                    }
                task["repos"][alias].pop("commit_intent", None)
        if task.get("state") in ("ready", "parked", "rejected"):
            task["owner"] = task.get("owner") or new_owner(cfg, tool, sessions)
            reg.set_state(task, "active", note="任务分支本地提交；ready 登记已失效")
        else:
            append_progress(task, progress_line(tool or "操作者", "任务分支本地提交：" + "、".join(committed)))
            reg.save(task)
        reg.event("commit", task=task["id"], tool=tool, repos=committed, remote_push=False)
    say("ok", f"{task['id']} 已提交到任务分支：{'、'.join(committed)}；未检查或修改 fxh，未推送远端")
    refresh_board(cfg, reg)
    return 0


def cmd_check(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    require_local(cfg, task, LIVE_STATES)
    results, bad = {}, False
    logdir = cfg.logs_dir / task["id"]
    for alias, r in task["repos"].items():
        path = Path(r["path"])
        changed = sorted(task_changes(cfg, task, alias))
        head = git.sha(path, "HEAD")
        dirty_before = bool(git.dirty(path))
        log = logdir / f"check-{alias}-{stamp()}.log"
        try:
            ok, summary = gates.run_gate(cfg, alias, path, changed, log)
        except (WtError, OSError) as e:
            ok, summary = False, str(e)
        if git.sha(path, "HEAD") != head or git.dirty(path) or dirty_before:
            ok, summary = False, "自检要求工作区干净、构建前后提交不变：请先提交再 check"
        results[alias] = {"sha": head, "ok": ok, "summary": summary, "log": str(log)}
        say("ok" if ok else "err", f"{alias}: {summary}")
        if not ok:
            bad = True
            print(gates.tail(log))
    tool, sessions = caller(args)
    with reg.lock():
        task = reg.load(task["id"])
        task["check"] = {"at": now_iso(), "results": results}
        if not heartbeat(reg, task, tool, sessions):
            reg.save(task)
    reg.event("check", task=task["id"], ok=not bad)
    return 1 if bad else 0


def select_repos(cfg: WtConfig, task, spec):
    """--repos 解析：只接受任务里有的仓库；省略即任务的全部仓库，按档案顺序。"""
    ordered = [a for a in cfg.repo_order if a in task["repos"]] + [a for a in task["repos"] if a not in cfg.repo_order]
    if not spec:
        return ordered
    wanted = {a.strip() for a in str(spec).split(",") if a.strip()}
    unknown = sorted(wanted - set(task["repos"]))
    if unknown or not wanted:
        raise WtError(f"{task['id']} 没有仓库 {'、'.join(unknown) or '（空）'}，可选：{'、'.join(ordered)}")
    return [a for a in ordered if a in wanted]


def repo_ready_problems(cfg: WtConfig, reg: Registry, task, alias, check):
    """单个仓库能否交付，返回 (问题, 新提交数)。问题只属于这个仓库，不连累任务里的其他仓库。"""
    r = task["repos"][alias]
    path = Path(r["path"])
    problems = []
    if git.dirty(path):
        problems.append("工作区有未提交改动")
    head = git.sha(path, "HEAD")
    upstream = r.get("base_sha") or base_ref(cfg)
    if task.get("base_task"):
        parent = reg.load(task["base_task"], must=False)
        if parent and parent["state"] in LIVE_STATES:
            upstream = parent["repos"][alias]["branch"]
    count = int(git.out(["rev-list", "--count", f"{upstream}..HEAD"], cwd=path))
    if cfg.repo(alias).gate.get("kind", "none") != "none" and count:
        c = check.get(alias)
        if not c or c.get("sha") != head or not c.get("ok"):
            problems.append(f"当前提交 {head[:9]} 没有通过 aisk task check")
    for h, body in git.log_messages(path, f"{upstream}..HEAD"):
        err = model.check_message(cfg, body)
        if err:
            problems.append(f"提交 {h[:9]} {err}")
    return problems, count


def cmd_ready(cfg, reg, args):
    """登记交付。**各仓库各自判定**：一个仓库没过只挡它自己，其余仓库照常 ready；--repos 只交付指定仓库。

    TASK.md、HANDOFF.md 属于整个任务，它们没写完哪个仓库都交付不了。
    没过的仓库保留上一次的 ready 登记：land 还会核对分支头，改过的提交不会被当成已交付。"""
    tool, sessions = caller(args)
    with reg.lock():
        task = recover_stale_queued(cfg, reg, reg.find_by_ref(args.task))
        require_local(cfg, task, ("active", "rejected", "parked", "ready"))
        refuse_if_foreign(cfg, task, tool, sessions, "交付")
        aliases = select_repos(cfg, task, getattr(args, "repos", None))
        check = (task.get("check") or {}).get("results", {})
        results = {alias: repo_ready_problems(cfg, reg, task, alias, check) for alias in aliases}
        task_problems = []
        if not any(count for _problems, count in results.values()):
            task_problems.append("所选仓库都没有新提交")
        task_md = Path(task["dir"]) / "TASK.md"
        if task_md.exists() and bind.PLACEHOLDER in task_md.read_text(encoding="utf-8"):
            task_problems.append("TASK.md 的目标或验收标准还是（待填写）")
        if bind.handoff_incomplete(task["dir"], cfg.handoff_sections):
            task_problems.append("HANDOFF.md 还有（待填写）或空的小节")
        failed = {alias: problems for alias, (problems, _count) in results.items() if problems}
        passed = [alias for alias in aliases if alias not in failed]
        for alias, problems in failed.items():
            for p in problems:
                say("err", f"{alias}: {p}")
        for p in task_problems:
            say("err", p)
        if task_problems or not passed:
            raise WtError("ready 未通过，修正后重试")
        for alias in passed:
            task["repos"][alias]["ready_sha"] = git.sha(Path(task["repos"][alias]["path"]), "HEAD")
        if cfg.os == "windows":  # pragma: no cover - 仅 Windows
            publish_to_hub(cfg, task, args=args, tool=tool, sessions=sessions)
        reg.set_state(task, "ready", note="ready：" + "、".join(passed) + (f"；未过：{'、'.join(failed)}" if failed else ""))
    say("ok", f"{task['id']} 已 ready：" + "，".join(f"{a}@{task['repos'][a]['ready_sha'][:9]}" for a in passed))
    if failed:
        say("warn", f"{'、'.join(failed)} 没有 ready，不影响已 ready 的仓库落地；修好后 "
                    f"{names.CLI} ready {task['id']} --repos {','.join(failed)}")
    refresh_board(cfg, reg)
    return 1 if failed else 0


def _hub_remote_url(repo):
    return git.out(["remote", "get-url", "hub"], cwd=repo, check=False).strip()


def _hub_remote_is_local(url):
    """Windows 侧中央交换只允许 hub 共享目录/UNC/本机路径，不把 ready 变成公网推送。"""
    value = str(url or "").lower()
    return (value.startswith(("file:", "\\\\", "/", "./", "../"))
            or (len(value) >= 3 and value[1:3] in (":\\", ":/")))


def _hub_confirm(args, tool, sessions, task, alias, repo, branch, sha):
    try:
        context = ActionContext(
            tool=tool or "", action="git推送", task_id=task["id"],
            session_id=(sessions or [""])[0], risk_level="HIGH",
            summary=f"{alias} 任务分支 {branch} 发布到中央 hub", repository=alias,
        )
    except (ActionContextError, IndexError, TypeError):
        return False
    prompt = (f"即将把任务 {task['id']} 的 {alias} 分支发布到中央交换 hub。\n\n"
              f"仓库：{alias}\n分支：{branch}\n提交：{sha[:9]}\n"
              "操作：只更新中央 hub，不推送 GitHub/公网；Mac 集成端后续仍需 land/promote。")
    print(prompt)
    return registry.confirm_human(prompt, "确认推送", context=context)


def publish_to_hub(cfg, task, *, args=None, tool=None, sessions=None):  # pragma: no cover - 仅 Windows
    """Windows ready 的中央交换发布：确认 → 非强制推送 → ls-remote 回读校验。

    旧实现使用 --force，既没有弹窗，也可能覆盖 Mac 侧刚发布的同名任务分支；
    现在任何一个 alias 失败都不写 ready 状态，并保留 partial 记录供重试。
    """
    published = []
    try:
        for alias, r in task["repos"].items():
            repo = cfg.repo(alias).path
            url = _hub_remote_url(repo)
            if not url:
                raise WtError(f"{alias}: 没有配置 hub 远端，无法中央交换")
            if not _hub_remote_is_local(url):
                raise WtError(f"{alias}: hub 不是本地/UNC 中央交换地址，Windows 禁止直接对外推送：{url[:120]}")
            sha = git.sha(repo, f"refs/heads/{r['branch']}")
            if not sha or sha != r.get("ready_sha"):
                raise WtError(f"{alias}: ready 提交与任务分支头不一致，请重新 ready")
            remote = git.out(["ls-remote", "--heads", "hub", f"refs/heads/{r['branch']}"], cwd=repo, check=False)
            old = remote.split()[0] if remote.split() else None
            if old == sha:
                published.append(alias)
                continue
            if not _hub_confirm(args, tool, sessions, task, alias, repo, r["branch"], sha):
                raise WtError(f"{alias}: 未确认中央 hub 发布，已停止")
            # 非强制推送：远端同名分支若已被另一端推进，Git 拒绝而不是覆盖。
            git.run(["push", "hub", f"refs/heads/{r['branch']}:refs/heads/{r['branch']}"], cwd=repo)
            check = git.out(["ls-remote", "--heads", "hub", f"refs/heads/{r['branch']}"], cwd=repo, check=False)
            got = check.split()[0] if check.split() else None
            if got != sha:
                raise WtError(f"{alias}: hub 回读提交 {str(got)[:9]} 与预期 {sha[:9]} 不一致")
            published.append(alias)
        data = {k: v for k, v in task.items() if not k.startswith("_")}
        data.update(state="ready", updated_at=now_iso(), hub_published=published)
        atomic_json(cfg.win_state_dir / f"{task['id']}.json", data)
    except Exception as exc:
        partial = {"id": task["id"], "state": "publish-partial", "published": published,
                   "failed": [a for a in task["repos"] if a not in published],
                   "error": str(exc)[:500], "updated_at": now_iso()}
        atomic_json(cfg.win_state_dir / f"{task['id']}.partial.json", partial)
        raise


def cmd_restack(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    require_local(cfg, task, LIVE_STATES)
    tool, sessions = caller(args)
    refuse_if_foreign(cfg, task, tool, sessions, "变基")
    for alias, r in task["repos"].items():
        path = Path(r["path"])
        if git.dirty(path):
            raise WtError(f"{alias} 有未提交改动，先提交")
        target = base_ref(cfg)
        if task.get("base_task"):
            parent = reg.load(task["base_task"], must=False)
            if parent and parent["state"] in LIVE_STATES:
                target = parent["repos"][alias]["branch"]
        res = git.run(["rebase", target], cwd=path, check=False)
        if res.returncode != 0:
            say("err", f"{alias}: rebase 到 {target} 有冲突。逐文件解决后 git add + git rebase --continue；"
                       f"放弃用 git rebase --abort")
            print((res.stdout + res.stderr).strip()[-2000:])
            return 1
        with reg.lock():
            task = reg.load(task["id"])
            task["repos"][alias].update(base_ref=target, base_sha=git.sha(path, target), ready_sha=None)
            task["check"] = None
            if task["state"] in ("rejected", "ready"):
                reg.set_state(task, "active", note="restack")
            else:
                reg.save(task)
        say("ok", f"{alias}: 已 rebase 到 {target}")
    return 0


# ------------------------------------------------------------------ salvage / merge-commit
def neutral(name):
    for k, v in {"codex": "cx", "claude": "cc", "gemini": "gm", "antigravity": "ag", "workbuddy": "wb",
                 "cursor": "cu"}.items():
        name = re.sub(k, v, name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._/-]", "-", name)


def salvage_dir(cfg, reg, wt, alias, base, label, date=None):
    wt = Path(wt).resolve()
    if not wt.is_dir() or not any(wt.iterdir()):
        raise WtError(f"抢救源目录不存在或为空：{wt}")
    repo = cfg.repo(alias).path
    base_sha = git.sha(repo, base)
    if not base_sha:
        raise WtError(f"{alias} 找不到基线 {base}")
    date = date or stamp()[:8]
    label = neutral(label)
    rec = {"dir": str(wt), "alias": alias, "base": base, "base_sha": base_sha, "label": label, "changed": False}
    commit = snapshot_worktree(cfg, alias, wt, base_sha, "wip: 在制品快照")
    if not commit:
        return rec
    ref = f"{names.REF_NS}/salvage/{date}/{alias}/{label}"
    git.run(["update-ref", ref, commit], cwd=repo)
    out_dir = cfg.salvage_dir / date
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / f"{alias}-{label.replace('/', '--')}.patch"
    patch.write_text(git.run(["diff", "--binary", base_sha, commit], cwd=repo).stdout, encoding="utf-8")
    rec.update(changed=True, ref=ref, commit=commit, patch=str(patch))
    reg.event("salvage", **rec)
    return rec


def cmd_salvage(cfg, reg, args):
    rec = salvage_dir(cfg, reg, args.dir, args.repo, args.base, args.label, args.date)
    say("ok", f"已快照到 {rec['ref']}，补丁 {rec['patch']}" if rec["changed"] else "相对基线没有改动，无需抢救")
    return 0


def cmd_merge_commit(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    require_local(cfg, task, LIVE_STATES)
    alias = args.repo or next(iter(task["repos"]))
    r = task["repos"][alias]
    path = Path(r["path"])
    merge_head = git.sha(path, "MERGE_HEAD")
    if not merge_head:
        raise WtError("当前没有进行中的合并（MERGE_HEAD 不存在）")
    if git.out(["ls-files", "-u"], cwd=path):
        raise WtError("还有未解决的冲突文件")
    msg = args.message or f"merge: 合并上游到任务({task['id']})"
    err = model.check_message(cfg, msg)
    if err:
        raise WtError(err)
    head = git.sha(path, "HEAD")
    commit = git.commit_tree(path, git.out(["write-tree"], cwd=path), [head, merge_head], msg)
    git.run(["update-ref", "-m", "aisk task merge-commit", f"refs/heads/{r['branch']}", commit, head], cwd=path)
    git.run(["merge", "--quit"], cwd=path)
    reg.event("merge-commit", task=task["id"], repo=alias, commit=commit)
    say("ok", f"{alias}: 合并提交 {commit[:9]}（仓库钩子的合并豁免在 worktree 里失效，这里按规则补落）")
    return 0


# ------------------------------------------------------------------ archive / restore
def harvest_workbuddy(cfg, task):
    alias = cfg.raw.get("workbuddy_memory_repo")
    if cfg.os != "mac" or not alias or alias not in task["repos"]:
        return []
    td = Path(task["dir"])
    mem = td / ".workbuddy" / "memory"
    if not mem.is_dir():
        return []
    seed_f = td / names.META_DIR / "workbuddy-seed.json"
    seed = json.loads(seed_f.read_text(encoding="utf-8")) if seed_f.exists() else {}
    dst = cfg.repo(alias).path / ".workbuddy" / "memory"
    dst.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in sorted(mem.glob("*.md")):
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        if seed.get(f.name) == h:
            continue
        target = dst / f.name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != h:
            target = dst / f"{f.stem}-{task['id']}{f.suffix}"
        shutil.copy2(f, target)
        moved.append(target.name)
    return moved


def build_artifact_dirs(cfg: WtConfig, task):
    """任务目录里的构建产物：node_modules/target/dist 这类，下次构建会重建，删了不丢工作成果。"""
    names_wanted = set(cfg.retention.get("build_dirs") or [])
    found = []
    for repo in (task.get("repos") or {}).values():
        root = Path(repo["path"])
        if not root.is_dir():
            continue
        for depth in range(1, 4):
            for path in root.glob("/".join(["*"] * depth)):
                if path.is_dir() and not path.is_symlink() and path.name in names_wanted:
                    if not any(str(path).startswith(str(f) + "/") for f in found):
                        found.append(path)
    return found


def dir_size(path):
    total = 0
    for child in Path(path).rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def gc_candidates(cfg: WtConfig, reg: Registry, include_live=False):
    """返回 (可归档的任务, 可清构建产物的任务)。可归档＝已上主干/已验收且超过保留期；
    工作成果不会丢：归档保留分支到 refs 下，需要时 restore 重开。

    构建产物默认只清不在进行中的任务：正在干活的会话删掉 node_modules 会当场打断它。"""
    hours = cfg.retention.get("promoted_hours", 72)
    deadline = time.time() - hours * 3600
    archivable, artifacts = [], []
    for t in reg.all(include_hub=False):
        if not is_local(cfg, t) or t.get("creating") or t.get("archiving"):
            continue
        if t["state"] in ("promoted", "verified"):
            last = parse_iso(t.get("updated_at") or t.get("created_at"))
            if last and last.timestamp() < deadline:
                archivable.append(t)
        cleanable = ("landed", "promoted", "verified") + (LIVE_STATES if include_live else ())
        if t["state"] in cleanable:
            dirs = build_artifact_dirs(cfg, t)
            if dirs:
                artifacts.append((t, dirs))
    return archivable, artifacts


def expired_logs(cfg: WtConfig):
    """超过保留期的构建日志。它们只是排查材料，过期后没人会再看。"""
    days = cfg.retention.get("archive_days", 30)
    deadline = time.time() - days * 86400
    out = []
    if cfg.logs_dir.is_dir():
        for path in sorted(cfg.logs_dir.rglob("*.log")):
            try:
                if path.stat().st_mtime < deadline:
                    out.append(path)
            except OSError:
                continue
    return out


def retirable_archive_refs(cfg: WtConfig, reg: Registry):
    """超过保留期、且提交已并入集成分支或主干的归档引用。

    **没并入的一律不动**：那条引用可能是这份工作唯一的副本，过期删掉就是真的丢代码。
    """
    days = cfg.retention.get("archive_days", 30)
    deadline = time.time() - days * 86400
    retirable, kept = [], []
    for t in reg.all(include_archived=True, include_hub=False):
        if t.get("state") != "archived" or not is_local(cfg, t):
            continue
        last = parse_iso(t.get("updated_at"))
        if not last or last.timestamp() > deadline:
            continue
        for alias in (t.get("repos") or {}):
            try:
                repo = cfg.repo(alias).path
            except WtError:
                continue
            ref = f"{names.REF_NS}/archive/{t['name']}/{alias}"
            sha = git.sha(repo, ref)
            if not sha:
                continue
            auto = cfg.repo(alias).automatic
            repo_record = (t.get("repos") or {}).get(alias) or {}
            if auto.get("push_only"):
                # Local fxh reachability alone is insufficient for tasks whose
                # only recovery copy may be the archive ref while fxh-dev is
                # still pending, rejected, or not yet verified remotely.
                operation_key = repo_record.get("publish_operation_key")
                state = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending").get(operation_key) \
                    if operation_key else None
                if not repo_record.get("remote_sha") or not state or state.get("status") != publish_pending.PUBLISHED:
                    kept.append((t, alias, repo, ref, sha))
                    continue
            merged = any(git.sha(repo, b) and git.is_ancestor(repo, sha, b)
                         for b in (cfg.integration, cfg.repo(alias).trunk))
            (retirable if merged else kept).append((t, alias, repo, ref, sha))
    return retirable, kept


def cmd_gc(cfg, reg, args):
    """回收任务目录：默认只报告，--apply 才动。

    **为什么需要它**：归档是唯一的回收器，但它排在「部署验收」之后——现场实测一天就攒下 3GB，
    其中 2.6GB 是每个任务各装一份的 node_modules。等人工验收再回收，磁盘先扛不住。
    """
    hours = cfg.retention.get("promoted_hours", 72)
    archivable, artifacts = gc_candidates(cfg, reg, include_live=args.include_live)
    art_bytes = sum(dir_size(d) for _t, dirs in artifacts for d in dirs) if (args.build_artifacts or not args.apply) else 0
    task_bytes = sum(dir_size(t["dir"]) for t in archivable if Path(t["dir"]).is_dir())
    logs = expired_logs(cfg)
    retirable, kept_refs = retirable_archive_refs(cfg, reg)
    if not archivable and not artifacts and not logs and not retirable:
        say("ok", "没有可回收的对象")
        if kept_refs:
            say("info", f"{len(kept_refs)} 条归档引用因为还没并入主干而保留（它可能是那份工作唯一的副本）")
        return 0
    for t in archivable:
        last = parse_iso(t.get("updated_at"))
        idle = model.age_text(last.timestamp(), time.time()) if last else "未知时长"
        size = dir_size(t["dir"]) // (1 << 20) if Path(t["dir"]).is_dir() else 0
        say("info", f"可归档 {t['id']} {t['title']}（{t['state']}，{idle}未动，{size}MB）")
    if args.build_artifacts or not args.apply:
        for t, dirs in artifacts:
            say("info", f"可清构建产物 {t['id']}：{len(dirs)} 个目录，"
                        f"{sum(dir_size(d) for d in dirs) // (1 << 20)}MB")
    if logs:
        say("info", f"可清过期构建日志 {len(logs)} 个（超过 {cfg.retention.get('archive_days', 30)} 天）")
    for t, alias, _repo, ref, sha in retirable:
        say("info", f"可退休归档引用 {ref}（{sha[:9]} 已并入主干）")
    if kept_refs:
        say("info", f"{len(kept_refs)} 条归档引用没并入主干，保留不动")
    if not args.apply:
        say("warn", f"以上为预览：归档 {len(archivable)} 个任务可回收 {task_bytes // (1 << 20)}MB，"
                    f"清构建产物可回收 {art_bytes // (1 << 20)}MB。"
                    f"确认后 {names.CLI} gc --apply [--build-artifacts]；阈值见档案 worktrees.retention（当前 {hours} 小时）。"
                    f"进行中的任务默认不清构建产物，要一起清加 --include-live")
        return 0
    freed = 0
    if args.build_artifacts:
        for t, dirs in artifacts:
            for d in dirs:
                size = dir_size(d)
                remove_tree(d)
                freed += size
            say("ok", f"{t['id']} 构建产物已清")
    for t in archivable:
        size = dir_size(t["dir"]) if Path(t["dir"]).is_dir() else 0
        # 复用 archive 这条已经验证过的回收路径，而不是另写一遍删目录
        cmd_archive(cfg, reg, SimpleNamespace(task=t["id"], force=True, abandon=False, tool="human", session=None))
        freed += size
    for path in logs:
        path.unlink(missing_ok=True)
    if logs:
        say("ok", f"已清过期构建日志 {len(logs)} 个")
    for t, alias, repo, ref, sha in retirable:
        git.run(["update-ref", "-d", ref, sha], cwd=repo, check=False)
        reg.event("gc-ref", task=t["id"], alias=alias, ref=ref, sha=sha)
    if retirable:
        say("ok", f"已退休 {len(retirable)} 条归档引用（都已并入主干，提交仍在主干历史里）")
    say("ok", f"共回收 {freed // (1 << 20)}MB；未并入主干的归档分支仍在 {names.REF_NS}/archive 下，"
              f"需要时 {names.CLI} restore")
    return 0


def cmd_archive(cfg, reg, args):
    tool, sessions = caller(args)
    with reg.lock():
        task = reg.find_by_ref(args.task)
        if task.get("direct_checkout"):
            raise WtError("direct 任务没有 worktree 可归档；使用 direct-finish 或干净基线下的 direct-abort")
        if not is_local(cfg, task):
            raise WtError("只能在任务所属系统上归档")
        resuming = bool(task.get("archiving"))
        if not resuming:
            children = [t["id"] for t in reg.all() if t.get("base_task") == task["id"] and t["state"] in LIVE_STATES]
            if children:
                raise WtError("仍有依赖它的任务，请先 restack：" + ", ".join(children))
            ok_states = ("verified",) + (("landed", "promoted") if args.force else ())
            if task["state"] not in ok_states and not args.abandon:
                raise WtError(f"状态 {task['state']} 不能归档：验证通过后归档，或 --force（已落地未验证）/"
                              f" --abandon（放弃，未提交改动先自动快照）")
            refuse_if_foreign(cfg, task, tool, sessions, "归档")
            for alias, r in task["repos"].items():  # 先整体预检，任何一个仓库不满足都不动手
                path = Path(r["path"])
                if path.exists() and git.dirty(path) and not args.abandon:
                    raise WtError(f"{alias} 有未提交改动，拒绝归档")
            task["archiving"] = now_iso()
            task["archive_mode"] = "abandon" if args.abandon else "archive"
            reg.save(task)
    td = Path(task["dir"])
    adir = cfg.archive_dir / task["name"]
    try:  # 删除 worktree、快照在制品都可能很慢，不占登记簿锁；中断后重跑 archive 会从这里继续
        for alias, r in task["repos"].items():
            repo = cfg.repo(alias).path
            path = Path(r["path"])
            snap = None
            if path.exists() and git.dirty(path):
                snap = salvage_dir(cfg, reg, path, alias, r["branch"], task["name"]).get("commit")
            tip = git.sha(repo, r["branch"])
            if tip:
                git.run(["update-ref", f"{names.REF_NS}/archive/{task['name']}/{alias}", snap or tip], cwd=repo)
            if path.exists():
                git.run(["worktree", "unlock", str(path)], cwd=repo, check=False)
                git.run(["worktree", "remove", "--force", str(path)], cwd=repo)
            if tip:
                git.run(["branch", "-D", r["branch"]], cwd=repo, check=False)
        moved = harvest_workbuddy(cfg, task)
        adir.mkdir(parents=True, exist_ok=True)
        for name in ("TASK.md", "PROGRESS.md", "HANDOFF.md"):
            if (td / name).exists():
                shutil.copy2(td / name, adir / name)
        if td.exists() and not task.get("legacy"):
            remove_tree(td)
    except BaseException as error:
        with reg.lock():
            current = reg.load(task["id"])
            current["archive_error"] = str(error)[:500]
            reg.save(current)
        raise
    with reg.lock():
        task = reg.load(task["id"])
        mode = task.pop("archive_mode", "archive")
        task.pop("archiving", None)
        task.pop("archive_error", None)
        task["owner"] = None
        reg.set_state(task, "archived", note=mode)
    say("ok", f"{task['id']} 已归档：分支存于 {names.REF_NS}/archive/{task['name']}/<仓库>，记录在 {adir}")
    if moved:
        say("info", f"WorkBuddy 记忆已收割：{', '.join(moved)}")
    refresh_board(cfg, reg)
    return 0


def cmd_harvest(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    if not is_local(cfg, task):
        raise WtError("只能在任务所属系统上收割")
    moved = harvest_workbuddy(cfg, task)
    say("ok", f"WorkBuddy 记忆已收割：{', '.join(moved)}" if moved else "没有新的 WorkBuddy 记忆需要收割")
    return 0


def cmd_restore(cfg, reg, args):
    old = reg.find_by_ref(args.task)
    if old["state"] != "archived":
        raise WtError("只有已归档的任务可以恢复")
    refs = {}
    for alias in old["repos"]:
        for namespace in (names.REF_NS, *names.LEGACY_REF_NS):
            ref = f"{namespace}/archive/{old['name']}/{alias}"
            if git.sha(cfg.repo(alias).path, ref):
                refs[alias] = ref
                break
        else:
            raise WtError(f"{old['id']}/{alias} 的归档引用不存在，未创建恢复任务")
    tool, sessions = caller(args)
    task = create_task(cfg, reg, slug=args.slug or old["slug"], title=args.title or old["title"],
                       repos=list(old["repos"]), goal=old.get("goal"), accept=old.get("accept"), from_refs=refs,
                       new_anyway=f"恢复 {old['id']}", draft=True, tool=tool, sessions=sessions,
                       extra={"restored_from": old["id"]})
    archive = cfg.archive_dir / old["name"]
    for name in ("TASK.md", "PROGRESS.md", "HANDOFF.md"):
        if (archive / name).is_file():
            shutil.copy2(archive / name, Path(task["dir"]) / name)
    say("ok", f"已从 {old['id']} 恢复为 {task['id']}：{task['dir']}")
    refresh_board(cfg, reg)
    return 0


# ------------------------------------------------------------------ adopt / adopt-branch
def cmd_adopt_branch(cfg, reg, args):
    alias = args.repo
    if cfg.repo(alias).workspace_mode == "direct":
        raise WtError(f"{alias} 是 direct 仓库，不能 adopt 成任务 worktree")
    repo = cfg.repo(alias).path
    if args.branch in cfg.protected:
        raise WtError("受保护分支不能被接手成任务")
    if not git.branch_exists(repo, args.branch):
        raise WtError(f"{alias} 没有分支 {args.branch}")
    holders = [w for w in git.worktree_list(repo) if w.get("branch") == args.branch]
    if holders:
        where = holders[0]["path"]
        if Path(where).resolve() == repo.resolve():
            raise WtError(f"{args.branch} 正检出在主工作区：请那边提交并切回 {cfg.integration} 后再接手")
        raise WtError(f"{args.branch} 正检出在 {where}：会话结束后用 aisk task adopt 收编那个目录")
    tool, sessions = caller(args)
    task = create_task(cfg, reg, slug=args.slug, title=args.title, repos=[alias], goal=args.goal,
                       accept=args.accept, source_branches={alias: args.branch}, new_anyway=args.new_anyway,
                       tool=tool, sessions=sessions, draft=not (args.goal and args.accept))
    append_progress(task, progress_line(tool or "操作者", f"从分支 {args.branch} 接手（原分支保留不动）"))
    say("ok", f"{task['id']} {task['title']} 已接手分支 {args.branch}：{task['dir']}")
    refresh_board(cfg, reg)
    return 0


def cmd_adopt(cfg, reg, args):
    src = Path(args.path).resolve()
    alias = args.repo
    if cfg.repo(alias).workspace_mode == "direct":
        raise WtError(f"{alias} 是 direct 仓库，不能 adopt 成任务 worktree")
    repo = cfg.repo(alias).path
    good, info = git.backlink_ok(src)
    if not good:
        raise WtError(f"不能收编不健康的 worktree：{info}")
    matches = [w for w in git.worktree_list(repo) if Path(w["path"]).resolve() == src]
    if not matches:
        raise WtError("源 worktree 不属于指定仓库")
    wt = matches[0]
    if wt.get("branch") in cfg.protected or wt.get("locked"):
        raise WtError("源 worktree 检出了受保护分支，或仍被锁定（会话可能仍在运行）")
    model.validate_slug(cfg, args.slug)
    title = model.validate_title(args.title)
    with reg.lock():
        if reg.find_by_path(src):
            raise WtError("源目录已受任务引擎管理")
        check_quota(cfg, reg, repos=[alias], materialize=[alias])
        tid = reg.next_id()
        name = f"{tid}-{args.slug}"
        td = cfg.tasks_dir / name
        dest = td / alias
        branch = cfg.task_branch(name)
        old_admin = git.admin_dir_of(src)
        new_admin = old_admin.parent / f"{names.TASK_ADMIN_PREFIX}{tid}-{alias}"
        if td.exists() or new_admin.exists() or git.branch_exists(repo, branch):
            raise WtError("目标任务目录、分支或管理目录已存在")
        base = git.merge_base(src, base_ref(cfg), "HEAD") or git.sha(src, "HEAD")
        block = free_port_block(reg)
        task = {"id": tid, "slug": args.slug, "name": name, "title": title, "goal": args.goal or "",
                "accept": args.accept or "", "os": cfg.os, "dir": str(td), "port_block": block,
                "ports": cfg.ports_for(block), "base_task": None, "scope": [], "source_branches": {},
                "repos": {alias: {"branch": branch, "path": str(dest), "admin": new_admin.name,
                                  "base_ref": base_ref(cfg), "base_sha": base, "ready_sha": None,
                                  "landed_sha": None}},
                "state": "active", "owner": None, "created_at": now_iso(), "history": [],
                "adopted_from": str(src), "base_short": base[:9]}
        moved = renamed_admin = renamed_branch = False
        try:
            td.mkdir(parents=True)
            git.run(["worktree", "move", str(src), str(dest)], cwd=repo)
            moved = True
            os.replace(old_admin, new_admin)
            renamed_admin = True
            (dest / ".git").write_text(f"gitdir: {new_admin}\n", encoding="utf-8")
            git.run(["worktree", "repair", str(dest)], cwd=repo)
            if wt.get("branch"):
                git.run(["branch", "-m", wt["branch"], branch], cwd=dest)
            else:
                git.run(["switch", "-c", branch], cwd=dest)
            renamed_branch = True
            git.run(["worktree", "lock", "--reason", f"{names.CLI} {tid} os={cfg.os}", str(dest)], cwd=repo)
            bind.write_task_files(cfg, task, java_home(cfg, [alias]))
            reg.save(task)
        except Exception as error:
            try:
                if moved:
                    git.run(["worktree", "unlock", str(dest)], cwd=repo, check=False)
                if renamed_branch:
                    if wt.get("branch"):
                        git.run(["branch", "-m", branch, wt["branch"]], cwd=dest)
                    else:
                        git.run(["switch", "--detach"], cwd=dest)
                        git.run(["branch", "-D", branch], cwd=dest)
                if renamed_admin:
                    os.replace(new_admin, old_admin)
                    (dest / ".git").write_text(f"gitdir: {old_admin}\n", encoding="utf-8")
                    git.run(["worktree", "repair", str(dest)], cwd=repo)
                if moved:
                    git.run(["worktree", "move", str(dest), str(src)], cwd=repo)
                if td.exists() and not dest.exists():
                    remove_tree(td)
                reg.task_file(tid).unlink(missing_ok=True)
            except Exception as rollback:  # noqa: BLE001
                raise WtError(f"收编失败且自动恢复未完成，保留现场 {td}：{rollback}") from error
            raise
        reg.event("adopt", task=tid, src=str(src))
    say("ok", f"已收编为 {tid} {title}：{dest}")
    refresh_board(cfg, reg)
    return 0


def try_lock(reg: Registry):
    """钩子与守卫用：拿不到登记簿锁就放弃，不能卡住工具。"""
    return file_lock(reg.cfg.locks_dir / "registry.lock", blocking=False)
