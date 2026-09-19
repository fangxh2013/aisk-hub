# -*- coding: utf-8 -*-
"""全部对外可见的名字集中在这里：命令、目录、git 引用与配置键、环境变量、生成文件。

原则（WORKTREE.md §1）：名字描述功能而不是项目，不用项目缩写；统一挂在内核名 aisk 之下，
与 AISK_HOME、AISK_PROFILE 同一家族；换项目只换档案，这些名字不变。
改名只改本文件；旧版本布局的名字只在 LEGACY_* 里出现，退役后删除。
"""

CLI = "aisk task"
SUBCOMMAND = "task"

# 任务目录内的元数据
META_DIR = ".aisk"
TASK_META = f"{META_DIR}/task.json"
ENV_FILE = f"{META_DIR}/env"
ENV_CMD = f"{META_DIR}/env.cmd"
ENV_PS1 = f"{META_DIR}/env.ps1"

# 数据根
BOARD = "BOARD.md"

# git：管理目录名前缀（includeIf 只命中任务，不命中锚点）、引用命名空间、配置标记
TASK_ADMIN_PREFIX = "aisk-task-"
ANCHOR_ADMIN_PREFIX = "aisk-anchor-"
REF_NS = "refs/aisk"
GIT_MARK_KEY = "aisk.taskWorktree"
PUSH_DISABLED_URL = "file:///dev/null/aisk-push-disabled"

# 机器级配置里的块标记
BLOCK_BEGIN = "# >>> aisk task >>>"
BLOCK_END = "# <<< aisk task <<<"
MD_BEGIN = "<!-- aisk-task:rules begin -->"
MD_END = "<!-- aisk-task:rules end -->"

# 各工具的生成物
CODEX_PROFILE = "aisk-task"
AGENTS_RULE_FILE = "aisk-task.md"
ANTIGRAVITY_HOOK_PREFIX = "aisk-task"
DEPS_MARKER = ".aisk-deps.json"

# 环境变量
ENV_TASK = "AISK_TASK"
ENV_TASK_DIR = "AISK_TASK_DIR"
ENV_TOOL = "AISK_TOOL"
ENV_SESSION = "AISK_SESSION"
ENV_LAUNCHER = "AISK_LAUNCHER"
ENV_GIT = "AISK_GIT"

# v1 旧布局的名字：迁移期识别旧槽位、替换旧配置块，退役后删除
LEGACY_TASK_METAS = (".xw/slot.json",)
LEGACY_TASK_ADMIN_PREFIXES = ("xw-",)
LEGACY_ANCHOR_ADMIN_PREFIXES = ("xa-",)
LEGACY_GIT_BLOCKS = (
    ("# >>> legacy-worktrees (xw) >>>", "# <<< legacy-worktrees (xw) <<<"),
    ("# >>> aisk worktree (xw) >>>", "# <<< aisk worktree (xw) <<<"),
)
LEGACY_MD_BLOCKS = (
    ("<!-- legacy-worktrees:rules begin -->", "<!-- legacy-worktrees:rules end -->"),
    ("<!-- aisk-worktree:rules begin -->", "<!-- aisk-worktree:rules end -->"),
)
LEGACY_CODEX_PROFILES = ("xw",)
LEGACY_REF_NS = ("refs/xw",)


def task_meta_file(root):
    """优先读取新元数据，迁移期继续识别旧槽位；不修改旧文件。"""
    from pathlib import Path
    for relative in (TASK_META, *LEGACY_TASK_METAS):
        candidate = Path(root) / relative
        if candidate.is_file():
            return candidate
    return None
