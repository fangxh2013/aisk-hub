# -*- coding: utf-8 -*-
"""调用者身份：哪个工具、哪个会话。

同一工具经常同时开多个会话。只比工具名，第二个会话就会悄悄"续租"第一个会话的任务——
这正是重复劳动的来源，所以认领记录保存会话号，任一会话号相同才算同一执行者。

会话号来源（取到几个记几个；钩子与命令行必须至少有一个共同来源，否则同一会话会被当成两个执行者）：
- 显式 `--session`、环境变量 AISK_SESSION（Claude 的 SessionStart 钩子会写进会话环境文件）
- 钩子输入：`session_id`（Claude / Codex / WorkBuddy）、`conversationId`（Antigravity）
- 工具进程号：Claude 提供 CLAUDE_PID；其余工具沿父进程链找工具主进程（钩子进程与命令行进程同属一个主进程）
一个会话号都取不到时，显式传 --session；桌面应用的进程号不能区分同一应用的多个会话。
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import names

TOOLS = ("claude", "codex", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human")
SESSION_ENV = {
    "claude": ("CLAUDE_CODE_SESSION_ID",),
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
    "workbuddy": ("CODEBUDDY_SESSION_ID", "WORKBUDDY_SESSION_ID"),
    "workbuddy-ai": ("CODEBUDDY_SESSION_ID", "WORKBUDDY_AI_SESSION_ID"),
}
PID_ENV = {"claude": "CLAUDE_PID"}
# 两个 WorkBuddy 入口的可执行文件名相同（都跑 codebuddy 引擎），进程链本来就分不开它们——
# 分开记账靠的是 _is_workbuddy_ai() 读环境变量。这里补 workbuddy-ai 只是让表与
# detect_tool() 的返回值域对齐，避免以后有人把 ancestor_pid 接进来时静默查不到 key。
PROCESS_NAMES = {"codex": ("codex",), "workbuddy": ("codebuddy", "WorkBuddy"),
                 "workbuddy-ai": ("codebuddy", "WorkBuddy"), "antigravity": ("language_server",)}


def _is_workbuddy_ai(env):
    """是不是 WorkBuddy AI，而不是桌面版 WorkBuddy。

    两个入口必须分开记账（见 `adapters/workbuddy-ai/README.md`）：混成同一个工具名，
    同一台机器上的两个 App 会共用任务租约与弹窗归属——桌面版与 AI 版在这台机器上都在用。

    判别变量换过代，只认旧变量会静默判错：
    - 旧壳：`WORKBUDDY_DATA_FOLDER_NAME=.workbuddy-ai` 或 `CODEBUDDY_APP=workbuddy-ai`
    - 现壳（2026-09-19 实测）：上面两个都不再注入，只注入 `WORKBUDDY_CONFIG_DIR`，
      指向应用数据根（`~/.workbuddy-ai`）。桌面版该值是 `~/.workbuddy`。
    """
    if env.get("WORKBUDDY_DATA_FOLDER_NAME") == ".workbuddy-ai":
        return True
    if env.get("CODEBUDDY_APP") == "workbuddy-ai":
        return True
    config_dir = env.get("WORKBUDDY_CONFIG_DIR") or ""
    return os.path.basename(config_dir.rstrip("/\\")) == ".workbuddy-ai"


def detect_tool(env=None):
    env = os.environ if env is None else env
    explicit = env.get(names.ENV_TOOL)
    if explicit in TOOLS:
        return explicit
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    # 门保持只看 CODEBUDDY_*：新增门变量会要求 tests/protocol_worktree.sh 的清理循环
    # 同步加前缀（它只清 CODEX_*/CODEBUDDY_* 等），否则沙箱清不干净、那道自检会假红。
    # 两个入口的区分交给下面的 _is_workbuddy_ai，它认多代判别变量。
    if any(k.startswith("CODEBUDDY_") for k in env):
        return "workbuddy-ai" if _is_workbuddy_ai(env) else "workbuddy"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    return None


AUTO_TOOL = "auto"


def resolve_tool(name, fallback="workbuddy", env=None):
    """把钩子命令里写死的工具名，解析成运行期的真实端。

    `auto` 是钩子生成方（`bind.py`）为 WorkBuddy 两个入口预留的哨兵值。原因：
    桌面版与 AI 版**共读同一份** `<项目根>/.codebuddy/settings.json`（实测两端的引擎
    都只读这一份），而这份文件只能写一个工具名——写死哪个，另一端的会话事件与守卫
    就会以那个名义记账。传 `auto` 让钩子在**自己的进程里**解析：实测宿主会把
    `WORKBUDDY_CONFIG_DIR` 传进钩子进程，所以解析结果就是当前真正在跑的那一端。

    其余取值**原样返回**——codex / antigravity / claude 的显式工具名行为完全不变。

    解析不出来时回落到 `workbuddy`（桌面版），即最坏情况等同于改造前的写死行为。
    """
    if name != AUTO_TOOL:
        return name
    return detect_tool(env) or fallback


def ancestor_pid(prefixes):
    """沿父进程链向上找可执行文件名以 prefixes 之一开头的进程，返回其 pid；找不到返回 None。"""
    if sys.platform.startswith("win"):
        return None
    try:
        text = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table = {}
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
    pid = os.getppid()
    for _ in range(16):
        if pid not in table:
            return None
        ppid, comm = table[pid]
        if any(comm == p or comm.startswith(p + "-") or comm.startswith(p + " ") for p in prefixes):
            return pid
        if ppid == pid:
            return None
        pid = ppid
    return None


def session_ids(tool, explicit=None, payload=None, env=None):
    env = os.environ if env is None else env
    payload = payload or {}
    ids = []

    def add(value):
        if value and str(value) not in ids:
            ids.append(str(value))

    if explicit:
        return [str(explicit)]
    if payload.get("session_id") or payload.get("conversationId"):
        return [str(payload.get("session_id") or payload.get("conversationId"))]
    add(env.get(names.ENV_SESSION))
    for key in SESSION_ENV.get(tool or "", ()):
        add(env.get(key))
    pid_key = PID_ENV.get(tool or "")
    if not ids and pid_key and env.get(pid_key):
        add(f"pid:{env[pid_key]}")
    return ids
