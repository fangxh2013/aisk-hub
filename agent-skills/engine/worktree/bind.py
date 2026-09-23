# -*- coding: utf-8 -*-
"""生成物：任务目录文件（须知、四端钩子、环境变量），以及机器级配置
（任务 git 配置、全局 includeIf、四端规则块、Codex 配置档、仓库本地 Claude 钩子）。

机器级配置全部走「计划 → 比对 → 备份后写入」：`aisk task bind` 默认只报告漂移，`--apply` 才写。
钩子命令写 Python 解释器与内核启动器的绝对路径——macOS 图形应用的 PATH 通常不含 aisk 所在目录。

四端钩子（任务目录内）：
| 工具 | 文件 | 事件 | 协议 |
|---|---|---|---|
| Claude | .claude/settings.json | PreToolUse、SessionStart、Stop、SessionEnd | tool_name/tool_input → hookSpecificOutput |
| Codex | .codex/hooks.json | 同上 | 同 Claude |
| WorkBuddy（内置 CodeBuddy 引擎） | .codebuddy/settings.json | 同上 | 同 Claude |
| WorkBuddy AI | .workbuddy-ai/settings.json | 同上 | 同 Claude 协议，独立工具归属 |
| Antigravity | .agents/hooks.json | PreToolUse、PreInvocation、Stop | toolCall → decision；PreInvocation 注入提示 |
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .. import profile as profile_mod
from . import actor, names
from .config import IS_WIN, WtConfig
from .registry import atomic_json, atomic_text, stamp

KERNEL = Path(__file__).resolve().parents[2]
TEMPLATES = KERNEL / "templates" / "worktree"
PLACEHOLDER = "（待填写"

GIT_BEGIN = names.BLOCK_BEGIN
GIT_END = names.BLOCK_END
LEGACY_GIT_BLOCKS = list(names.LEGACY_GIT_BLOCKS)
MD_BEGIN = names.MD_BEGIN
MD_END = names.MD_END
LEGACY_MD_BLOCKS = list(names.LEGACY_MD_BLOCKS)
TOML_BEGIN = names.BLOCK_BEGIN
TOML_END = names.BLOCK_END


# ------------------------------------------------------------------ 基础
def render(name, values):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{[a-z_]+\}\}", text)
    if left:
        raise RuntimeError(f"模板 {name} 有未替换的变量：{left}")
    return text


def stable_python():
    """钩子里写的解释器要经得起升级。Homebrew 把真实解释器放在带版本号的目录里（opt/python@3.14），
    升级后旧目录被删，写死它的钩子会一起失效；PATH 上不带版本号的 python3 入口若就是当前解释器，就写它。"""
    current = Path(sys.executable)
    for name in ("python3", "python"):
        found = shutil.which(name)
        try:
            if found and Path(found).resolve() == current.resolve():
                return found
        except OSError:
            continue
    return str(current)


def launcher_argv():
    """钩子里调用内核的方式：绝对路径的解释器 + 内核启动器。测试可用 AISK_LAUNCHER 覆盖。"""
    override = os.environ.get(names.ENV_LAUNCHER)
    if override:
        return shlex.split(override)
    return [stable_python(), str(KERNEL / "bin" / "aisk")]


def launcher_cmd(*args):
    argv = launcher_argv() + list(args)
    if IS_WIN:  # pragma: no cover
        # Windows 任务目录常含空格、括号或中文；手写只包空格的规则会在 cmd
        # 和 PowerShell 中产生不同解析结果。list2cmdline 是 Python/POSIX
        # 子进程约定的稳定 argv → Windows 命令行编码。
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def cli_hint():
    return names.CLI


def generated_dir():
    return profile_mod.runtime_root() / "generated" / "worktree"


def _strip_block(text, begin, end):
    return re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)


def _append_block(text, block):
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


# ------------------------------------------------------------------ 任务目录
def task_values(cfg: WtConfig, task):
    rows, repo_names, agents = [], [], []
    for alias, r in task["repos"].items():
        rows.append(f"| {alias} | {cfg.repo(alias).path.name} | `{r['branch']}` | `{r['path']}` |")
        repo_names.append(f"{alias}（{cfg.repo(alias).path.name}）")
        agents.append(f"`{alias}/AGENTS.md`")
    ports = task["ports"]
    base = task.get("base_task")
    base_desc = (f"叠放在任务 `{base}` 之上（父任务落地前本任务不能落地）" if base
                 else f"`{task['repos'][next(iter(task['repos']))]['base_ref']}`（{task.get('base_short', '')}）")
    td = Path(task["dir"])
    if cfg.os == "windows":
        posix_env = str(td / names.ENV_FILE).replace("\\", "/")
        env_hint = (f"Git Bash: source {shlex.quote(posix_env)}；"
                    f'CMD: call "{td / names.ENV_CMD}"；PowerShell: . ' +
                    "'" + str(td / names.ENV_PS1).replace("'", "''") + "'")
        sync_ref, sync_hint = f"mac/{cfg.integration}", "（先执行 `aisk task sync` 拉取 mac 侧最新集成分支）"
        if cfg.windows_integration:
            land_hint = (f"已开启 Windows 集成：ready 后可执行 `aisk task land {task['id']}`；"
                         "仅会写入档案声明的 integration_repo，确认框不可用或取消时保持不变。")
            main_roots = "、".join(f"`{cfg.integration_repo(a)}`" for a in task["repos"])
        else:
            land_hint = "ready 后由 mac 集成端落地；Windows 本机不能 land/promote。"
            main_roots = f"`{cfg.data_root / 'repos'}`"
    else:
        env_hint = f"source {td}/{names.ENV_FILE}"
        sync_ref, sync_hint = cfg.integration, ""
        land_hint = f"落地：`aisk task land {task['id']}` 你可以执行；改到敏感路径时会弹窗等操作者确认。"
        main_roots = "、".join(f"`{r.path}`" for r in cfg.repos.values())
    return {
        "task_id": task["id"], "name": task["name"], "title": task["title"],
        "goal": task.get("goal") or "（待填写）", "accept": task.get("accept") or "（待填写）",
        "task_dir": str(td), "repos_table": "\n".join(rows), "repos_list": "、".join(repo_names),
        "repo_agents_list": "、".join(agents), "base_desc": base_desc,
        "scope": "、".join(f"`{s}`" for s in task.get("scope") or []) or "未声明（aisk task status 会按实际改动预警重叠）",
        "web_port": ports["web"], "gw_port": ports["gateway"],
        "svc_ports": f"{ports['services_from']}–{ports['services_to']}", "env_hint": env_hint,
        "sync_ref": sync_ref, "sync_hint": sync_hint, "cli": cli_hint(), "land_hint": land_hint,
        "main_roots": main_roots, "anchors": str(cfg.anchors_dir), "hub": str(cfg.hub),
        "protected": "、".join(f"`{b}`" for b in cfg.protected), "idle": cfg.idle_minutes,
        "claimable": cfg.claimable_minutes, "message_hint": f"`{cfg.message_hint}`", "max_chars": cfg.max_msg_chars,
        "task_rules": "\n".join(f"- {rule}" for rule in cfg.task_rules),
        "handoff_sections": "\n\n".join(f"## {section}\n\n（待填写）" for section in cfg.handoff_sections),
    }


def write_env(cfg: WtConfig, task, java_home=""):
    td = Path(task["dir"])
    ports = task["ports"]
    env = {
        names.ENV_TASK: task["id"], names.ENV_TASK_DIR: str(td), "AISK_PROFILE": cfg.profile_name,
        "AISK_PORT_WEB": ports["web"], "AISK_PORT_GATEWAY": ports["gateway"],
        "AISK_PORT_SERVICES": f"{ports['services_from']}-{ports['services_to']}",
    }
    # 任务直接在隔离目录里启动时，也必须沿用当前工程的内核、私有 overlay、profile 和运行时，
    # 不能因 cwd 变成 task worktree 就回落到用户旧的全局配置。环境变量不完整正是 Windows
    # 从 hub 重新加载任务时找不到 profile 的根因之一；这里写入稳定解析结果，而不是只复制
    # 启动器恰好注入的变量。
    profile_dir = os.environ.get("AISK_PROFILE_DIR") or os.environ.get("AISKHUB_PROFILE_DIR")
    if not profile_dir and cfg.profile_path:
        profile_dir = str(cfg.profile_path.parent)
    private_root = os.environ.get("AISK_PRIVATE_ROOT") or os.environ.get("AISKHUB_PRIVATE_ROOT")
    if not private_root and profile_dir and Path(profile_dir).name == "profiles":
        private_root = str(Path(profile_dir).parent)
    runtime_root = os.environ.get("AISKHUB_RUNTIME_ROOT") or str(profile_mod.runtime_root())
    stable = {
        "AISK_HUB_ROOT": str(KERNEL.parent),
        "AISK_ENGINE_ROOT": str(KERNEL),
        "AISK_HOME": os.environ.get("AISK_HOME") or str(profile_mod.runtime_root().parent / ".aisk"),
        "AISK_PRIVATE_ROOT": private_root,
        "AISKHUB_PRIVATE_ROOT": private_root,
        "AISK_PROFILE_DIR": profile_dir,
        "AISKHUB_PROFILE_DIR": profile_dir,
        "AISKHUB_RUNTIME_ROOT": runtime_root,
    }
    for key, value in stable.items():
        if value:
            env[key] = value
    for key, value in cfg.task_env.items():
        if key in env:
            raise ValueError(f"task_env 不能覆盖内核变量 {key}")
        env[key] = value.replace("{task_id}", task["id"]).replace("{task_name}", task["name"])
    maven_args = cfg.raw.get("maven_args")
    if maven_args:
        env["MAVEN_ARGS"] = str(maven_args)
    if java_home:
        env["JAVA_HOME"] = java_home
    (td / names.META_DIR).mkdir(parents=True, exist_ok=True)
    atomic_text(td / names.ENV_FILE, "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()))
    # cmd 的首行先切 UTF-8；百分号必须转义，含双引号/换行/延迟扩展符的值改用 PowerShell。
    if any(any(c in str(v) for c in '\r\n"!') for v in env.values()):
        cmd_text = '@echo Use env.ps1 or the Git Bash env file for these values. 1>&2\r\n@exit /b 1\r\n'
    else:
        cmd_text = '@chcp 65001 >nul\r\n' + "".join(
            f'set "{k}={str(v).replace("%", "%%")}"\r\n' for k, v in env.items())
    (td / names.ENV_CMD).write_bytes(cmd_text.encode("utf-8"))
    (td / names.ENV_PS1).write_text(
        "".join("$env:" + k + " = '" + str(v).replace("'", "''") + "'\n" for k, v in env.items()), encoding="utf-8-sig")
    return env


def extra_guard_commands(cfg: WtConfig):
    """档案 worktrees.extra_guards：{python} 换成本机解释器，{repo:<别名>} 换成该仓库主工作区路径；
    引用的仓库文件不存在时跳过该条（与旧引擎"钩子文件不在就不挂"一致）。"""
    out = []
    for raw in cfg.extra_guards:
        cmd, missing = str(raw), False
        for alias in re.findall(r"\{repo:([A-Za-z0-9_-]+)\}", cmd):
            path = cfg.repo(alias).path
            cmd = cmd.replace("{repo:" + alias + "}", str(path))
        python = stable_python()
        cmd = cmd.replace("{python}", shlex.quote(python) if not IS_WIN else f'"{python}"')
        for token in shlex.split(cmd, posix=not IS_WIN):
            if token.endswith(".py") and not Path(token.strip('"')).exists():
                missing = True
        if not missing:
            out.append(cmd)
    return out


def guard_cmd(tool, task_dir):
    """守卫命令带上任务目录：工具上报的 cwd 由模型给，指到任务外就定位不到任务，守卫会整体失效。"""
    scope = ("--task-root", str(task_dir)) if task_dir else ()
    return launcher_cmd(names.SUBCOMMAND, "guard", "--tool", tool, *scope)


def claude_like_hooks(cfg: WtConfig, tool, shell_matcher, edit_matcher=None, task_dir=None):
    """Claude、Codex、WorkBuddy 共用的钩子结构：守卫挂在命令与编辑类工具上，会话事件交给 hook 子命令。"""
    guard = guard_cmd(tool, task_dir)
    hook = launcher_cmd(names.SUBCOMMAND, "hook", tool)
    shell_hooks = [{"type": "command", "timeout": 10, "command": c} for c in extra_guard_commands(cfg)]
    shell_hooks.append({"type": "command", "timeout": 10, "statusMessage": "校验任务边界与认领", "command": guard})
    pre = [{"matcher": shell_matcher, "hooks": shell_hooks}]
    if edit_matcher:
        pre.append({"matcher": edit_matcher, "hooks": [
            {"type": "command", "timeout": 10, "statusMessage": "校验任务边界与认领", "command": guard}]})
    session = [{"hooks": [{"type": "command", "timeout": 15, "command": hook}]}]
    return {"hooks": {"PreToolUse": pre, "SessionStart": session, "Stop": session, "SessionEnd": session}}


def claude_settings(cfg: WtConfig, task):
    settings = claude_like_hooks(cfg, "claude", "Bash", "Edit|Write|NotebookEdit|MultiEdit", task_dir=task["dir"])
    if cfg.os == "mac":
        first = cfg.repo(next(iter(task["repos"]))).path
        settings["autoMemoryDirectory"] = "~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", str(first)) + "/memory"
        deny = []
        for p in [r.path for r in cfg.repos.values()] + [cfg.anchors_dir, cfg.hub, cfg.state_dir]:
            deny += [f"Edit(/{p}/**)", f"Write(/{p}/**)"]
        deny += ["Bash(git push *)", "Bash(git stash *)", "Bash(git stash)", "Bash(git worktree *)",
                 "Bash(git update-ref *)"]
        settings["permissions"] = {"deny": deny}
    return settings


def codex_hooks(cfg: WtConfig, task_dir=None):
    return claude_like_hooks(cfg, "codex", "Bash|shell|exec_command|local_shell|apply_patch", task_dir=task_dir)


def codebuddy_settings(cfg: WtConfig, task_dir=None):
    """WorkBuddy 内置 CodeBuddy 引擎（项目级 .codebuddy/settings.json，协议与 Claude 相同）。
    工具名与 Claude 不完全一致，匹配全部工具，由守卫按输入判断是不是命令或写文件。

    这一份是**两个入口共用且唯一生效**的项目级设置：2026-09-19 隔离实测，桌面版与 AI 版
    的引擎都只读 `<项目根>/.codebuddy/settings.json`，不读 `.workbuddy-ai/settings.json`。
    一份文件只能写一个工具名，所以这里传哨兵 `auto`，由 guard / hook 在**运行期**
    按 `detect_tool()` 解析成真正在跑的那一端（桌面版 `workbuddy` / AI 版 `workbuddy-ai`）。
    写死任一个都会让另一端的会话事件与守卫以错误名义记账。"""
    return claude_like_hooks(cfg, actor.AUTO_TOOL, ".*", task_dir=task_dir)


def workbuddy_ai_settings(cfg: WtConfig, task_dir=None):
    """WorkBuddy AI 独立项目设置；不复用桌面端工具归属。

    保留生成是为了向前兼容（若宿主改为读该文件即可生效），但**当前引擎不读它**：
    见 `codebuddy_settings()` 的实测结论。写出来不影响任何行为，只是别把它当成
    「AI 端已有钩子」的证据——真正的钩子在 `.codebuddy/settings.json`。"""
    return claude_like_hooks(cfg, actor.AUTO_TOOL, ".*", task_dir=task_dir)


def antigravity_hooks(cfg: WtConfig, task_dir=None):
    """Antigravity 生命周期钩子（.agents/hooks.json）：守卫、调用模型前注入认领提示、每轮结束续心跳。"""
    guard = guard_cmd("antigravity", task_dir)
    hook = launcher_cmd(names.SUBCOMMAND, "hook", "antigravity", "--event")
    return {
        f"{names.ANTIGRAVITY_HOOK_PREFIX}-guard": {
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": guard, "timeout": 10}]}],
        },
        f"{names.ANTIGRAVITY_HOOK_PREFIX}-session": {
            "PreInvocation": [{"type": "command", "command": hook + " PreInvocation", "timeout": 15}],
            "Stop": [{"type": "command", "command": hook + " Stop", "timeout": 15}],
        },
    }


def seed_workbuddy_memory(cfg: WtConfig, task):
    alias = cfg.raw.get("workbuddy_memory_repo")
    if not alias or alias not in task["repos"] or cfg.os != "mac":
        return
    src = cfg.repo(alias).path / ".workbuddy" / "memory"
    if not src.is_dir():
        return
    dst = Path(task["dir"]) / ".workbuddy" / "memory"
    dst.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for f in sorted(src.glob("*.md")):
        shutil.copy2(f, dst / f.name)
        manifest[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    atomic_json(Path(task["dir"]) / names.META_DIR / "workbuddy-seed.json", manifest)


def write_task_files(cfg: WtConfig, task, java_home="", keep_notes=False):
    td = Path(task["dir"])
    td.mkdir(parents=True, exist_ok=True)
    vals = task_values(cfg, task)
    atomic_text(td / "AGENT-WORKTREE.md", render("AGENT-WORKTREE.md.tmpl", vals))
    for name in ("TASK.md", "PROGRESS.md", "HANDOFF.md"):
        if not (keep_notes and (td / name).exists()):
            atomic_text(td / name, render(name + ".tmpl", vals))
    atomic_text(td / "CLAUDE.md", render("CLAUDE.md.tmpl", vals))
    atomic_text(td / "AGENTS.md", render("AGENTS.md.tmpl", vals))
    atomic_text(td / "GEMINI.md", render("GEMINI.md.tmpl", vals))
    atomic_text(td / ".agents" / "rules" / names.AGENTS_RULE_FILE, render("agents-rule.md.tmpl", vals))
    atomic_json(td / ".claude" / "settings.json", claude_settings(cfg, task))
    atomic_json(td / ".codex" / "hooks.json", codex_hooks(cfg, td))
    atomic_json(td / ".codebuddy" / "settings.json", codebuddy_settings(cfg, td))
    atomic_json(td / ".workbuddy-ai" / "settings.json", workbuddy_ai_settings(cfg, td))
    atomic_json(td / ".agents" / "hooks.json", antigravity_hooks(cfg, td))
    write_env(cfg, task, java_home)
    atomic_json(td / names.TASK_META, {
        "id": task["id"], "name": task["name"], "title": task["title"], "root": str(td), "profile": cfg.profile_name,
        "repos": {a: r["path"] for a, r in task["repos"].items()},
        "profile_repos": {cfg.repo(a).profile_repo: r["path"] for a, r in task["repos"].items()},
    })
    if not keep_notes:
        seed_workbuddy_memory(cfg, task)


def handoff_incomplete(task_dir, required_sections=None):
    f = Path(task_dir) / "HANDOFF.md"
    if not f.exists():
        return True
    from .config import DEFAULT_HANDOFF_SECTIONS
    sections = re.split(r"(?m)^## ", f.read_text(encoding="utf-8"))[1:]
    bodies = {s.partition("\n")[0].strip(): s.partition("\n")[2].strip() for s in sections}
    return any(not bodies.get(title) or PLACEHOLDER in bodies[title]
               for title in (required_sections or DEFAULT_HANDOFF_SECTIONS))


# ------------------------------------------------------------------ 机器级配置
def global_gitconfig_path():
    if os.environ.get("GIT_CONFIG_GLOBAL"):
        return Path(os.environ["GIT_CONFIG_GLOBAL"])
    home = Path.home()
    if (home / ".gitconfig").exists() or not (home / ".config" / "git" / "config").exists():
        return home / ".gitconfig"
    return home / ".config" / "git" / "config"


def lane_gitconfig_text(cfg: WtConfig):
    """只对任务 worktree 生效的 git 配置（仓库本地已设的键会压过这里，所以只放仓库本地不会设置的键）。
    protocol.allow=never 让任务里的 push、fetch、ls-remote 对任何写法的地址都失败；清空凭据助手；远端推送地址指向空。"""
    lines = [f"# 由 {names.CLI} bind 生成：仅对任务 linked worktree 生效（含迁移期旧槽位）。",
             "[credential]", "\thelper =", "\tinteractive = never",
             "[protocol]", "\tallow = never"]
    for prefix in cfg.push_block_prefixes:
        lines += [f'[url "{names.PUSH_DISABLED_URL}/"]', f"\tpushInsteadOf = {prefix}"]
    section, key = names.GIT_MARK_KEY.split(".", 1)
    for remote in ("origin", "mac", "hub"):
        lines += [f'[remote "{remote}"]', f"\tpushurl = {names.PUSH_DISABLED_URL}"]
    lines += [f"[{section}]", f"\t{key} = true"]
    return "\n".join(lines) + "\n"


def include_block(cfg: WtConfig):
    lane = str(generated_dir() / "lane.gitconfig").replace("\\", "/")
    parts = [GIT_BEGIN, "# 必须留在文件末尾：空的 credential.helper 只能清掉排在它前面的凭据助手"]
    for alias in cfg.repo_order:
        repo = cfg.repo(alias)
        globs = [names.TASK_ADMIN_PREFIX + "*", *(p + "*" for p in names.LEGACY_TASK_ADMIN_PREFIXES),
                 *cfg.legacy_admin_globs]
        for pattern in dict.fromkeys(globs):
            if cfg.os == "windows":
                cond = f"gitdir/i:{str(repo.path).replace(chr(92), '/')}/worktrees/{pattern}"
            else:
                root = str(repo.path.resolve()).replace("\\", "/")
                match = "gitdir/i" if IS_WIN else "gitdir"
                cond = f"{match}:{root}/.git/worktrees/{pattern}"
            parts += [f'[includeIf "{cond}"]', f"\tpath = {lane}"]
    parts.append(GIT_END)
    return "\n".join(parts) + "\n"


def rules_block():
    return f"{MD_BEGIN}\n{(TEMPLATES / 'rules-block.md.tmpl').read_text(encoding='utf-8')}{MD_END}\n"


def codex_profile_block():
    return (f"{TOML_BEGIN}\n[profiles.{names.CODEX_PROFILE}]\nsandbox_mode = \"workspace-write\"\n"
            f"approval_policy = \"on-request\"\n{TOML_END}\n")


def _merged_claude_local(path: Path):
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    hook = launcher_cmd(names.SUBCOMMAND, "hook", "claude")
    hooks = data.setdefault("hooks", {})
    for event in ("WorktreeCreate", "WorktreeRemove"):
        hooks[event] = [{"hooks": [{"type": "command", "timeout": 60, "command": hook}]}]
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def plan(cfg: WtConfig):
    """返回 [(描述, 目标路径, 当前文本, 期望文本)]。"""
    items = []
    lane_path = generated_dir() / "lane.gitconfig"
    items.append(("任务 git 配置", lane_path, _read(lane_path), lane_gitconfig_text(cfg)))

    gpath = global_gitconfig_path()
    cur = _read(gpath)
    base = cur
    for b, e in LEGACY_GIT_BLOCKS + [(GIT_BEGIN, GIT_END)]:
        base = _strip_block(base, b, e)
    items.append(("全局 includeIf", gpath, cur, _append_block(base, include_block(cfg))))

    for f in cfg.rule_files:
        p = profile_mod._expand(f)
        if not p.parent.exists():
            continue
        cur = _read(p)
        base = cur
        for b, e in LEGACY_MD_BLOCKS + [(MD_BEGIN, MD_END)]:
            base = _strip_block(base, b, e)
        items.append((f"规则块 {f}", p, cur, _append_block(base, rules_block())))

    codex = Path.home() / ".codex" / "config.toml"
    if codex.exists():
        cur = _read(codex)
        base = _strip_block(cur, TOML_BEGIN, TOML_END)
        for b, e in LEGACY_GIT_BLOCKS:
            base = _strip_block(base, b, e)
        if re.search(rf"^\[profiles\.{re.escape(names.CODEX_PROFILE)}\]", base, re.M):
            items.append(("Codex 配置档", codex, cur, cur))  # 用户自有的任务配置档，不覆盖
        else:
            items.append(("Codex 配置档", codex, cur, _append_block(base, codex_profile_block())))

    if cfg.os == "mac":
        for alias in cfg.repo_order:
            p = cfg.repo(alias).path / ".claude" / "settings.local.json"
            if cfg.repo(alias).path.exists():
                items.append((f"{alias} 仓库本地 Claude 钩子", p, _read(p), _merged_claude_local(p)))
    return items


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


def apply_plan(items):
    """只写有漂移的项；写前把原文件备份到 ~/.aisk/backups/worktree/<时间>/。返回写入的描述列表。"""
    changed = [it for it in items if it[2] != it[3]]
    if not changed:
        return []
    backup = profile_mod.runtime_root() / "backups" / "worktree" / stamp()
    written = []
    for desc, path, cur, want in changed:
        path = Path(path)
        target = path.resolve() if path.is_symlink() else path  # dotfiles 软链：写穿到真实文件，不把软链替换掉
        mode = None
        if target.exists():
            dst = backup / re.sub(r"[^A-Za-z0-9._-]", "_", str(path).lstrip("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dst)
            mode = target.stat().st_mode
        atomic_text(target, want)
        if mode is not None:
            os.chmod(target, mode & 0o7777)
        written.append(desc)
    return written
