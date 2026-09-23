# -*- coding: utf-8 -*-
"""PreToolUse 守卫（入口 `aisk task guard --tool <工具>`；Claude、Codex、WorkBuddy、Antigravity 共用）。

两件事：
1. 越界：把 AI 限制在自己的任务目录里（主工作区、锚点、hub、登记簿、其他任务、内核与档案目录只读）；
2. 租约：别人持有中的任务不许改；没人认领的任务在第一次改动时自动为当前会话认领。

命令分析按 shell 语法做，而不是整串正则：拆开 `;`、`&&`、`|`，展开 `sh -c`、`bash -lc`、`eval`、`xargs`、
`env`/`sudo`/`timeout` 前缀、`$(...)` 与反引号、简单变量赋值，识别绝对路径的 git、`git-push` 这类连字符命令与
git 别名。它仍是启发式的第二道防线：真正的边界是任务 git 配置（禁止一切传输协议、清空凭据）、锚点与落地门禁。
任务目录内的守卫自身故障按读放行、写拒绝处理；任务目录外的高风险确认故障按未获确认拒绝，不能替人放行对外动作。

输入与输出按工具协议适配：
- Claude / Codex / WorkBuddy：tool_name + tool_input → hookSpecificOutput.permissionDecision
- Antigravity：toolCall{name,args} → {"decision": "deny", "reason": …}；放行输出 {}
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from .. import profile as profile_mod
from . import actor, config, gitops as git, model, names, registry, tasks
from .registry import LIVE_STATES, Registry, parse_iso

IS_WIN = sys.platform.startswith("win")
KERNEL = Path(__file__).resolve().parents[2]
HEARTBEAT_EVERY = 300
MAX_DEPTH = 6

CONFIG_QUERY_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
CONFIG_WRITE_FLAGS = {"--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section",
                      "--edit", "-e"}
CONFIG_OPTS_WITH_VALUE = {"--file", "-f", "--blob", "--type", "--default", "--comment"}
GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                      "--config-env", "--attr-source", "--list-cmds"}
DANGEROUS_CONFIG = ("protocol.", "credential", "url.", "remote.", "core.hookspath", "core.sshcommand", "core.askpass",
                    "core.gitproxy", "include.", "includeif.", "http.", "alias.", "safe.directory", "core.worktree",
                    "extensions.")
GIT_ENV_OVERRIDES = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_PARAMETERS",
                     "GIT_CONFIG_COUNT", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_ALLOW_PROTOCOL",
                     "GIT_PROTOCOL_FROM_USER", "GIT_EXEC_PATH", "GIT_NO_REPLACE_OBJECTS"}
GIT_TARGET_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"}
GIT_KNOWN = set("""add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore
check-mailmap check-ref-format checkout checkout-index cherry cherry-pick citool clean clone column commit
commit-graph commit-tree config count-objects credential describe diagnose diff diff-files diff-index diff-tree
difftool fast-export fast-import fetch fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo
format-patch fsck gc get-tar-commit-id grep gui hash-object help hook index-pack init instaweb interpret-trailers
log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-index merge-tree
mktag mktree multi-pack-index mv name-rev notes pack-objects pack-refs patch-id prune prune-packed pull push
range-diff read-tree rebase reflog remote repack replace request-pull rerere reset restore rev-list rev-parse
revert rm send-email shortlog show show-branch show-index show-ref sparse-checkout stage stash status
stripspace submodule switch symbolic-ref tag update-index update-ref update-server-info var verify-commit
verify-pack verify-tag version whatchanged worktree write-tree""".split())
GIT_MUTATING = {"add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "merge", "mv", "rebase", "reset",
                "restore", "revert", "rm", "switch", "tag", "stage", "notes"}
GIT_FORBIDDEN_ALWAYS = {
    "push": "任务内禁止 git push：交付走 aisk task ready → land → promote。",
    "update-ref": "任务内禁止 git update-ref。",
    "fetch": "任务内禁止 git fetch：由操作者 aisk task sync 集中抓取。",
    "pull": "任务内禁止 git pull：需要最新代码请在任务分支上 rebase 集成分支。",
    "gc": "任务内禁止 git gc：对象库由所有 worktree 共用。",
    "prune": "任务内禁止 git prune：对象库由所有 worktree 共用。",
    "repack": "任务内禁止 git repack：对象库由所有 worktree 共用。",
    "filter-branch": "任务内禁止 git filter-branch。",
    "replace": "任务内禁止 git replace。",
    "pack-refs": "任务内禁止 git pack-refs。",
}
TASK_OPERATOR_ONLY = {"promote", "init", "bind", "import-legacy", "legacy-shims"}
TASK_MUTATING = {"check", "commit", "ready", "land", "pause", "archive", "restack", "merge-commit", "revert", "release",
                 "harvest"}
WRITE_PROGS = {"rm", "rmdir", "mv", "touch", "mkdir", "chmod", "chown", "truncate", "tee", "patch", "ln", "unzip", "tar",
               "dd", "shred", "del", "rd", "move", "copy", "install", "rsync", "cp", "ditto", "robocopy", "xcopy", "scp",
               "unlink", "trash"}
COPY_PROGS = {"cp", "rsync", "install", "scp", "ditto", "robocopy", "xcopy", "copy"}
WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "exec", "time", "nohup", "nice", "stdbuf", "caffeinate",
            "timeout", "xargs", "unbuffer", "watch", "chronic"}
WRAPPER_OPTS_WITH_VALUE = {"-u", "-n", "-I", "-L", "-P", "-s", "-d", "-E", "-C", "-S", "-g", "--signal", "-k"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd"}
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "osascript", "deno", "bun", "php"}
SHELL_TOOL_NAMES = {"bash", "shell", "exec_command", "local_shell", "run_command", "run_terminal_cmd",
                    "execute_command", "terminal", "powershell"}
WRITE_TOOL_NAMES = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "write_to_file", "replace_file_content",
                    "multi_replace_file_content", "delete_directory", "edit_notebook", "file_change"}
WRITE_TOOL_RE = re.compile(r"write|edit|create|delete|remove|move|rename|patch|replace|insert|save", re.I)
# 写文件类工具名（用于「看着像写文件却给不出路径」的拒绝），排除写待办/记忆/计划这类不落盘到任务外的调用
FILE_WRITE_RE = re.compile(r"file|patch|notebook|dir", re.I)
NON_FILE_WRITE_RE = re.compile(r"todo|memory|plan|diagram|comment|issue|ticket|task|note|message", re.I)
# 解释器一行代码里的写操作特征：命中才按写处理，只读的 -c 不误拦
INTERPRETER_WRITE_RE = re.compile(
    r"""open\s*\([^)]*['"][wax]|\bwrite|unlink|\bremove\b|rename|replace|mkdir|rmdir|truncate|chmod|chown|"""
    r"""shutil|copy|move|writeFile|appendFile|createWriteStream|os\.system|subprocess|Popen|system\s*\(""", re.I)
GUARD_BROKEN = "任务守卫自身异常（{detail}）：读可以继续，写入、提交、推送一律拒绝。修复守卫后再继续。"
MANUAL_ONLY_BRANCHES = {"main", "master"}
MAIN_BRANCH_WRITE_COMMANDS = GIT_MUTATING | {"branch", "push", "pull", "update-ref", "stash", "submodule"}
MAIN_BRANCH_TARGET_COMMANDS = {"checkout", "switch", "merge", "rebase", "cherry-pick", "reset", "restore"}

# 任务之外只对受保护分支的写操作询问人。个人集成分支（例如 fxh → fxh-dev）
# 是日常开发闭环，默认放行；dev/main/master 等受保护分支和 merge dev 必须确认。
# 任务内的 push 仍沿用 GIT_FORBIDDEN_ALWAYS（无条件拒绝），不能被这个「任务外确认」分支绕开。
OUTSIDE_CONFIRM_TOOLS = frozenset({"codex", "claude", "antigravity", "workbuddy", "workbuddy-ai", "cursor"})
OUTSIDE_CONFIRM_ACTIONS = {"push": "git推送", "merge": "git合并"}
DEFAULT_PROTECTED_PUSH_BRANCHES = frozenset({"dev", "main", "master"})
LOCAL_REMOTE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_REMOTE_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".corp")


class Deny(Exception):
    pass


# ------------------------------------------------------------------ 路径
def norm(p):
    s = os.path.realpath(os.path.expanduser(str(p))).replace("\\", "/")
    return s.lower() if IS_WIN else s


def within(path, root):
    path, root = norm(path), norm(root).rstrip("/")
    return path == root or path.startswith(root + "/")


def find_task_root(start):
    if not start:
        return None
    try:
        p = Path(start).resolve()
    except OSError:
        return None
    for cand in [p, *p.parents]:
        if names.task_meta_file(cand):
            return cand
    return None


def protected_roots(cfg):
    roots = [cfg.anchors_dir, cfg.hub, cfg.state_dir, cfg.tasks_dir]
    if cfg.os == "windows":
        roots.append(cfg.data_root / "repos")
    else:
        roots += [r.path for r in cfg.repos.values()]
    if cfg.legacy_root:
        roots.append(cfg.legacy_root / "lanes")
    roots += [KERNEL, profile_mod.PROFILE_DIR.parent]
    roots += [profile_mod._expand(p) for p in (cfg.raw.get("guard_protected") or [])]
    return [str(r) for r in roots]


def writable_roots(cfg, task_root):
    roots = [str(task_root), tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders",
             os.path.expanduser("~/.claude/projects"), os.path.expanduser("~/.claude/plans")]
    if IS_WIN:
        roots += [os.environ.get("TEMP", ""), os.environ.get("TMP", "")]
    roots += [str(profile_mod._expand(p)) for p in (cfg.raw.get("guard_writable") or [])]
    return [r for r in roots if r]


def is_protected(path, task_root, roots):
    if within(path, str(task_root)):
        return False
    return any(within(path, r) for r in roots)


def unmounted_repo_hint(cfg, path, task_root):
    """目标落在某个档案仓库的主工作区、而这个仓库没挂在本任务里时，给出能直接执行的下一步。
    只说「不许改」会让模型要么放弃、要么去越界——2026-09-16 现场两种都出现过。"""
    meta_file = names.task_meta_file(Path(task_root))
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file else {}
    except (OSError, ValueError):
        meta = {}
    mounted = set(meta.get("repos") or {})
    for alias in cfg.repo_order:
        try:
            repo = cfg.repo(alias).path
        except Exception:  # noqa: BLE001
            continue
        if within(str(path), str(repo)) and alias not in mounted:
            tid = meta.get("id") or "<任务号>"
            return (f" 仓库 {alias} 没有挂在本任务里：先 {names.CLI} add-repo {tid} {alias}，"
                    f"再改任务目录下的 {alias}/。")
    return ""


def check_edit(cfg, target, cwd, task_root):
    p = Path(target)
    if not p.is_absolute():
        p = Path(cwd) / p
    if is_protected(str(p), task_root, protected_roots(cfg)):
        raise Deny(f"目标 {p} 属于任务之外的受保护目录。{unmounted_repo_hint(cfg, p, task_root)}")
    if not any(within(str(p), r) for r in writable_roots(cfg, task_root)):
        raise Deny(f"只能修改本任务目录 {task_root} 内的文件，目标 {p} 在任务之外。"
                   f"{unmounted_repo_hint(cfg, p, task_root)}")


# ------------------------------------------------------------------ 输入归一
def strip_uri(value):
    value = str(value)
    if value.startswith("file://"):
        value = unquote(value[len("file://"):])
        if IS_WIN and re.match(r"^/[A-Za-z]:", value):
            value = value[1:]
    return value


def patch_paths(text):
    return [m.group(1).strip() for m in
            re.finditer(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", str(text), re.M)]


def normalize(payload):
    """把各工具的 PreToolUse 输入归一为 {name, cwd, command, paths, write, session_payload}。"""
    if isinstance(payload.get("toolCall"), dict):
        call = payload["toolCall"]
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        workspaces = payload.get("workspacePaths") or []
        cwd = args.get("Cwd") or args.get("cwd") or (workspaces[0] if workspaces else os.getcwd())
        command = args.get("CommandLine") or args.get("commandLine") or args.get("command")
        paths = [strip_uri(v) for k, v in args.items()
                 if isinstance(v, str) and re.search(r"(file|path|uri|directory)$", k, re.I)]
    else:
        name = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input") or {}
        cwd = payload.get("cwd") or ti.get("workdir") or os.getcwd()
        command = ti.get("command") or ti.get("cmd")
        paths = [str(ti[k]) for k in ("file_path", "notebook_path", "path", "filePath", "target_file")
                 if isinstance(ti.get(k), str)]
        if name.lower() == "apply_patch":
            paths += patch_paths(ti.get("patch") or ti.get("input") or command or "")
            command = None
    if isinstance(command, list):
        command = " ".join(shlex.quote(str(c)) for c in command)
    lower = name.lower()
    write = bool(paths) and (lower in WRITE_TOOL_NAMES or bool(WRITE_TOOL_RE.search(lower)))
    shell = bool(command) and (lower in SHELL_TOOL_NAMES or not paths)
    # 各端的参数字段随版本变化：工具名看着在写文件却给不出路径时，守卫无从判断越界，只能拒绝
    file_write = lower in WRITE_TOOL_NAMES or bool(WRITE_TOOL_RE.search(lower) and FILE_WRITE_RE.search(lower))
    unresolved = bool(lower) and file_write and not paths and not command and not NON_FILE_WRITE_RE.search(lower)
    session_id = payload.get("session_id") or payload.get("conversationId") or ""
    task_id = payload.get("task_id") or payload.get("taskId") or ""
    return {"name": name, "cwd": str(cwd), "command": str(command) if shell else "", "paths": paths, "write": write,
            "unresolved_write": unresolved, "session_id": str(session_id), "task_id": str(task_id)}


def mutating_request(payload):
    """守卫出错时用来判断这次调用会不会写。判断不了就当成会写——出错时宁可拦住写，也不能默认放行。"""
    try:
        n = normalize(payload)
    except Exception:  # noqa: BLE001
        return True
    return bool(n["write"] or n["command"] or n["unresolved_write"] or WRITE_TOOL_RE.search(n["name"] or ""))


# ------------------------------------------------------------------ shell 分析
class ShellContext:
    def __init__(self, cfg, cwd, task_root):
        self.cfg = cfg
        self.cwd = cwd
        self.task_root = task_root
        self.roots = protected_roots(cfg)
        self.protected_branches = set(cfg.protected)
        self.mutating = False
        self.vars = {}


class MainGuardShellContext:
    """Minimal shell-analysis context used outside task worktrees for main protection."""

    def __init__(self, cwd):
        self.cfg = SimpleNamespace(forbidden_commands={})
        self.cwd = str(cwd)
        self.task_root = None
        self.roots = []
        self.protected_branches = set()
        self.mutating = False
        self.vars = {}
        self.main_guard_only = True


def is_main_branch_ref(value):
    """Recognize exact main refs, including common remote refs and push refspecs."""
    ref = str(value or "").strip().strip("'\"")
    if ":" in ref:
        ref = ref.rsplit(":", 1)[-1]
    ref = ref.lstrip("+").lower()
    prefixes = ("refs/heads/", "refs/remotes/", "remotes/origin/", "origin/", "mac/")
    while True:
        prefix = next((p for p in prefixes if ref.startswith(p)), None)
        if not prefix:
            break
        ref = ref[len(prefix):]
    return ref == "main"


def main_repo_for_path(path):
    """Return the repository root when path belongs to a checkout currently on main."""
    target = Path(str(path or os.getcwd())).expanduser()
    try:
        root = profile_mod.repo_root(target)
        if root and git.current_branch(root) == "main":
            return root
    except (OSError, ValueError):
        return None
    return None


def deny_main_write_path(path):
    root = main_repo_for_path(path)
    if root:
        raise Deny(f"Aisk 守卫禁止 AI 修改 main 分支工作区：{root}；该限制不可配置或确认解除。")


def direct_repo_context(normalized):
    """Resolve one configured direct checkout from the host tool's cwd or paths."""
    candidates = [normalized.get("cwd") or os.getcwd(), *(normalized.get("paths") or [])]
    if normalized.get("command"):
        try:
            candidates.extend(outside_cwd_candidates(normalized["command"], normalized.get("cwd") or os.getcwd()))
            tokens = tokenize(normalized["command"])
            for index, token in enumerate(tokens):
                value = None
                if token in ("--git-dir", "--work-tree") and index + 1 < len(tokens):
                    value = tokens[index + 1]
                elif token.startswith(("--git-dir=", "--work-tree=")) or token.startswith(("GIT_DIR=", "GIT_WORK_TREE=")):
                    value = token.split("=", 1)[1]
                if value:
                    candidate = Path(os.path.expanduser(value.strip("'\"")))
                    if not candidate.is_absolute():
                        candidate = Path(normalized.get("cwd") or os.getcwd()) / candidate
                    candidates.append(candidate)
        except Exception:  # noqa: BLE001  malformed shell is handled fail-closed once a direct repo is identified
            pass
    for candidate in candidates:
        try:
            root = profile_mod.repo_root(candidate)
            if root is None:
                continue
            cfg, _why = config.load_config(start=candidate)
            matches = [rc for rc in cfg.repos.values()
                       if rc.workspace_mode == "direct" and rc.path.resolve() == root.resolve()]
            if len(matches) == 1:
                return cfg, matches[0], root.resolve()
            if len(matches) > 1:
                raise Deny("同一普通检出匹配多个 direct 仓库配置，拒绝写入。")
        except Deny:
            raise
        except Exception:  # noqa: BLE001  unrelated projects continue through normal guard routing
            continue
    return None


def _direct_task_for_repo(cfg, rc, repo):
    reg = Registry(cfg)
    matches = []
    for task in reg.all(include_hub=False):
        if not task.get("direct_checkout") or task.get("state") not in LIVE_STATES or task.get("os") != cfg.os:
            continue
        rows = task.get("repos") or {}
        if len(rows) != 1 or rc.alias not in rows:
            continue
        try:
            row_path = Path(rows[rc.alias].get("path", "")).resolve()
        except (OSError, TypeError):
            continue
        if row_path == repo:
            matches.append(task)
    if len(matches) != 1:
        if not matches:
            raise Deny(f"{rc.alias} 是 direct 仓库；写入必须由唯一活跃 direct 任务持有 lease，先运行 aisk task direct-new。")
        raise Deny(f"{rc.alias} 有多个活跃 direct 任务匹配同一普通检出，拒绝写入。")
    return matches[0]


def _direct_path_in_scope(raw, cwd, repo, allowed_paths):
    from .. import direct_checkout

    target = Path(strip_uri(raw)).expanduser()
    if not target.is_absolute():
        target = Path(cwd) / target
    target = target.resolve(strict=False)
    try:
        rel = target.relative_to(repo).as_posix()
    except ValueError:
        raise Deny(f"direct 任务只能写入其声明的仓库范围内：{target}")
    if not rel or rel == ".git" or rel.startswith(".git/") or not direct_checkout._within_scope(rel, allowed_paths):
        raise Deny(f"direct 任务未声明此写入路径：{rel or '.'}")


def _direct_shell_is_safe_read(command, cwd, task_id=None):
    """Permit narrow read-only shell commands; all shell writes use direct-finish or file tools."""
    try:
        tokens = tokenize(command)
    except ValueError:
        return False
    segments = list(split_simple(tokens))
    if len(segments) != 1 or not segments[0]:
        return False
    argv = segments[0]
    prog = prog_name(argv[0], set())
    if prog in ("aisk", "xw"):
        rest = list(argv[1:])
        if rest[:1] == [names.SUBCOMMAND]:
            rest = rest[1:]
        return bool(task_id and len(rest) >= 2 and rest[0] == "direct-finish" and rest[1] == task_id)
    if prog == "git":
        argv = list(argv[1:])
        if argv[:1] == ["-C"]:
            argv = argv[2:]
        if not argv:
            return False
        sub = argv[0]
        if sub in {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "describe"}:
            return not any(x in argv for x in (">", ">>", "<", "|"))
        if sub == "branch":
            return any(x in argv for x in ("--show-current", "--list", "-l", "-a", "-r", "--all", "-v", "-vv"))
        if sub == "remote":
            return len(argv) > 1 and argv[1] in {"show", "get-url", "-v"}
        if sub == "config":
            return len(argv) > 1 and (argv[1] in CONFIG_QUERY_FLAGS or argv[1] in ("get", "get-all", "get-regexp", "list"))
        return False
    if prog == "find":
        return not any(x in argv for x in ("-delete", "-exec", "-execdir", ">", ">>", "|"))
    return prog in {"pwd", "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "rg", "grep"} \
        and not any(x in argv for x in (">", ">>", "<", "|"))


def direct_checkout_guard(normalized):
    """Guard host writes aimed at direct repositories outside task worktrees.

    File editors are permitted only for paths inside the active task's declared
    scope while its persistent lease and clean branch/HEAD baseline still match.
    Shell writes are routed to direct-finish or denied because their full write
    set cannot be proven before execution.
    """
    if not (normalized.get("write") or normalized.get("command") or normalized.get("unresolved_write")):
        return None
    try:
        context = direct_repo_context(normalized)
    except Deny as error:
        return str(error)
    if context is None:
        return None
    cfg, rc, repo = context
    if normalized.get("command"):
        if _direct_shell_is_safe_read(normalized["command"], normalized.get("cwd") or os.getcwd()):
            return None
    try:
        task = _direct_task_for_repo(cfg, rc, repo)
        if normalized.get("command"):
            if _direct_shell_is_safe_read(normalized["command"], normalized.get("cwd") or os.getcwd(), task["id"]):
                return None
            raise Deny("direct 任务不允许普通 shell 写入；请使用声明范围内的文件编辑工具，完成时运行 aisk task direct-finish。")
        from .. import direct_checkout
        row = task["repos"][rc.alias]
        baseline = direct_checkout.RepoBaseline.from_dict(row.get("direct_baseline"))
        expected = str(rc.automatic.get("commit_branch") or "")
        if not expected or expected.lower() == "main" or baseline.branch != expected or row.get("branch") != expected:
            raise Deny(f"{rc.alias} direct 任务的 commit_branch 基线无效或命中 main，拒绝写入。")
        if normalized.get("unresolved_write"):
            raise Deny("direct 仓库写入工具没有提供可核对的目标路径，拒绝写入。")
        lease = direct_checkout.acquire_repo_lease(repo, owner=task["id"], runtime_root=cfg.data_root)
        try:
            validation = direct_checkout.validate_task_changes(repo, baseline, lease=lease)
            validation.raise_if_invalid()
            if validation.current_branch != expected:
                raise Deny(f"direct 仓库当前分支为 {validation.current_branch}，要求 {expected}。")
            for raw in normalized.get("paths") or []:
                _direct_path_in_scope(raw, normalized.get("cwd") or os.getcwd(), repo, baseline.allowed_paths)
        finally:
            lease.close()
    except Deny as error:
        return str(error)
    except Exception as error:  # noqa: BLE001  direct guard fails closed on lease/config/state errors
        return f"direct 仓库写守卫失败，已拒绝写入：{type(error).__name__}: {str(error)[:180]}"
    return None


def check_main_git_write(ctx, sub, rest, positional, cwd):
    """Reject writes on main and explicit writes targeting main, regardless of profile policy."""
    current = git.current_branch(cwd) if cwd else None
    if current == "main" and sub in MAIN_BRANCH_WRITE_COMMANDS:
        raise Deny("Aisk 守卫禁止 AI 在 main 分支执行写操作；该限制不可配置或确认解除。")

    refs = []
    if sub in MAIN_BRANCH_TARGET_COMMANDS:
        refs = positional
    elif sub == "branch":
        branch_mutation_flags = {"-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move",
                                "-c", "-C", "--copy"}
        branch_read_flags = {"--list", "-l", "--show-current", "--contains", "--merged", "--no-merged",
                             "--points-at", "--format", "-v", "-vv", "-a", "-r", "--all", "--remotes"}
        if any(flag in rest for flag in branch_mutation_flags) or not any(flag in rest for flag in branch_read_flags):
            refs = positional
    elif sub == "update-ref":
        if "--stdin" in rest:
            raise Deny("Aisk 守卫无法无歧义核对 git update-ref --stdin 的目标；为保护 main，已拒绝该写入。")
        refs = positional[:1]
    elif sub == "push":
        if any(option in rest for option in ("--all", "-a", "--branches", "--mirror")):
            raise Deny("Aisk 守卫禁止 git push --all/--mirror 等批量引用推送；无法保证 main 不被写入。")
        refs = [value for value in positional if ":" in value or is_main_branch_ref(value)]
    elif sub == "worktree" and positional and positional[0] == "add":
        refs = positional[1:]
    if any(is_main_branch_ref(value) for value in refs):
        raise Deny("Aisk 守卫禁止 AI 修改 main 分支引用；该限制不可配置或确认解除。")


def expand_home(text):
    home = os.path.expanduser("~")
    return re.sub(r"\$\{HOME\}|\$HOME|(?<![\w/])~(?=/|\s|$)", lambda m: home, text)


def strip_heredocs(text):
    """heredoc 正文是数据不是命令（常见于写文档），分析前去掉。"""
    return re.sub(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)",
                  lambda m: m.group(0).split("\n", 1)[0], text, flags=re.S)


def extract_substitutions(text):
    subs = []

    def repl(m):
        subs.append(m.group(1))
        return f" __AISK_SUBST_{len(subs) - 1}__ "

    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\$\(([^()]*)\)", repl, text)
        text = re.sub(r"`([^`]*)`", repl, text)
    return text, subs


def tokenize(text):
    lexer = shlex.shlex(text.replace("\r", " ").replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def split_simple(tokens):
    current = []
    for tok in tokens:
        if tok and set(tok) <= set(";&|()"):
            if current:
                yield current
            current = []
            continue
        current.append(tok)
    if current:
        yield current


def prog_name(token, subs_to_git):
    token = token.replace("\\", "/").strip("'\"")
    match = re.fullmatch(r"__AISK_SUBST_(\d+)__", token)
    if match:
        return "git" if int(match.group(1)) in subs_to_git else ""
    base = token.rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith((".exe", ".cmd", ".bat")) else base


def resolve_path(ctx, token):
    """把路径参数还原成绝对路径。相对路径同样要还原：任务目录里的符号链接可以指向主仓库，
    「不含 .. 的相对路径一定落在任务目录内」这个假设对符号链接不成立（is_protected 会走 realpath）。"""
    token = token.strip("'\"").rstrip(",")
    if not token:
        return None
    return os.path.normpath(os.path.join(ctx.cwd, os.path.expanduser(token)))


def deny_if_protected(ctx, token, message):
    target = resolve_path(ctx, token)
    if getattr(ctx, "main_guard_only", False):
        if target:
            deny_main_write_path(target)
        return
    if target and is_protected(target, ctx.task_root, ctx.roots):
        raise Deny(f"{message}：{token}{unmounted_repo_hint(ctx.cfg, target, ctx.task_root)}")


def analyze_text(ctx, text, depth=0):
    if depth > MAX_DEPTH:
        raise Deny("命令嵌套过深，无法确认安全，请拆开执行。")
    text = expand_home(strip_heredocs(text))
    for pattern, reason in ctx.cfg.forbidden_commands.items():
        if re.search(pattern, text):
            raise Deny(reason)
    text, subs = extract_substitutions(text)
    subs_to_git = set()
    for index, inner in enumerate(subs):
        analyze_text(ctx, inner, depth + 1)
        try:
            if "git" in [prog_name(t, set()) for t in tokenize(inner)]:
                subs_to_git.add(index)
        except ValueError:
            pass
    try:
        tokens = tokenize(text)
    except ValueError:
        if getattr(ctx, "main_guard_only", False) and main_repo_for_path(ctx.cwd):
            raise Deny("main 分支命令无法安全解析，Aisk 守卫已按 fail-closed 拒绝。")
        conservative_scan(ctx, text)
        return
    for argv in split_simple(tokens):
        analyze_command(ctx, argv, subs_to_git, depth)


def conservative_scan(ctx, text):
    """shell 语法都解析不了（引号不配对等）时的兜底：出现 git 危险子命令就拒绝。"""
    if re.search(r"\bgit(?:\.exe)?\b[^;&|\n]*\b(push|stash|worktree|update-ref|fetch|pull)\b", text):
        raise Deny("命令无法解析且包含危险的 git 子命令，请改写成普通命令。")


def env_assignment(ctx, name, value):
    if getattr(ctx, "main_guard_only", False):
        ctx.vars[name] = value
        return
    if name in GIT_ENV_OVERRIDES:
        raise Deny(f"任务内禁止通过环境变量 {name} 改写 git 配置或传输方式。")
    if name in GIT_TARGET_ENV:
        target = os.path.normpath(os.path.join(ctx.cwd, os.path.expanduser(value.strip("'\""))))
        temp_roots = ("/tmp", "/private/tmp", tempfile.gettempdir())
        if not within(target, str(ctx.task_root)) and not any(within(target, r) for r in temp_roots):
            raise Deny(f"任务内禁止用 {name} 把 git 指向任务之外：{value}")
    ctx.vars[name] = value


def substitute_vars(ctx, token):
    return re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: ctx.vars.get(m.group(1), m.group(0)), token)


def analyze_command(ctx, argv, subs_to_git, depth):
    argv = [substitute_vars(ctx, t) for t in argv]
    # 重定向：记录写入目标并从参数里去掉
    clean, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok in (">", ">>", ">|", "&>", "&>>", "<", "<<", "<<-", "<<<", "<>"):
            target = argv[i + 1] if i + 1 < len(argv) else ""
            if ">" in tok and target and target != "/dev/null" and not target.startswith("&"):
                if clean and re.fullmatch(r"\d", clean[-1]):
                    clean.pop()
                ctx.mutating = True
                deny_if_protected(ctx, target, "任务内禁止重定向写入任务之外的受保护路径")
            i += 2
            continue
        if tok == "&" and clean and clean[-1].isdigit():
            i += 1
            continue
        clean.append(tok)
        i += 1
    argv = clean
    i = 0
    while i < len(argv):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", argv[i])
        if match:
            env_assignment(ctx, match.group(1), match.group(2))
            i += 1
            continue
        base = prog_name(argv[i], subs_to_git)
        if base in WRAPPERS:
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or (base == "timeout" and re.match(r"^\d", argv[i]))):
                i += 2 if argv[i] in WRAPPER_OPTS_WITH_VALUE else 1
            continue
        break
    argv = argv[i:]
    if not argv:
        return
    prog = prog_name(argv[0], subs_to_git)
    args = argv[1:]
    if prog in ("export", "declare", "typeset", "set"):
        for a in args:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a)
            if match:
                env_assignment(ctx, match.group(1), match.group(2))
        return
    if prog == "eval":
        analyze_text(ctx, " ".join(args), depth + 1)
        return
    if prog in SHELLS:
        script = shell_script(prog, args)
        if script is not None:
            analyze_text(ctx, script, depth + 1)
        return
    if prog in INTERPRETERS and any(a in ("-c", "-e", "-E") for a in args):
        code = " ".join(args)
        conservative_scan(ctx, code)
        if INTERPRETER_WRITE_RE.search(code):
            ctx.mutating = True
            # 绝对路径 + 所有字符串字面量：相对路径也要查，任务目录里的符号链接能指到任务外
            targets = re.findall(r"""(?:[A-Za-z]:[\\/]|~/|/)[^\s'"()\]},;:]+""", code)
            targets += re.findall(r"""['"]([^'"\n]{1,200})['"]""", code)
            for token in targets:
                deny_if_protected(ctx, token, "任务内禁止用解释器写任务之外的受保护路径")
        return
    if prog == "git" or prog.startswith("git-"):
        check_git(ctx, prog, args, depth)
        return
    if prog in ("aisk", "xw"):
        check_task_cli(ctx, prog, args)
        return
    check_paths(ctx, prog, argv)


def shell_script(prog, args):
    for index, a in enumerate(args):
        if prog in ("pwsh", "powershell") and a.lower() in ("-command", "-c"):
            return " ".join(args[index + 1:])
        if prog == "cmd" and a.lower() in ("/c", "/k"):
            return " ".join(args[index + 1:])
        if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a) and index + 1 < len(args):
            return args[index + 1]
    return None


def check_task_cli(ctx, prog, args):
    rest = list(args)
    if prog == "aisk":
        while rest and rest[0].startswith("--profile"):
            rest = rest[2:] if rest[0] == "--profile" else rest[1:]
        if not rest or rest[0] not in (names.SUBCOMMAND, "wt"):
            return
        rest = rest[1:]
    while rest and rest[0].startswith("--profile"):
        rest = rest[2:] if rest[0] == "--profile" else rest[1:]
    sub = rest[0] if rest else ""
    if sub in TASK_OPERATOR_ONLY:
        raise Deny(f"{names.CLI} {sub} 由操作者在任务目录之外执行。")
    if sub == "doctor" and any(a in ("--fix", "--ack-refs") for a in rest[1:]):
        raise Deny(f"{names.CLI} doctor --fix / --ack-refs 由操作者执行。")
    if sub in TASK_MUTATING:
        ctx.mutating = True


def check_git(ctx, prog, args, depth):
    opts, i = [], 0
    git_cwd = ctx.cwd
    if prog.startswith("git-"):
        sub, rest = prog[4:], list(args)
    else:
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            if a in GIT_OPT_WITH_VALUE:
                opts.append((a, args[i + 1] if i + 1 < len(args) else ""))
                i += 2
                continue
            key = a.split("=", 1)[0]
            if "=" in a and key in GIT_OPT_WITH_VALUE:
                opts.append((key, a.split("=", 1)[1]))
            elif a.startswith("-c") and len(a) > 2:
                opts.append(("-c", a[2:]))
            elif a.startswith("-C") and len(a) > 2:
                opts.append(("-C", a[2:]))
            else:
                opts.append((a, None))
            i += 1
        sub, rest = (args[i] if i < len(args) else ""), list(args[i + 1:])
    for key, value in opts:
        if getattr(ctx, "main_guard_only", False):
            if key == "-C" and value:
                git_cwd = os.path.normpath(os.path.join(ctx.cwd, os.path.expanduser(value.strip("'\""))))
                continue
            if key in ("--git-dir", "--work-tree") and value:
                raise Deny("Aisk 守卫无法安全核对重定向的 Git 目录；请在仓库普通检出中执行。")
            if key == "-c" and value is not None:
                name = value.split("=", 1)[0].lower()
                if name.startswith(DANGEROUS_CONFIG):
                    raise Deny("Aisk 守卫无法安全核对临时 Git 配置；请移除该覆盖后重试。")
            continue
        if key in ("-c", "--config-env") and value is not None:
            name = value.split("=", 1)[0].lower()
            if name.startswith(DANGEROUS_CONFIG):
                raise Deny(f"任务内禁止用 git {key} 临时改写 {name}（传输、凭据、远端、钩子类配置）。")
        if key in ("-C", "--git-dir", "--work-tree") and value:
            target = os.path.normpath(os.path.join(ctx.cwd, os.path.expanduser(value.strip("'\""))))
            if not within(target, str(ctx.task_root)):
                raise Deny(f"任务内的 git 只能作用于本任务，{value} 在任务之外。")
    if not sub:
        return
    if sub not in GIT_KNOWN:
        alias = git_alias(ctx, sub)
        if alias:
            if alias.startswith("!"):
                analyze_text(ctx, alias[1:] + " " + " ".join(shlex.quote(a) for a in rest), depth + 1)
            else:
                check_git(ctx, "git", shlex.split(alias) + rest, depth + 1)
        return
    positional = [a for a in rest if not a.startswith("-")]
    check_main_git_write(ctx, sub, rest, positional, git_cwd)
    if getattr(ctx, "main_guard_only", False):
        if sub in MAIN_BRANCH_WRITE_COMMANDS:
            ctx.mutating = True
        return
    if sub in GIT_FORBIDDEN_ALWAYS:
        raise Deny(GIT_FORBIDDEN_ALWAYS[sub])
    if sub == "stash" and (not positional or positional[0] not in ("list", "show")):
        raise Deny("任务内禁止 git stash：所有 worktree 共用一个 stash 栈；要暂存请提交，或 aisk task pause。")
    if sub == "worktree" and (not positional or positional[0] != "list"):
        raise Deny("任务内禁止 git worktree：worktree 只能由任务引擎创建与删除。")
    if sub == "symbolic-ref" and len(positional) >= 2:
        raise Deny("任务内禁止 git symbolic-ref 改写引用。")
    if sub == "reflog" and positional and positional[0] in ("expire", "delete", "drop"):
        raise Deny("任务内禁止清理 reflog：受保护分支的来源审计依赖它。")
    if sub == "remote" and positional and positional[0] not in ("show", "get-url", "-v"):
        raise Deny("任务内禁止修改远端配置。")
    if sub == "config" and config_writes(rest):
        raise Deny("任务内禁止修改 git 配置（只读查询允许）。")
    policy = merge_policy(ctx)
    if policy.get("forbid_main_operations") and sub in {"merge", "rebase", "cherry-pick", "checkout", "switch", "reset", "restore"}:
        if any(is_manual_only_ref(a) for a in positional):
            raise Deny("禁止命令直接合并或改写 main/master；请通过 aisk task 生成候选，并由本人手工处理主分支。")
    if sub in {"merge", "rebase"} and any(a in {"ours", "theirs", "-Xours", "-Xtheirs"} for a in rest):
        raise Deny("禁止用 ours/theirs 批量吞掉冲突；必须基于共同祖先逐文件、逐块核对业务意图。")
    if sub in {"checkout", "restore"} and any(a in {"--ours", "--theirs"} for a in rest):
        raise Deny("禁止用 --ours/--theirs 批量解决冲突；必须逐文件、逐块核对业务意图。")
    if sub in ("checkout", "switch") and any(a in ctx.protected_branches for a in positional):
        raise Deny(f"任务内禁止切换到受保护分支：{'、'.join(sorted(ctx.protected_branches))}。")
    if sub == "branch":
        if any(a in ctx.protected_branches for a in positional) and any(
                a in ("-f", "--force", "-D", "-d", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy") for a in rest):
            raise Deny("任务内禁止创建、删除、移动或强制更新受保护分支。")
        if len(positional) >= 2 and positional[0] in ctx.protected_branches:
            raise Deny("任务内禁止改动受保护分支。")
    if "--no-verify" in rest or (sub == "commit" and any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a) for a in rest)):
        raise Deny("任务内禁止 --no-verify：仓库钩子是提交规则的一部分。")
    if sub in GIT_MUTATING or (sub == "branch" and positional):
        ctx.mutating = True


def config_writes(rest):
    if any(a in CONFIG_WRITE_FLAGS for a in rest):
        return True
    if any(a in CONFIG_QUERY_FLAGS for a in rest):
        return False
    positional, skip = [], False
    for a in rest:
        if skip:
            skip = False
            continue
        if a in CONFIG_OPTS_WITH_VALUE:
            skip = True
            continue
        if not a.startswith("-"):
            positional.append(a)
    if positional and positional[0] in ("get", "list"):
        return False
    if positional and positional[0] in ("set", "unset", "rename-section", "remove-section", "edit"):
        return True
    return len(positional) >= 2


def merge_policy(ctx):
    policy = ctx.cfg.raw.get("merge_policy") if isinstance(ctx.cfg.raw, dict) else None
    return policy if isinstance(policy, dict) else {}


def is_manual_only_ref(value):
    """只识别会实际指向分支的常见写法，不拦 log/diff 等只读查询。"""
    ref = str(value).strip().strip("'\"")
    for prefix in ("refs/heads/", "refs/remotes/origin/", "remotes/origin/", "origin/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    return ref.lower() in MANUAL_ONLY_BRANCHES


def git_alias(ctx, sub):
    try:
        result = subprocess.run(["git", "config", "--get", f"alias.{sub}"], cwd=ctx.cwd, capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def check_paths(ctx, prog, argv):
    args = [a for a in argv[1:] if not a.startswith("-") or "=" in a]
    if prog in ("cd", "pushd") and args:
        if getattr(ctx, "main_guard_only", False):
            return
        deny_if_protected(ctx, args[0], "任务内禁止进入主工作区、锚点、hub、登记簿或其他任务")
        return
    if prog in WRITE_PROGS:
        ctx.mutating = True
        targets = args[-1:] if prog in COPY_PROGS else args
        for a in targets:
            deny_if_protected(ctx, a.split("=", 1)[1] if a.startswith("of=") else a, "任务内禁止修改任务之外的受保护路径")
        return
    if prog in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in argv[1:]):
        ctx.mutating = True
        for a in args:
            deny_if_protected(ctx, a, "任务内禁止就地修改任务之外的文件")


def check_bash(cfg, cmd, cwd, task_root):
    """兼容入口：分析一条命令，拒绝时抛 Deny，返回是否会改动文件或任务状态。"""
    ctx = ShellContext(cfg, cwd, task_root)
    analyze_text(ctx, str(cmd))
    return ctx.mutating


def bash_mutates(cfg, cmd, cwd, task_root):
    try:
        return check_bash(cfg, cmd, cwd, task_root)
    except Deny:
        return True


# ------------------------------------------------------------------ 任务之外：对外动作确认
def _scan_outside_argv(argv, hits, depth=0):
    """在不依赖 profile 的情况下识别一段 shell argv 里的 git push。

    这是任务外的**发现器**，不是任务内守卫：不做路径/租约/分支判断，只追踪 shell 包装器，
    让 `sudo git push`、`env FOO=1 git push`、`bash -lc 'git push'`、`git -C <repo> push`
    等写法都落入同一个确认闸门。"""
    if depth > MAX_DEPTH or not argv:
        return
    i = 0
    while i < len(argv):
        token = str(argv[i])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            i += 1
            continue
        base = prog_name(token, set())
        if base in WRAPPERS:
            i += 1
            while i < len(argv):
                current = str(argv[i])
                if base == "timeout" and re.match(r"^\d", current):
                    i += 1
                    continue
                if current in WRAPPER_OPTS_WITH_VALUE:
                    i += 2
                elif current.startswith("-"):
                    i += 1
                else:
                    break
            continue
        if base in SHELLS:
            script = shell_script(base, [str(v) for v in argv[i + 1:]])
            if script is not None:
                _scan_outside_text(script, hits, depth + 1)
            return
        break
    argv = argv[i:]
    if not argv:
        return
    prog = prog_name(argv[0], set())
    args = [str(v) for v in argv[1:]]
    if prog in ("eval",):
        _scan_outside_text(" ".join(args), hits, depth + 1)
        return
    if prog == "git-push":
        hits.add("push")
        return
    if prog != "git":
        return
    i = 0
    while i < len(args) and args[i].startswith("-"):
        option = args[i]
        if option in GIT_OPT_WITH_VALUE:
            i += 2
        elif option.startswith(("-C", "-c")) and len(option) > 2:
            i += 1
        else:
            i += 1
    if i < len(args) and args[i] in {"push", "merge"}:
        hits.add(args[i])


def _scan_outside_text(text, hits, depth=0):
    if depth > MAX_DEPTH:
        return
    text, substitutions = extract_substitutions(str(text))
    for inner in substitutions:
        _scan_outside_text(inner, hits, depth + 1)
    try:
        tokens = tokenize(text)
    except ValueError:
        # 引号损坏时仍识别最关键的动作；不能因为 shell 语法坏了就漏掉 push。
        if re.search(r"(?<![\w-])git(?:\.exe)?(?:\s+-[^;&|\s]+(?:\s+[^;&|\s]+)?)*\s+push(?:\s|$)", text):
            hits.add("push")
        return
    for argv in split_simple(tokens):
        _scan_outside_argv(argv, hits, depth)


def scan_outside_high_risk(command):
    """返回任务外命中的动作名；只读分析失败时宁可返回空，不改变工具执行语义。"""
    hits = set()
    try:
        _scan_outside_text(str(command), hits)
    except Exception:  # noqa: BLE001
        return set()
    return hits


def _remote_host(url):
    value = str(url or "").strip().strip("\\\"'")
    if not value or value.startswith(("/", "./", "../", "file:")):
        return ""
    # scp-like SSH URL：git@github.com:org/repo
    match = re.match(r"^(?:[^@/]+@)?([^:/]+):.+$", value)
    if match and "://" not in value:
        return match.group(1).lower().rstrip(".")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower().rstrip(".")


def is_public_remote(url):
    """判断远端是否不是本机/内网地址。凭 host 判定，不把完整 URL 放进弹窗或审计。"""
    host = _remote_host(url)
    if not host or host in LOCAL_REMOTE_HOSTS or host.endswith(LOCAL_REMOTE_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


def outside_origin_url(cwd):
    """读取 cwd 所在仓库的 origin URL；失败表示没有可确认的公开远端。"""
    target = Path(str(cwd or os.getcwd())).expanduser()
    if target.is_file():
        target = target.parent
    try:
        result = subprocess.run(["git", "-C", str(target), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _command_dirs(command, cwd, depth=0):
    """命令里 cd / git -C 指向的目录，按出现顺序返回（后面的 cd 覆盖前面的）。

    WorkBuddy 的 PreToolUse 载荷里 cwd 恒为会话工作区根，通常不是 git 仓库；
    真正执行 push 的目录只写在命令文本里（含 `sh -c` 之类的包装层），所以判定
    远端必须回到命令本身。只读解析，解析不出来就返回空列表，调用方回退到 cwd。"""
    if depth > MAX_DEPTH:
        return []
    base = Path(str(cwd or os.getcwd())).expanduser()
    text, substitutions = extract_substitutions(str(command))
    found = []
    for source in [text] + [str(item) for item in substitutions]:
        try:
            tokens = tokenize(source)
        except ValueError:
            continue
        current = base
        for argv in split_simple(tokens):
            argv = [str(v) for v in argv]
            script = None
            for index, token in enumerate(argv[:-1]):
                option = token.strip("'\"")
                program = prog_name(token, set())
                if program in SHELLS:
                    script = shell_script(program, argv[index + 1:])
                    break
                if program == "cd":
                    target = argv[index + 1].strip("'\"")
                    if target and not target.startswith("-"):
                        found.append(os.path.normpath(os.path.join(current, os.path.expanduser(target))))
                        current = Path(found[-1])
                elif option == "-C":
                    found.append(argv[index + 1].strip("'\""))
                elif option.startswith("-C") and len(option) > 2:
                    found.append(option[2:].strip("'\""))
            if script is not None:
                # 包装层里的 cd 是相对外层当前目录的，带着 current 递归下去
                found.extend(_command_dirs(script, current, depth + 1))
    resolved = []
    for target in reversed(found):  # push 通常跟在最后一个 cd 后面
        candidate = Path(os.path.normpath(os.path.join(base, os.path.expanduser(target))))
        if candidate.is_dir() and candidate not in resolved:
            resolved.append(candidate)
    return list(reversed(resolved))


def outside_cwd_candidates(command, cwd):
    """push 可能落在的目录：命令里的 cd/git -C 目标优先，工具上报的 cwd 兜底。"""
    base = Path(str(cwd or os.getcwd())).expanduser()
    candidates = _command_dirs(command, cwd)
    if base not in candidates:
        candidates.append(base)
    return candidates


def outside_push_context(candidates):
    """在候选目录里找第一个能读到 origin 的，返回 (remote_url, 目录)。"""
    for candidate in candidates:
        url = outside_origin_url(candidate)
        if url:
            return url, str(candidate)
    return "", str(candidates[-1]) if candidates else ""


def _outside_forbidden_reasons(command, cwd):
    """读取可定位到的 profile 的 forbidden_commands；无 profile 仍能识别 git push。"""
    try:
        cfg, _ = config.load_config(start=cwd)
    except Exception:  # noqa: BLE001
        return []
    reasons = []
    for pattern, reason in cfg.forbidden_commands.items():
        try:
            if re.search(pattern, str(command)):
                reasons.append(str(reason))
        except re.error:
            continue
    return reasons


def _outside_action_argv(argv, action, depth=0):
    """从一组已分词的 shell argv 中取出 git <action> 的参数。"""
    if depth > MAX_DEPTH or not argv:
        return None
    i = 0
    while i < len(argv):
        token = str(argv[i])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            i += 1
            continue
        base = prog_name(token, set())
        if base in WRAPPERS:
            i += 1
            while i < len(argv):
                current = str(argv[i])
                if base == "timeout" and re.match(r"^\d", current):
                    i += 1
                    continue
                if current in WRAPPER_OPTS_WITH_VALUE:
                    i += 2
                elif current.startswith("-"):
                    i += 1
                else:
                    break
            continue
        if base in SHELLS:
            script = shell_script(base, [str(v) for v in argv[i + 1:]])
            return _outside_action_argv_text(script, action, depth + 1) if script is not None else None
        break
    argv = [str(v) for v in argv[i:]]
    if not argv:
        return None
    prog = prog_name(argv[0], set())
    args = argv[1:]
    if prog == f"git-{action}":
        return args
    if prog != "git":
        return None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        option = args[i]
        if option in GIT_OPT_WITH_VALUE:
            i += 2
        elif option.startswith(("-C", "-c")) and len(option) > 2:
            i += 1
        else:
            i += 1
    return args[i + 1:] if i < len(args) and args[i] == action else None


def _outside_action_argv_text(command, action, depth=0):
    if command is None or depth > MAX_DEPTH:
        return None
    text, substitutions = extract_substitutions(str(command))
    # substitutions 里的 shell 命令即使不在顶层 argv，也可能包含真正的动作。
    for inner in substitutions:
        found = _outside_action_argv_text(inner, action, depth + 1)
        if found is not None:
            return found
    try:
        tokens = tokenize(text)
    except ValueError:
        return None
    for argv in split_simple(tokens):
        found = _outside_action_argv(list(argv), action, depth)
        if found is not None:
            return found
    return None


def _outside_push_argv(argv, depth=0):
    return _outside_action_argv(argv, "push", depth)


def _outside_push_argv_text(command, depth=0):
    return _outside_action_argv_text(command, "push", depth)


def _short_sha(value):
    return str(value)[:7] if value else "未获取"


def outside_push_details(command, cwd):
    """取得用于确认框的最小可审计信息，不返回 URL、凭据或完整路径。"""
    args = _outside_push_argv_text(command) or []
    root = git.out(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    root = root.strip() if root else ""
    repo = Path(root).name if root else Path(str(cwd or os.getcwd())).expanduser().name
    current = git.current_branch(cwd) or ""
    remotes = [line.strip() for line in git.out(["remote"], cwd=cwd, check=False).splitlines() if line.strip()]
    remote = "origin"
    positional = [value for value in args if value != "--" and not value.startswith("-")]
    # `git push <remote> <ref>` 的 remote 通常能从 git remote 读到；测试/异常仓库可能读不到，
    # 仍按 Git 的常见 remote 名称识别，避免把 `origin` 错报成待更新分支。
    if positional and (positional[0] in remotes or positional[0] in {"origin", "upstream", "hub"}) and len(positional) >= 2:
        remote = positional.pop(0)
    all_refs = any(value in ("--all", "--mirror") for value in args)
    refs = positional or ([current] if current else ["当前分支"])
    targets = []
    for ref in refs:
        target = str(ref).rsplit(":", 1)[-1]
        target = re.sub(r"^refs/heads/", "", target)
        if target in ("HEAD", "") and current:
            target = current
        if target and target not in targets:
            targets.append(target)
    branch = "全部分支" if all_refs else (",".join(targets) or "当前分支")
    local_sha = git.sha(cwd, "HEAD")
    baseline = None
    if len(targets) == 1 and re.fullmatch(r"[A-Za-z0-9._/-]+", targets[0]):
        baseline = git.sha(cwd, f"refs/remotes/{remote}/{targets[0]}")
    force = any(value in ("--force", "-f", "--force-with-lease") for value in args)
    delete = any(value in ("--delete", "-d") for value in args)
    if delete:
        operation = f"删除远端分支 {branch}。"
    elif force:
        operation = f"强制推送，并且只更新 {branch}（不是 fast-forward）。"
    else:
        operation = f"普通 fast-forward 推送，并且只更新 {branch}。"
    return {"repository": repo or "未知仓库", "branch": branch, "local_sha": _short_sha(local_sha),
            "remote_base": _short_sha(baseline), "operation": operation,
            "targets": targets, "all_refs": all_refs}


def outside_merge_details(command, cwd):
    """取得 git merge 的目标分支，不执行合并也不读取业务内容。"""
    args = _outside_action_argv_text(command, "merge") or []
    targets = []
    for value in args:
        if value == "--":
            continue
        if value.startswith("-"):
            continue
        target = str(value).rsplit(":", 1)[-1]
        target = re.sub(r"^refs/(?:heads|remotes)/", "", target)
        if target and target not in targets:
            targets.append(target)
    return {"repository": Path(str(cwd or os.getcwd())).expanduser().name,
            "branch": ",".join(targets) or "未知分支", "targets": targets}


def _protected_targets(cwd):
    """读取当前项目档案的受保护分支；档案不可读时使用最小安全默认值。"""
    protected = set(DEFAULT_PROTECTED_PUSH_BRANCHES)
    personal = set()
    try:
        cfg, _ = config.load_config(start=cwd)
        protected.update(str(b) for b in cfg.protected)
        for alias in cfg.repo_order:
            repo = cfg.repo(alias)
            protected.add(str(repo.trunk))
            # integration_branch 是个人集成面（新华为 fxh），即使旧档案把它列在
            # protected 中，也不能把日常 fxh → fxh-dev 推送误判成主干发布。
            if cfg.integration and cfg.integration != repo.trunk:
                personal.add(str(cfg.integration))
            if repo.push_branch and repo.push_branch != repo.trunk:
                personal.add(str(repo.push_branch))
    except Exception:  # noqa: BLE001
        pass
    return protected, personal


def _requires_branch_confirmation(cwd, targets, *, all_refs=False):
    protected, personal = _protected_targets(cwd)
    if all_refs or not targets:
        return True
    for target in targets:
        # 个人集成分支优先于旧档案中的 protected 声明；只有仓库 trunk 不能走这个例外。
        # 这兼容历史 profile 把 fxh 同时列入 protected 的情况。
        if target in personal:
            continue
        if target in protected:
            return True
    return False


def confirm_outside_high_risk(tool, normalized):
    """任务外动作的分支策略：个人分支日常推送放行，受保护交付必须确认。

    这个分支只在 ``task_root is None`` 时调用。任务内仍由现有守卫无条件拒绝
    push，因此不会把任务目录的推送封锁降级成「点一下就能过」。任务外支持的
    AI 工具统一使用带工具名的确认框：``git推送-codex``、``git合并-claude`` 等。
    ``dev/main/master`` 以及 profile 声明的 trunk 永远需要确认；``fxh``、
    ``fxh-dev`` 这类个人集成分支默认不拦截。"""
    if tool not in OUTSIDE_CONFIRM_TOOLS or not normalized.get("command"):
        return None
    command = str(normalized["command"])
    cwd = str(normalized.get("cwd") or os.getcwd())
    # 工具上报的 cwd 常是会话工作区根（多半不是仓库），真正的执行目录要从命令里还原，
    # 否则读不到 origin → 判不成公开远端 → 任务外 push 会被静默放行。
    candidates = outside_cwd_candidates(command, cwd)
    reasons = []
    for candidate in candidates:
        for reason in _outside_forbidden_reasons(command, candidate):
            if reason not in reasons:
                reasons.append(reason)
    hits = scan_outside_high_risk(command)
    _remote, push_cwd = outside_push_context(candidates) if "push" in hits else ("", cwd)
    details = outside_push_details(command, push_cwd) if "push" in hits else None
    merge_cwd = candidates[0] if candidates else cwd
    merge = outside_merge_details(command, merge_cwd) if "merge" in hits else None

    # 个人分支 push 是日常开发闭环，不能因为远端是 GitHub、内网或本地路径而误拦；
    # dev/main/master 则不区分远端类型，一律进入确认框。远端是否真正禁止直推由
    # GitHub/GitLab 等服务端保护规则兜底，不能只依赖临时 clone 的本地钩子。
    if details and not _requires_branch_confirmation(
        push_cwd, details.get("targets", []), all_refs=details.get("all_refs", False)
    ):
        hits.discard("push")
        details = None
    elif details:
        target = details.get("branch", "受保护分支")
        reasons.append(f"{OUTSIDE_CONFIRM_ACTIONS['push']} 将写入受保护分支 {target}")

    if merge and not _requires_branch_confirmation(cwd, merge.get("targets", [])):
        hits.discard("merge")
        merge = None
    elif merge:
        reasons.append(f"{OUTSIDE_CONFIRM_ACTIONS['merge']} 将合入受保护分支 {merge.get('branch', '未知分支')}")

    # 只有「git push / git merge」命中但已判定为个人分支时，才清除对应的默认命中；
    # 自定义 forbidden_commands 产生的原因仍然保留，不能被分支例外绕过。
    action_details = details or merge
    if not reasons:
        return None
    summary = "；".join(reasons[:3])
    if details:
        # 标题由 ActionContext 统一生成，包含工具名；正文给出提交、远端旧基线、
        # fast-forward 与目标分支，不显示完整 URL 或本地路径。
        prompt = (f"即将推送 {details['repository']} 的受保护分支 {details['branch']}。\n\n"
                  f"仓库：{details['repository']}\n"
                  f"本地提交：{details['local_sha']}\n"
                  f"远端旧基线：{details['remote_base']}\n"
                  f"操作：{details['operation']}")
        expect = "确认推送"
        action = "git推送"
        repository = details["repository"]
    elif merge:
        prompt = (f"即将把分支 {merge['branch']} 合入 {merge['repository']} 的受保护分支。\n\n"
                  f"仓库：{merge['repository']}\n"
                  f"目标：{merge['branch']}\n"
                  f"命令：{command[:300]}")
        expect = "确认合并"
        action = "git合并"
        repository = merge["repository"]
    else:
        prompt = (f"WorkBuddy 将在任务目录之外执行高风险动作：\n"
                  f"  目录：{cwd}\n"
                  f"  命令：{command[:300]}\n"
                  f"  原因：{summary}\n"
                  "这条操作不受 aisk 任务目录、租约与交付门禁保护。")
        expect = "确认"
        action = "高危动作"
        repository = action_details.get("repository", "") if action_details else ""
    try:
        accepted = registry.confirm_human(
            prompt, expect, action=action, tool=tool, task_id=normalized.get("task_id", ""),
            session_id=normalized.get("session_id", ""), risk_level="HIGH", repository=repository)

    except Exception:  # noqa: BLE001
        accepted = False
    if accepted:
        return None
    return f"任务目录之外的高风险动作未获操作者确认，已阻止：{summary}。请由操作者本人执行。"


# ------------------------------------------------------------------ 租约
def check_lease(cfg, meta, tool, payload):
    reg = Registry(cfg)
    task = reg.load(meta.get("id", ""), must=False) if meta.get("id") else None
    if not task or task.get("_from_hub") or task.get("state") not in LIVE_STATES or task.get("creating") \
            or task.get("archiving"):
        return
    sessions = actor.session_ids(tool, payload=payload)
    if not sessions and tool != "human":
        raise Deny(f"无法识别当前会话：请设置本会话独有的 {names.ENV_SESSION} 后 {names.CLI} claim，不能只按工具名写入。")
    owner = task.get("owner")
    if model.same_actor(owner, tool, sessions):
        heartbeat_at = parse_iso(owner.get("heartbeat_at"))
        if not heartbeat_at or time.time() - heartbeat_at.timestamp() > HEARTBEAT_EVERY:
            with tasks.try_lock(reg) as got:
                if got:
                    tasks.heartbeat(reg, reg.load(task["id"]), tool, sessions)
        return
    lease, act = tasks.lease_of(cfg, task, quick=True)
    if lease != model.HELD:
        lease, act = tasks.lease_of(cfg, task)
    if lease == model.CLAIMABLE:
        with tasks.try_lock(reg) as got:
            if not got:
                return
            fresh = reg.load(task["id"])
            fresh_lease, _ = tasks.lease_of(cfg, fresh)
            if fresh_lease != model.CLAIMABLE and not model.same_actor(fresh.get("owner"), tool, sessions):
                raise Deny(f"任务 {fresh['id']} 刚被 {tasks.owner_label(fresh.get('owner'))} 认领，只读，不要修改。")
            prev = fresh.get("owner")
            fresh["owner"] = tasks.new_owner(cfg, tool, sessions)
            note = "首次改动时自动认领" + (f"（原执行者 {tasks.owner_label(prev)} 已超时）" if prev else "")
            tasks.append_progress(fresh, tasks.progress_line(tool, note))
            if fresh["state"] == "parked":
                reg.set_state(fresh, "active", note=note)
            else:
                reg.save(fresh)
            reg.event("claim", task=fresh["id"], tool=tool, prev=tasks.owner_label(prev), lease=lease, auto=True)
        return
    who = tasks.owner_label(owner)
    age = model.age_text(act, time.time())
    if lease == model.IDLE:
        raise Deny(f"任务 {task['id']} 由 {who} 持有但已空闲（{age}活动）。确认对方不会回来再接手："
                   f"{names.CLI} claim {task['id']} --tool {tool} --takeover --reason \"…\"；否则只读。")
    raise Deny(f"任务 {task['id']} 由 {who} 持有中（{age}活动）：只读，不要修改。"
               f"要协作请让操作者另开子任务（{names.CLI} new … --base {task['id']}）。")


# ------------------------------------------------------------------ 入口
def locate_task(normalized, fallback=None):
    """定位这次调用属于哪个任务。

    **fallback 不是可选优化**：cwd 来自工具载荷（Antigravity 的 `Cwd`、Claude 的 `workdir` 都由模型给），
    指到任务之外就一个任务都找不到，守卫会整体放行——等于凭一个参数就绕过全部边界。
    任务目录里的钩子生成时写死了自己的任务根，作为兜底。"""
    for start in (normalized["cwd"], os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get("CODEBUDDY_PROJECT_DIR"),
                  *normalized["paths"]):
        root = find_task_root(start) if start else None
        if root:
            return root
    return find_task_root(fallback) if fallback else None


def deny_main_branch_write_request(normalized):
    """Apply the unconditional main guard to file tools and shell commands, with or without a task."""
    if normalized.get("write"):
        for raw in normalized.get("paths", []):
            target = Path(strip_uri(raw)).expanduser()
            if not target.is_absolute():
                target = Path(normalized.get("cwd") or os.getcwd()) / target
            try:
                deny_main_write_path(target)
            except Deny as error:
                return str(error)

    command = normalized.get("command") or ""
    if not command:
        return None
    cwd = normalized.get("cwd") or os.getcwd()
    try:
        candidates = outside_cwd_candidates(command, cwd)
    except Exception:  # noqa: BLE001
        candidates = [cwd]
    for candidate in candidates:
        # Shell parsing finds relative paths, redirections, wrapped commands,
        # git aliases, and explicit refs without needing a project profile.
        ctx = MainGuardShellContext(candidate)
        try:
            analyze_text(ctx, command)
        except Deny as error:
            return str(error)
    return None


def evaluate(payload, tool, task_root=None):
    """返回拒绝理由；放行返回 None。"""
    normalized = normalize(payload)
    main_write_reason = deny_main_branch_write_request(normalized)
    if main_write_reason:
        return main_write_reason
    task_root = locate_task(normalized, task_root)
    if task_root is None:
        direct_reason = direct_checkout_guard(normalized)
        if direct_reason:
            return direct_reason
        # 用户级宿主 hook 没有固定任务根，因此任务外动作必须在这里单独处理。
        # 个人分支 push 默认放行，受保护分支 push/merge 统一交给带工具归属的确认框；
        # 未接入宿主 hook 的工具仍由远端 branch protection 作为最终兜底。
        return confirm_outside_high_risk(tool, normalized)
    try:
        meta = json.loads(names.task_meta_file(task_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        meta = {}
    try:
        cfg, _ = config.load_config(meta.get("profile"), start=task_root)
    except Exception as e:  # noqa: BLE001  档案读不出来：读放行，写一律拒绝，不能默认放行
        if mutating_request(payload):
            return GUARD_BROKEN.format(detail=f"读不到项目档案：{str(e)[:120]}")
        return None
    if normalized["unresolved_write"]:
        return (f"工具 {normalized['name']} 是写入类调用，却没有给出可识别的文件路径："
                f"守卫无法判断是否写到任务之外，请让工具明确给出目标文件路径。")
    cwd = normalized["cwd"]
    try:
        mutating = False
        if normalized["write"]:
            for target in normalized["paths"]:
                check_edit(cfg, target, cwd, task_root)
            mutating = True
        if normalized["command"]:
            mutating = check_bash(cfg, normalized["command"], cwd, task_root) or mutating
        if mutating and tool:
            check_lease(cfg, meta, tool, payload)
    except Deny as d:
        return str(d)
    return None


def deny_output(tool, reason):
    text = f"[任务守卫] {reason}\n任务须知见 AGENT-WORKTREE.md；需要越界操作请交给操作者。"
    if tool == "antigravity":
        return json.dumps({"decision": "deny", "reason": text}, ensure_ascii=False)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": text}}, ensure_ascii=False)


def deny_json(reason):
    return deny_output("claude", reason)


def main(tool, stdin=None, task_root=None):
    # WorkBuddy 两个入口共读同一份项目级 settings，只能写一个工具名，所以 bind.py 传
    # 哨兵值 `auto`，在这里按当前进程解析成真正在跑的那一端。其余取值原样返回。
    tool = actor.resolve_tool(tool)
    try:
        payload = json.load(stdin or sys.stdin)
    except Exception:  # noqa: BLE001
        payload = None
    reason = None
    if isinstance(payload, dict):
        try:
            reason = evaluate(payload, tool, task_root)
        except Exception as e:  # noqa: BLE001  守卫自身故障：读放行，写拒绝
            reason = GUARD_BROKEN.format(detail=f"{type(e).__name__}: {str(e)[:120]}") if mutating_request(payload) else None
    else:
        # 连输入都读不出来，就判断不了这次是读还是写——此时放行等于整条防线失效
        reason = GUARD_BROKEN.format(detail="钩子输入不是 JSON 对象")
    if reason:
        print(deny_output(tool, reason))
    elif tool == "antigravity":
        print("{}")
    return 0
