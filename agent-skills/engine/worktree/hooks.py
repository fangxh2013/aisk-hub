# -*- coding: utf-8 -*-
"""工具钩子适配（入口 `aisk task hook <工具> [--event 事件]`，stdin 为钩子 JSON）。

Claude、Codex、WorkBuddy（CodeBuddy 引擎）用同一套事件与输出协议：
- SessionStart：在任务目录里开会话时，能认领就为本会话认领并注入须知；被别人持有中注入"只读"提示；
- Stop：本会话持有的任务续心跳；
- SessionEnd：本会话持有的任务自动交还（接手的人看 PROGRESS.md 续做），并收割 WorkBuddy 记忆；
- Claude 独有 WorktreeCreate / WorktreeRemove：原生 `--worktree` 改由任务引擎建任务；移除时保留在制品并暂停。

Antigravity 没有会话起止事件：
- PreInvocation（每轮第一次调用模型前）：认领或提示，输出 injectSteps 的临时消息；
- Stop（每轮结束）：续心跳。会话号取钩子输入的 conversationId。

除 WorktreeCreate / WorktreeRemove 外，任何异常都静默放行：钩子不能拖垮会话。
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import actor, bind, config, gitops as git, model, names, tasks
from .config import WtError
from .guards import find_task_root
from .registry import LIVE_STATES, Registry, atomic_json

CLAUDE_LIKE = ("claude", "codex", "workbuddy", "workbuddy-ai")


def _load(*starts):
    root = None
    for start in (*starts, os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get("CODEBUDDY_PROJECT_DIR")):
        root = find_task_root(start) if start else None
        if root:
            break
    if root is None:
        return None, None, None
    meta = json.loads(names.task_meta_file(root).read_text(encoding="utf-8"))
    cfg, _ = config.load_config(meta.get("profile"), start=root)
    reg = Registry(cfg)
    return cfg, reg, reg.load(meta["id"], must=False)


# ------------------------------------------------------------------ Claude 原生 worktree
def worktree_create(payload, cfg=None):
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    if cfg is None:
        cfg, _ = config.load_config(start=cwd)
    reg = Registry(cfg)
    alias = next((a for a in cfg.repo_order if cfg.repo(a).path.exists()
                  and (cwd == cfg.repo(a).path.resolve() or cfg.repo(a).path.resolve() in cwd.parents)), None)
    if alias is None:
        raise WtError(f"请在档案登记的主仓库目录里使用原生 --worktree；已有任务请 {names.CLI} claim 续做")
    name = str(payload.get("name") or "task")
    slug = model.slug_from_name(tasks.neutral(name), cfg.ai_re)
    title = name.strip()[:30] or slug
    sessions = actor.session_ids("claude", payload=payload)
    suffix, base_slug = 1, slug
    while True:
        try:
            task = tasks.create_task(cfg, reg, slug=slug, title=title, repos=[alias], draft=True, tool="claude",
                                     sessions=sessions)
            break
        except WtError as e:
            if "已存在分支" not in str(e) or suffix > 20:
                raise
            suffix += 1
            slug = f"{base_slug}-{suffix}" if base_slug.count("-") < 4 else f"{base_slug.rsplit('-', 1)[0]}-{suffix}"
    path = Path(task["repos"][alias]["path"])
    local = path / ".claude" / "settings.local.json"
    if git.ok(["check-ignore", "-q", ".claude/settings.local.json"], cwd=path):
        atomic_json(local, bind.claude_settings(cfg, task))
    else:
        print(f"{names.CLI}: {alias} 未忽略 .claude/settings.local.json，原生 worktree 会话里没有守卫钩子，"
              f"建议改用 {names.CLI} open {task['id']}", file=sys.stderr)
    return str(path)


def worktree_remove(payload):
    target = Path(payload["worktree_path"]).resolve()
    cfg, reg, task = _load(target)
    if not task:
        raise WtError("此路径不属于任何任务，未删除任何文件")
    if task["state"] in ("active", "ready", "rejected", "parked"):
        sessions = actor.session_ids("claude", payload=payload)
        args = type("A", (), {"task": task["id"], "tool": "claude", "session": sessions[0] if sessions else None,
                              "next": "原生 worktree 会话已结束，在制品保留在任务目录"})()
        with contextlib.redirect_stdout(sys.stderr):
            tasks.cmd_pause(cfg, reg, args)
    return None


# ------------------------------------------------------------------ 会话事件
def session_hint(tool, sessions):
    if tool == "claude" or not sessions:
        return ""
    return (f"本会话标识 {sessions[0]}：在任务里执行 {names.CLI} 命令时加 --session {sessions[0]}"
            f"（或先设置环境变量 {names.ENV_SESSION}={sessions[0]}），否则会被当成另一个会话。")


def export_claude_session(payload):
    """Claude 在后续 Bash 中加载 CLAUDE_ENV_FILE，让钩子与命令行使用同一个会话号。"""
    session = payload.get("session_id")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if session and env_file:
        with open(env_file, "a", encoding="utf-8") as stream:
            stream.write(f"export {names.ENV_SESSION}={shlex.quote(str(session))}\n")


def session_start(payload, tool="claude", cwd=None):
    cfg, reg, task = _load(cwd or payload.get("cwd"))
    if not task or task.get("_from_hub"):
        return None
    if tool == "claude":
        export_claude_session(payload)
    if task["state"] not in LIVE_STATES:
        return (f"本目录是任务 {task['id']}（{task['title']}），状态 {task['state']}，不再接受改动；"
                f"需要续做请操作者 {names.CLI} restore 或另开任务。")
    sessions = actor.session_ids(tool, payload=payload)
    if not sessions:
        return f"未识别到会话号，请设置本会话独有的 {names.ENV_SESSION} 后执行 {names.CLI} claim；本会话尚未认领。"
    hint = session_hint(tool, sessions)
    with reg.lock():
        task = reg.load(task["id"])
        lease, act = tasks.lease_of(cfg, task)
        allowed, why = model.claim_decision(task.get("owner"), lease, tool, sessions, takeover=False)
        prev = task.get("owner")
        if allowed and not task.get("creating") and not task.get("archiving"):
            if task["state"] == "parked":
                try:
                    tasks.check_quota(cfg, reg, exclude=task["id"])
                except WtError as e:
                    return f"任务 {task['id']} 暂停中，本机配额已满，未自动认领：{e}"
            task["owner"] = tasks.new_owner(cfg, tool, sessions)
            if why != "续租":
                note = "会话开始，自动认领" + (f"（原执行者 {tasks.owner_label(prev)} 已超时）" if prev else "")
                tasks.append_progress(task, tasks.progress_line(tool, note))
            if task["state"] == "parked":
                reg.set_state(task, "active", note="会话开始自动认领")
            else:
                reg.save(task)
            reg.event("claim", task=task["id"], tool=tool, prev=tasks.owner_label(prev), lease=lease, auto=True)
            return (f"已为本会话认领任务 {task['id']}（{task['title']}）。先读 AGENT-WORKTREE.md、TASK.md、PROGRESS.md；"
                    f"上次下一步：{tasks.last_next(task) or '（无记录）'}。过程中用 {names.CLI} note {task['id']} "
                    f"--done … --next … 记进度，离开前 {names.CLI} pause。{hint}")
    age = model.age_text(act, time.time())
    state = (f"空闲中，确认对方不会回来可 {names.CLI} claim {task['id']} --tool {tool} --takeover --reason …"
             if lease == model.IDLE else "持有中")
    return (f"⚠ 任务 {task['id']}（{task['title']}）由 {tasks.owner_label(prev)} {state}（{age}活动）。"
            f"本会话只读：不要修改文件、不要提交。{hint}")


def heartbeat_event(payload, tool, cwd=None):
    cfg, reg, task = _load(cwd or payload.get("cwd"))
    if not task or task.get("_from_hub"):
        return False
    sessions = actor.session_ids(tool, payload=payload)
    if not model.same_actor(task.get("owner"), tool, sessions):
        return False
    with tasks.try_lock(reg) as got:
        if got:
            return tasks.heartbeat(reg, reg.load(task["id"]), tool, sessions)
    return False


def session_end(payload, tool="claude"):
    cfg, reg, task = _load(payload.get("cwd"))
    if not task or task.get("_from_hub"):
        return None
    sessions = actor.session_ids(tool, payload=payload)
    released = False
    with reg.lock():
        task = reg.load(task["id"])
        if model.same_actor(task.get("owner"), tool, sessions) and task["state"] in LIVE_STATES:
            reason = payload.get("reason") or ""
            tasks.release_task(cfg, reg, task, tool, f"会话结束（{reason}），自动交还" if reason else "会话结束，自动交还")
            released = True
    if released:
        tasks.harvest_workbuddy(cfg, task)
    return None


def run_event(payload, tool, event):
    if event == "WorktreeCreate" and tool == "claude":
        return worktree_create(payload)
    if event == "WorktreeRemove" and tool == "claude":
        return worktree_remove(payload)
    if event == "SessionStart":
        text = session_start(payload, tool)
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                          ensure_ascii=False) if text else None
    if event == "Stop":
        heartbeat_event(payload, tool)
        return None
    if event == "SessionEnd":
        return session_end(payload, tool)
    return None


def run_claude(payload):
    # 不猜事件：缺事件名时什么都不做。曾默认当成 WorktreeCreate，一份空载荷就建出了真任务与 worktree
    return run_event(payload, "claude", payload.get("hook_event_name") or "")


# ------------------------------------------------------------------ Antigravity
def antigravity_event(payload, event):
    workspaces = payload.get("workspacePaths") or []
    cwd = workspaces[0] if workspaces else os.getcwd()
    if event == "PreInvocation":
        cfg, reg, task = _load(cwd)
        if not task:
            return {}
        sessions = actor.session_ids("antigravity", payload=payload)
        if model.same_actor(task.get("owner"), "antigravity", sessions):
            heartbeat_event(payload, "antigravity", cwd=cwd)
            return {}
        if int(payload.get("invocationNum") or 0) > 1:
            return {}  # 每轮只在第一次调用模型前提示一次；写入仍由守卫把关
        text = session_start(payload, "antigravity", cwd=cwd)
        return {"injectSteps": [{"ephemeralMessage": text}]} if text else {}
    if event == "Stop":
        heartbeat_event(payload, "antigravity", cwd=cwd)
    return {}


def main(tool, stdin=None, event=None):
    # 同 guards.main：WorkBuddy 两入口共用一份项目级 settings，工具名由哨兵 `auto`
    # 在运行期解析，避免 AI 端会话被记成桌面版。其余取值原样返回。
    tool = actor.resolve_tool(tool)
    try:
        payload = json.load(stdin or sys.stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if tool == "antigravity":
        try:
            with contextlib.redirect_stdout(sys.stderr):
                out = antigravity_event(payload, event or "")
        except Exception:  # noqa: BLE001
            out = {}
        print(json.dumps(out or {}, ensure_ascii=False))
        return 0
    if tool not in CLAUDE_LIKE:
        print(f"{names.CLI}: 暂不支持 {tool} 的钩子", file=sys.stderr)
        return 0
    event_name = event or payload.get("hook_event_name") or ""
    if not event_name:
        print(f"{names.CLI}: 钩子输入没有事件名，未做任何操作", file=sys.stderr)
        return 0
    try:
        with contextlib.redirect_stdout(sys.stderr):
            out = run_event(payload, tool, event_name)
    except Exception as e:  # noqa: BLE001
        if event_name in ("WorktreeCreate", "WorktreeRemove"):
            print(f"{names.CLI}: {e}", file=sys.stderr)
            return 1
        return 0
    if out:
        print(out)
    return 0
