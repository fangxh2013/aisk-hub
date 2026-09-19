# -*- coding: utf-8 -*-
"""各端的只读动词白名单生成（决策 5：只读动词全白名单免确认）。

**为什么要有这层**：`aisk` 的只读动词本身就是安全边界——broker 里生产写操作
根本没实现，`db query` 有只读白名单 + 脱敏 + 行数上限。既然边界在 CLI 里，
端上就不该再对每条只读查询弹确认，否则「无人值守自主执行」无从谈起。

**这比现状更安全，不是更松**：以前授权的是裸 `mysql` / `kubectl`（靠正则守），
现在授权的是「只读 broker 动词」，写操作在默认路径上根本调不出来。

**各端能力差异很大，如实分档**：
  claude / cursor  原生支持命令前缀白名单，直接生成（结构近乎同构）
  antigravity      无命令前缀白名单，但有 hooks.json 的 PreToolUse，等效实现
  workbuddy        已核实：与 claude 同构，写 user 作用域
                   `~/.codebuddy/settings.json`（桌面版与 AI 版同一份，见
                   `apply_workbuddy` 的 docstring）
  codex            本适配器没有实现当前宿主的权限配置生成；保留现有配置，
                   不据此推断产品是否支持命令规则

**只增不改**：所有写入都是合并进已有配置，不覆盖用户自己的条目。
"""

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

# 只读动词。写操作（db exec）刻意不在列——它必须次次确认。
READONLY_VERBS = [
    "aisk env *",
    "aisk fact *",
    "aisk repo *",
    "aisk profile",
    "aisk doctor",
    "aisk secret list",
    "aisk secret status",
    "aisk db query *",
    "aisk netcheck",
]

# 明确拒绝的：即使用户手滑加进 allow，这些也该挡住
DENY_VERBS = [
    "aisk db exec *",           # 写操作，必须人工确认
    "aisk secret set *",        # 录入凭据是人的动作
    "aisk secret rm *",
    # main 只能由本人手工合并。2026-08-27 实测有 AI 会话自行完成 dev→main 并推送成功，
    # 而当时技能里写着「服务端保护，任何人不能直接 push」——那句话是假的。
    # 这里在命令执行前挡住；pre-push hook 是第二道；服务端保护才是第三道（需人工开）。
    "git push origin main*",
    "git push origin HEAD:main*",
    "git push * main*",
    "git merge dev*",           # 合并到 main 的常见前置
    "HBXH_ALLOW_MAIN_PUSH*",    # 人工放行开关，AI 不得使用。
                                # 注意：这是**环境变量**不是命令，套 wrapper 后得到
                                # `Bash(HBXH_ALLOW_MAIN_PUSH*)` 其实匹配不到任何命令。
                                # 继承自既有实现、不属于 WorkBuddy 这次改动；此处
                                # 如实保留原样，不在本次顺手改语义。
]


class PermissionResult:
    def __init__(self, tool, status, detail="", path=None, added=0):
        self.tool, self.status, self.detail = tool, status, detail
        self.path, self.added = path, added

    def line(self):
        icon = {"ok": "✅", "skip": "·", "unsupported": "⚠️"}.get(self.status, "?")
        loc = f" → {self.path}" if self.path else ""
        n = f"（新增 {self.added} 条）" if self.added else ""
        return f"  {icon} {self.tool}: {self.detail}{n}{loc}"


def _backup(path):
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".aisk-bak")
        if not bak.exists():
            shutil.copy2(path, bak)


def _merge_json_allow(path, wrap, dry_run=False):
    """把只读动词并进 JSON 配置的 permissions.allow。已存在的不重复加。"""
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return None, f"配置不是合法 JSON，跳过（{e}）"

    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    deny = perms.setdefault("deny", [])

    want_allow = [wrap(v) for v in READONLY_VERBS]
    want_deny = [wrap(v) for v in DENY_VERBS]
    added = [v for v in want_allow if v not in allow]
    added_deny = [v for v in want_deny if v not in deny]

    if not added and not added_deny:
        return 0, "白名单已是最新"

    if not dry_run:
        allow.extend(added)
        deny.extend(added_deny)
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup(path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return len(added) + len(added_deny), "只读动词已并入"


def apply_claude(dry_run=False):
    path = Path.home() / ".claude" / "settings.json"
    n, msg = _merge_json_allow(path, lambda v: f"Bash({v})", dry_run)
    if n is None:
        return PermissionResult("claude", "skip", msg, path)
    return PermissionResult("claude", "ok", msg, path, n)


def apply_cursor(dry_run=False):
    path = Path.home() / ".cursor" / "cli-config.json"
    n, msg = _merge_json_allow(path, lambda v: f"Shell({v})", dry_run)
    if n is None:
        return PermissionResult("cursor", "skip", msg, path)
    return PermissionResult("cursor", "ok", msg, path, n)


# Antigravity 没有命令前缀白名单，但 hooks.json 的 PreToolUse 能等效实现：
# 收到 run_command 时看命令前缀，只读动词直接 allow，写动词 deny，其余 ask。
# 官方限制：只支持 type:"command"，且 hooks 同步阻塞 agent 循环——
# 所以判定脚本必须是纯字符串匹配、零网络调用。
ANTIGRAVITY_GUARD = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity PreToolUse 守卫：aisk 只读动词免确认，写动词拒绝。

由 `aisk link` 生成，不要手工编辑——下次 link 会覆盖。

必须快：hooks 同步阻塞 agent 循环，所以这里只做纯字符串匹配，
不读配置、不连网络、不 import 内核。
"""
import json
import sys

ALLOW = %(allow)s
DENY = %(deny)s


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"decision": "ask"}))
        return
    call = payload.get("toolCall") or {}
    if call.get("name") not in ("run_command", "run_terminal_command"):
        print(json.dumps({"decision": "ask"}))
        return
    args = call.get("args") or {}
    cmd = (args.get("command") or args.get("cmd") or "").strip()

    for pat in DENY:
        if cmd.startswith(pat):
            print(json.dumps({"decision": "deny",
                              "reason": "aisk 写操作必须人工确认"}))
            return
    for pat in ALLOW:
        if cmd.startswith(pat):
            print(json.dumps({"decision": "allow"}))
            return
    print(json.dumps({"decision": "ask"}))


if __name__ == "__main__":
    main()
'''


def apply_antigravity(plugin_dir=None, dry_run=False):
    root = Path(plugin_dir) if plugin_dir else \
        Path.home() / ".gemini" / "config" / "plugins" / "agent-skills"
    hooks_json = root / "hooks.json"
    guard = root / "scripts" / "aisk_guard.py"

    # 前缀形式：去掉尾部通配，改成 startswith 判定
    allow = sorted({v.replace(" *", "").strip() for v in READONLY_VERBS})
    deny = sorted({v.replace(" *", "").strip() for v in DENY_VERBS})

    if dry_run:
        return PermissionResult("antigravity", "ok",
                                "将生成 hooks.json + 守卫脚本", hooks_json,
                                len(allow) + len(deny))

    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(ANTIGRAVITY_GUARD % {"allow": json.dumps(allow, ensure_ascii=False),
                                          "deny": json.dumps(deny, ensure_ascii=False)},
                     encoding="utf-8")
    guard.chmod(0o755)

    is_win = sys.platform.startswith("win")
    venv_py = Path.home() / ".aisk" / "venv" / ("Scripts" if is_win else "bin") / ("python.exe" if is_win else "python")
    python_cmd = str(venv_py) if venv_py.is_file() else sys.executable

    cfg = {
        "PreToolUse": [
            {
                "matcher": "run_command",
                "hooks": [
                    {"type": "command",
                     "command": f'"{python_cmd}" "{guard}"'}
                ],
            }
        ]
    }
    root.mkdir(parents=True, exist_ok=True)
    _backup(hooks_json)
    hooks_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return PermissionResult("antigravity", "ok", "PreToolUse 守卫已生成",
                            hooks_json, len(allow) + len(deny))


def apply_codex(dry_run=False):
    return PermissionResult(
        "codex", "unsupported",
        "本适配器尚未实现当前 Codex 宿主的权限配置生成，保留现有设置；"
        "是否支持命令规则以当前宿主工具与官方文档为准")


def _workbuddy_cli_path():
    """返回安装这份内核对应的 CLI；优先尊重启动器显式传入的路径。"""
    configured = os.environ.get("AISK_CLI")
    if configured:
        return configured
    root = os.environ.get("AISK_HUB_ROOT")
    if root:
        candidate = Path(root).expanduser() / "bin" / "aisk"
    else:
        candidate = Path(__file__).resolve().parents[2] / "bin" / "aisk"
    return str(candidate)


def _merge_workbuddy_guard_hook(path, dry_run=False):
    """把任务外高风险动作守卫并入 user 级 PreToolUse hooks。

    这是 C 方案能覆盖「任意目录原始 git push」的必要安装步骤：项目级 hooks 只存在于
    aisk 任务目录，离开任务目录就没有 `guards.py` 进程；user 级 hook 才能让
    `evaluate(..., task_root=None)` 在所有项目中看到这次调用。只追加自己的精确命令，
    不覆盖用户已有 hooks。"""
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return None, f"配置不是合法 JSON，跳过（{e}）"
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return None, "hooks 不是对象，跳过以免覆盖用户配置"
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        return None, "hooks.PreToolUse 不是数组，跳过以免覆盖用户配置"
    cli = shlex.quote(_workbuddy_cli_path())
    command = f"{cli} task guard --tool auto"
    for group in pre:
        if not isinstance(group, dict):
            continue
        for item in group.get("hooks") or []:
            if isinstance(item, dict) and item.get("type") == "command" and item.get("command") == command:
                return 0, "任务外高风险确认钩子已是最新"
    entry = {
        "matcher": ".*",
        "hooks": [{"type": "command", "timeout": 660,
                   "statusMessage": "确认任务外高风险动作", "command": command}],
    }
    if dry_run:
        return 1, "将新增任务外高风险确认钩子"
    pre.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1, "任务外高风险确认钩子已并入"


def apply_workbuddy(dry_run=False, tool="workbuddy"):
    """WorkBuddy 桌面版与 AI 版写的是**同一份** user 作用域配置，这是对的。

    **一手依据**：宿主把官方文档随应用发布在
    `/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/dist/web-ui/docs/cn/cli/`
    下的 `permissions.md` / `settings.md`。核实到的关键事实：

    1. 配置作用域共 4 层，落盘的是三层：user = `~/.codebuddy/settings.json`、
       project = `<repo>/.codebuddy/settings.json`、project-local =
       `<repo>/.codebuddy/settings.local.json`。user 作用域是唯一可脚本化、
       且**永远可信**（不受「项目目录是否被显式信任」影响）的一层。
    2. 权限对象形状与 claude 同构：`{"permissions": {"allow": [], "deny": []}}`。
    3. 规则语法 `Tool` / `Tool(specifier)`。Bash 支持精确匹配、`:*` 前缀与
       glob 三种；glob 里 `*` **可跨 `/`**。因此 `Bash(aisk env *)` 是合法
       glob、`Bash(aisk profile)` 是合法精确匹配。
    4. `deny` 永远优先；复合命令（`&&` `||` `;` `|`）下 allow 要求所有子命令
       都命中才放行——所以我们只把「只读动词」放进 allow 是安全的。
    5. 桌面版与 AI 版跑的是**同一个 codebuddy 引擎**，user 作用域文件同一份，
       所以两端的权限结论相同，写入天然幂等。**这不是 bug**：`tool` 参数只用
       于让 `aisk permit` 的结果行区分展示，不用于选择不同的落盘路径。

    **关于 `trustedDirectories` 的层级**（一手核实，勿凭猜）：
    `permissions.md` 写的是「`permissions.trustedDirectories` 配置项」，但
    `settings.md` 的字段表把 `trustedDirectories` 列在**顶层**（与 `model`
    同级），而 `permissions` 子表里只有 `additionalDirectories`。本机
    `~/.codebuddy/settings.json` 实测也是顶层 `trustedDirectories`。结论：
    `permissions.md` 那一行不准确，正确层级是顶层；本函数不写这两个键，
    仅记录以免后人照抄错文档。

    只增不改：合并进已有配置，用户自己的条目与无关配置键一字不动。
    """
    path = Path.home() / ".codebuddy" / "settings.json"
    n, msg = _merge_json_allow(path, lambda v: f"Bash({v})", dry_run)
    if n is None:
        return PermissionResult(tool, "skip", msg, path)
    hook_n, hook_msg = _merge_workbuddy_guard_hook(path, dry_run)
    if hook_n is None:
        return PermissionResult(tool, "skip", f"{msg}；{hook_msg}", path, n)
    return PermissionResult(tool, "ok", f"{msg}；{hook_msg}", path, n + hook_n)


HANDLERS = {
    "claude": apply_claude,
    "cursor": apply_cursor,
    "antigravity": apply_antigravity,
    "codex": apply_codex,
    "workbuddy": apply_workbuddy,
    "workbuddy-ai": lambda dry_run=False: apply_workbuddy(dry_run, "workbuddy-ai"),
}


def apply(tools, dry_run=False):
    out = []
    for t in tools:
        fn = HANDLERS.get(t)
        if not fn:
            out.append(PermissionResult(t, "skip", "未知端"))
            continue
        try:
            out.append(fn(dry_run=dry_run))
        except Exception as e:                      # noqa: BLE001
            out.append(PermissionResult(t, "skip", f"失败: {type(e).__name__}: {e}"))
    return out
