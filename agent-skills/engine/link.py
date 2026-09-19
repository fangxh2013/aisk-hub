# -*- coding: utf-8 -*-
"""把内核技能分发到各 AI 工具端。

**这一层的存在意义**：各端对技能的要求各不相同（路径、命名、清单、frontmatter 契约），
但内核只写一份。所有端上差异由本模块吸收，技能正文零分叉——
这直接消灭了原 `adapters/claude/` 手工适配层造成的事实漂移（曾出现 `hbxhxt-infra`
拼错和 12 处死链）。

**安全约束（各端都有第三方技能，误删就是事故）**：
  - 只按名写入我们自己的技能，绝不整目录 --delete
  - 写入前记录哪些是我们的（.aisk-managed 清单），只回收清单内的
  - 端上自带/第三方技能（Codex 6 项、WorkBuddy 7 项腾讯系、Antigravity 官方插件）一律不碰
"""

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import profile as profile_mod
from .skill_router import SkillRouter, SkillRouterError

MANAGED_MARKER = ".aisk-managed"
# 分发出去的脚本不在内核目录里，靠 $AISK_HOME/kernel-root 回找内核；内核目录搬家后重跑 aisk link 即可
KERNEL_POINTER = "kernel-root"
RESOURCE_DIRS = ("references", "scripts", "assets", "agents")

# Claude 内置技能撞名，必须改名（见设计文档 §3.2）
CLAUDE_RENAMES = {
    "security-review": "sec-review",
    "database-design": "db-design",
    "ruoyi-module-scaffold": "ruoyi-scaffold",
    "element-plus-patterns": "element-plus",
    "k3s-deployment": "k3s-deploy",
}

# WorkBuddy 扁平命名空间，方法论类加前缀避免撞名
METHODOLOGY = {"brainstorming", "change-safety", "pre-commit-review", "systematic-debugging"}

# WorkBuddy 桌面版与 WorkBuddy AI 是两个独立 App，配置目录不同（后者读
# WORKBUDDY_DATA_FOLDER_NAME=.workbuddy-ai）。两端对 SKILL.md 的要求完全一致
# （扁平命名空间 + methodology- 前缀 + 顶层 agent_created 还原），所以共用一套端上改写。
WORKBUDDY_TOOLS = {"workbuddy", "workbuddy-ai"}

# 被合并掉的旧技能：留在端上会与新的 ops / git-flow 重复覆盖，模型会选错技能。
# **这份清单是硬编码的、可审计的**——link 只会退役这里列出的名字，
# 端上任何第三方技能（Codex 的 pdf/playwright、WorkBuddy 的腾讯系、
# 以及待收编的孤儿 dev-release）都不在其中，不可能被误删。
RETIRED = {
    "dev-ops", "dev-ops-lite", "prod-ops", "prod-ops-lite",
    "prod-k8s", "uat-ops", "v2-prod-ops",   # → 合并进 ops
    "git-commit", "git-merge-dev",           # → 合并进 git-flow
}

# Codex 的旧适配曾把技能写到 ~/.codex/skills；现在官方用户级发现目录是
# ~/.agents/skills。这里的映射用于把已经被新内核替代的旧名字**归档**，而不是
# 删除。它只在 Codex 目标端执行，避免把 Claude 的改名约定带到其他工具。
CODEX_SHADOWS = {
    "dev-ops": "ops", "dev-ops-lite": "ops", "prod-ops": "ops",
    "prod-ops-lite": "ops", "prod-k8s": "ops", "uat-ops": "ops",
    "v2-prod-ops": "ops", "git-commit": "git-flow", "git-merge-dev": "git-flow",
    "sec-review": "security-review", "db-design": "database-design",
    "ruoyi-scaffold": "ruoyi-module-scaffold", "element-plus": "element-plus-patterns",
    "k3s-deploy": "k3s-deployment",
}

def write_kernel_pointer(kernel_root):
    """记下这次分发来自哪个内核检出，供分发出去的脚本定位 engine。"""
    home = Path(os.environ.get("AISK_HOME") or Path.home() / ".aisk")
    home.mkdir(parents=True, exist_ok=True)
    pointer = home / KERNEL_POINTER
    pointer.write_text(str(Path(kernel_root).resolve()) + "\n", encoding="utf-8")
    return pointer


TARGETS = {
    "claude": {"root": "~/.claude/skills", "flat": True},
    "codex": {
        "root": "~/.agents/skills", "legacy_root": "~/.codex/skills",
        "backup_root": "$AISK_HOME/backups/codex-skills", "flat": True,
    },
    "antigravity": {"root": "~/.gemini/config/plugins/agent-skills/skills", "flat": True},
    "workbuddy": {"root": "~/.workbuddy/skills", "flat": True},
    # WorkBuddy AI 是另一个 App：用户级技能目录跟着它的配置目录走
    # （`<configDir>/skills`，本机 configDir = ~/.workbuddy-ai）。
    # 不指向这里，内核技能对 WorkBuddy AI 就是「一个都没分发」。
    "workbuddy-ai": {"root": "~/.workbuddy-ai/skills", "flat": True},
    "cursor": {"root": "~/.cursor/skills", "flat": True},
}

# 不带端名时 `aisk link` 分发哪些端。放在这里而不是 CLI 里，是为了让
# 「端清单」只有一处来源——文档里的端数与它比对（tools/check_doc_drift.py），
# 加端时漏改文档会被门禁挡下。Cursor 不在默认里：它的技能目录由用户按需启用。
DEFAULT_TOOLS = ["claude", "codex", "antigravity", "workbuddy", "workbuddy-ai"]


def target_name(tool, skill):
    # Endpoints receive canonical names only.  Legacy directories remain
    # readable during migration, but are never copied as duplicate entries.
    skill = SkillRouter().resolve(skill)
    if tool == "claude":
        return CLAUDE_RENAMES.get(skill, skill)
    if tool in WORKBUDDY_TOOLS and skill in METHODOLOGY:
        return f"methodology-{skill}"
    return skill


def _kernel_sources(kernel_skills):
    """Return ``{canonical_name: source_dir_name}`` for a skill tree.

    A checkout may still contain old directories ignored by Git.  If both an
    old alias and its canonical directory exist, the canonical directory wins;
    this makes a rerun deterministic without deleting the old directory.
    """
    router = SkillRouter()
    sources = {}
    for directory in sorted(kernel_skills.iterdir(), key=lambda item: item.name):
        if not (directory / "SKILL.md").is_file():
            continue
        name = directory.name
        canonical = router.resolve(name)
        if canonical in router.canonical_names():
            if canonical not in sources or name == canonical:
                sources[canonical] = name
        else:
            sources[name] = name
    return sources


def _split_fm(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    return (m.group(1), m.group(2)) if m else (None, text)


def _restore_top_level(fm):
    """WorkBuddy 依赖顶层 agent_created 判定「AI 可管理技能」，分发时从 metadata 还原。

    实测 35/39 己方技能带此顶层字段、第三方技能 0/7 带，相关性 100%。
    收进 metadata 会让 WorkBuddy 失去对这批技能的管理能力，所以这一端必须还原。
    """
    ver = re.search(r"^\s{2}version:\s*(.+)$", fm, re.M)
    ac = re.search(r"^\s{2}agent_created:\s*(.+)$", fm, re.M)
    fm = re.sub(r"\nmetadata:\n(?:\s{2}\w+:.*\n?)*", "\n", fm).rstrip()
    extra = ""
    if ver:
        extra += f"\nversion: {ver.group(1).strip()}"
    if ac:
        extra += f"\nagent_created: {ac.group(1).strip()}"
    return fm + extra


def render_skill(tool, skill, text):
    """按端改写 SKILL.md 内容。"""
    fm, body = _split_fm(text)
    if fm is None:
        return text
    new_name = target_name(tool, skill)
    fm = re.sub(r"^name:\s*.+$", f"name: {new_name}", fm, count=1, flags=re.M)
    if tool in WORKBUDDY_TOOLS:
        fm = _restore_top_level(fm)
    # 端上调用前缀不同：Codex 用 $，其余用 /
    if tool == "codex":
        body = body.replace("`/agent-skills:", "`$")
        body = re.sub(
            r"!`aisk env --brief`\n+(?:<!--.*?-->\n*)?",
            "需要环境事实时执行 `aisk env --brief`；本端不会执行 Markdown 动态宏。\n\n",
            body, flags=re.S,
        )
    elif tool == "antigravity":
        # 1. 转换 Claude 专用的动态宏为 Antigravity 友好的提示
        body = re.sub(
            r"!`aisk env --brief`\n+(?:<!--.*?-->\n*)?",
            "> [!TIP]\n> 可运行 `aisk env --brief` 或直接调用 MCP 工具 `aisk_env_summary` 获取当前环境拓扑与配置。\n\n",
            body,
            flags=re.S,
        )
        # 2. 方法论技能对接 Antigravity Planning Mode 与 Artifacts
        if skill == "brainstorming" and "Planning Mode" not in body:
            body += (
                "\n\n## Antigravity 规划模式对接 (Planning Mode)\n\n"
                "- 需求澄清完成后，按 Antigravity 标准生成 `implementation_plan.md` Artifact（设置 `RequestFeedback: true`，`UserFacing: true`）；\n"
                "- 复杂系统交互使用 Mermaid 状态图/架构图展示；关键注意事项使用 GitHub Alerts (`> [!IMPORTANT]`, `> [!WARNING]`)；\n"
                "- 用户已授权实现时完成方案后继续执行；仅在缺少影响结果的选择或权限时询问。\n"
            )
        elif skill in ("change-safety", "pre-commit-review") and "walkthrough.md" not in body:
            body += (
                "\n\n## Antigravity 验证与交付对接 (Walkthrough)\n\n"
                "- 验证完成后，生成或更新 `walkthrough.md` Artifact，记录执行的最小构建命令、测试输出与关键改动；\n"
                "- 涉及代码符号或文件路径时，一律使用 `[basename](file:///absolute/path)` 格式生成可点击链接。\n"
            )
        elif skill == "systematic-debugging" and "排障与事实定位指引" not in body:
            body += (
                "\n\n## Antigravity 排障与事实定位指引\n\n"
                "- 排查环境配置、微服务端口与镜像问题时，优先调用 `aisk_fact_service` / `aisk_env_summary` MCP 工具获取权威事实；\n"
                "- 数据库数据或表结构比对，优先使用 `aisk_db_query` MCP 工具执行只读查询；\n"
                "- 严禁未定位根因直接盲改或包裹 try-catch；定位后先做最小验证再交付。\n"
            )
    return f"---\n{fm}\n---\n{body}"


def _managed_meta(root):
    """读端上的受管标记：`{"skills": [...], "overlaid": [...]}`；没有标记时返回 {}。

    `overlaid` 记的是「本端故意保留在制品、不等于内核版本」的技能名，
    体检据此不再把它报成「落后」——报假警比不报警更糟。
    """
    f = root / MANAGED_MARKER
    if not f.is_file():
        return {}
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError(f"无效的受管技能清单: {f}")
        for key in ("skills", "overlaid"):
            names = meta.get(key, [])
            if (not isinstance(names, list) or any(
                    not isinstance(n, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", n)
                    for n in names)):
                raise ValueError(f"无效的受管技能清单: {f}")
        return meta
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"无法读取受管技能清单: {f}") from e


def _managed_list(root):
    return _managed_meta(root).get("skills", [])


def _overlay_sources(overlay_dirs):
    """把「在制品技能目录」列表解析成 `{技能名: 目录}`。

    每个入参必须**本身就是一个技能目录**（含 SKILL.md），不接受「技能目录的父目录」：
    在制品通常来自内核自己的某个 worktree，那里的 `skills/` 躺着整条分支的全部技能，
    父目录形式会把 25 个技能一起叠加，等于静默换掉整端。一次一个，写清楚。
    """
    out = {}
    for raw in overlay_dirs or ():
        root = Path(raw).expanduser()
        if not (root / "SKILL.md").is_file():
            raise ValueError(f"叠加源不是一个技能目录（缺少 SKILL.md）: {root}")
        out[root.name] = root
    return out


def _write_skill_dir(tool, skill, source, destination):
    """把一个技能目录按端改写后写到端上（与 link 主流程同一套规则）。"""
    if destination.exists():
        for sub in RESOURCE_DIRS:
            d = destination / sub
            if d.exists():
                _remove_dir(d)
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in _payload(tool, skill, source).items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _payload(tool, skill, source):
    """分发与体检共用同一份完整资源清单。"""
    result = {"SKILL.md": render_skill(
        tool, skill, (source / "SKILL.md").read_text(encoding="utf-8")).encode("utf-8")}
    for sub in RESOURCE_DIRS:
        directory = source / sub
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    result[path.relative_to(source).as_posix()] = path.read_bytes()
    return result


def _matches_payload(destination, payload):
    actual = {"SKILL.md"} if (destination / "SKILL.md").is_file() else set()
    for sub in RESOURCE_DIRS:
        directory = destination / sub
        if directory.is_dir():
            actual.update(p.relative_to(destination).as_posix()
                          for p in directory.rglob("*") if p.is_file()
                          and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return actual == set(payload) and all(
        (destination / name).read_bytes() == data for name, data in payload.items())


def _backup_parent(raw):
    """展开备份根里的 `$AISK_HOME`，变量缺失时退回运行时根。

    启动器 `bin/aisk` 会注入 `AISK_HOME`；绕过启动器直接跑 `python3 bin/aisk link`，
    或纯内核/测试调用时它并不存在，`os.path.expandvars` 会把字面量原样留下，
    于是在**当前工作目录**建出名为 `$AISK_HOME` 的目录。退回 `runtime_root()` 与
    `profile.runtime_root()` 的口径一致，也不会把临时状态写进真实用户目录。
    """
    if "AISK_HOME" not in os.environ:
        fallback = str(profile_mod.runtime_root())
        raw = raw.replace("${AISK_HOME}", fallback).replace("$AISK_HOME", fallback)
    return Path(os.path.expandvars(raw)).expanduser()


def _backup_directory(directory, target):
    parent = _backup_parent(target.get("backup_root", "$AISK_HOME/backups/skills"))
    parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=directory.name + "-", dir=parent))
    shutil.copytree(directory, backup / directory.name, symlinks=True)
    return backup / directory.name


def _write_managed(root, names, overlaid=()):
    meta = {"skills": sorted(names), "by": "aisk link"}
    if overlaid:
        meta["overlaid"] = sorted(overlaid)
    (root / MANAGED_MARKER).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _archive_codex_shadows(root, wanted, target, dry_run, log):
    """将明确被新技能替代的旧 Codex 名称移入可恢复备份。

    这些目录不一定带历史 .aisk-managed 标记，不能用「无标记一律不碰」处理；但
    名称与替代关系是显式白名单，且只移动到 ~/.aisk/backups，绝不删除。
    """
    shadows = [old for old, new in CODEX_SHADOWS.items()
               if new in wanted and (root / old).is_dir()]
    if not shadows:
        return 0
    if dry_run:
        for old in sorted(shadows):
            log.append(f"归档旧技能 {old}（已由 {CODEX_SHADOWS[old]} 替代）")
        return len(shadows)

    backup = _backup_parent(target["backup_root"]) / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True, exist_ok=True)
    for old in sorted(shadows):
        shutil.move(str(root / old), str(backup / old))
        log.append(f"归档 {old} -> {backup / old}")
    return len(shadows)


def _remove_dir(path):
    """删除目录并确认真的删掉了：有的沙箱把删除换成静默失败还不报错，不能把"调用过删除"当成"已回收"。"""
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists():
        raise ValueError(f"删除失败，目录仍在（可能被沙箱拦截）：{path}；备份已保留，请在普通终端重新分发")


def _remove_legacy_codex_copies(target, dry_run, log):
    """仅回收旧 ~/.codex/skills 中有 .aisk-managed 标记的副本。"""
    legacy = Path(target.get("legacy_root", "")).expanduser()
    if not target.get("legacy_root") or not legacy.is_dir():
        return 0
    managed = set(_managed_list(legacy))
    copies = sorted(name for name in managed if (legacy / name).is_dir())
    if not copies:
        return 0
    if dry_run:
        for name in copies:
            log.append(f"回收旧 Codex 副本 {legacy / name}")
        return len(copies)

    for name in copies:
        saved = _backup_directory(legacy / name, target)
        _remove_dir(legacy / name)
        log.append(f"回收旧 Codex 副本 {legacy / name}（备份 {saved}）")
    marker = legacy / MANAGED_MARKER
    if marker.is_file():
        marker.unlink()
    return len(copies)


def link(tool, kernel_skills, dry_run=False, overlay_from=None):
    """分发到一个端。返回 (写入数, 回收数, 未触碰数, 日志行)。

    `overlay_from` 是若干「还在内核之外、但端上正在用」的技能目录（一次一个技能）。
    列进去的技能**不再从内核写**，改由这份在制品覆盖，并记进端上标记的 `overlaid`：
    这样 `aisk link` 不会把在制品刷回内核旧版，体检也不会把它误报成「落后」。
    """
    if tool not in TARGETS:
        raise ValueError(f"未知的端: {tool}。可用: {', '.join(TARGETS)}")
    root = Path(TARGETS[tool]["root"]).expanduser()
    log = []

    source_map = _kernel_sources(kernel_skills)
    want = {target_name(tool, canonical): source
            for canonical, source in source_map.items()}

    existing = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    previously = set(_managed_list(root))
    conflicts = (existing & set(want)) - previously
    if conflicts:
        raise ValueError("目标端存在同名非受管技能，未覆盖: " + ", ".join(sorted(conflicts)))
    overlay_sources = _overlay_sources(overlay_from)
    overlaid = {target_name(tool, s): s for s in overlay_sources
                if target_name(tool, s) in want}
    for s in overlay_sources:
        if target_name(tool, s) not in want:
            log.append(f"叠加跳过 {s}（内核没有同名技能）")
    payloads = {name: _payload(tool, name, kernel_skills / source)
                for name, source in want.items() if name not in overlaid}
    changed = {name for name, payload in payloads.items()
               if not _matches_payload(root / name, payload)}
    # 在任何写入前检查整批目标，防止某一技能的链接指向用户其他文件。
    for name in changed:
        directory = root / name
        if directory.is_symlink() or (directory.exists() and any(
                p.is_symlink() for p in directory.rglob("*"))):
            raise ValueError(f"目标技能包含符号链接，未覆盖: {directory}")
    # 只回收「我们上次写过、这次不再需要」的，端上第三方技能永远不在这个集合里
    stale = (previously - set(want)) & existing
    # 旧技能可能是被老的 sync.sh 写进去的，不在 .aisk-managed 里，
    # 靠硬编码的 RETIRED 清单显式识别（绝不按模式猜，避免误伤第三方技能）
    retired_here = RETIRED & existing if tool != "codex" else set()
    stale |= retired_here
    untouched = existing - set(want) - stale
    if tool == "codex":
        untouched -= {old for old, new in CODEX_SHADOWS.items() if new in want}

    if dry_run:
        extra_stale = 0
        if tool == "codex":
            extra_stale += _archive_codex_shadows(root, want, TARGETS[tool], True, log)
            extra_stale += _remove_legacy_codex_copies(TARGETS[tool], True, log)
        for tname, sname in sorted(overlaid.items()):
            log.append(f"叠加在制品 {tname} <- {overlay_sources[sname]}")
        return len(changed) + len(overlaid), len(stale) + extra_stale, len(untouched), log

    root.mkdir(parents=True, exist_ok=True)
    for tname, sname in want.items():
        if tname not in changed:
            continue
        ddir = root / tname
        if ddir.exists():
            saved = _backup_directory(ddir, TARGETS[tool])
            log.append(f"更新前备份 {tname} -> {saved}")
        _write_skill_dir(tool, tname, kernel_skills / sname, ddir)

    for s in sorted(stale):
        saved = _backup_directory(root / s, TARGETS[tool])
        _remove_dir(root / s)
        why = "已合并进 ops/git-flow" if s in RETIRED else "不再需要"
        log.append(f"退役 {s}（{why}；备份 {saved}）")

    # 在制品最后写：内核版本先落位，再用端上真正在用的那份覆盖
    for tname, sname in sorted(overlaid.items()):
        _write_skill_dir(tool, sname, overlay_sources[sname], root / tname)
        log.append(f"叠加在制品 {tname} <- {overlay_sources[sname]}")

    _write_managed(root, want.keys(), overlaid.keys())
    extra_stale = 0
    if tool == "codex":
        extra_stale += _archive_codex_shadows(root, want, TARGETS[tool], False, log)
        extra_stale += _remove_legacy_codex_copies(TARGETS[tool], False, log)
    return len(changed) + len(overlaid), len(stale) + extra_stale, len(untouched), log


def write_codex_plugin(kernel_skills, target_plugin_dir):
    """生成可进入 Codex 个人市场的技能插件包；不自动安装，避免与独立技能重名。"""
    root = Path(target_plugin_dir).expanduser()
    skills_dir = root / "skills"
    manifest_dir = root / ".codex-plugin"
    if skills_dir.exists():
        _remove_dir(skills_dir)
    shutil.copytree(kernel_skills, skills_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "agent-skills",
        "version": "2.1.0",
        "description": "Portable, on-demand engineering skill kernel for Codex.",
        "author": {"name": "agent-skills"},
        "skills": "./skills/",
        "interface": {
            "displayName": "Agent Skills",
            "shortDescription": "Portable engineering skills for Codex.",
            "longDescription": "On-demand engineering skills with environment facts kept outside prompt bodies.",
            "developerName": "agent-skills",
            "category": "Productivity",
            "capabilities": [],
            "defaultPrompt": "Help me use Agent Skills.",
        },
    }
    manifest_file = manifest_dir / "plugin.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root, manifest_file


def write_antigravity_plugin(manifest_meta):
    """Antigravity 要求技能挂在插件下，plugin.json 是「这是插件」的标记文件。"""
    root = Path(TARGETS["antigravity"]["root"]).expanduser().parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(manifest_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root / "plugin.json"


def write_antigravity_rules(target_plugin_dir=None):
    """为 Antigravity 插件写入原生规则 rules/AGENTS.md。

    Antigravity 在编辑或访问工作区文件时会自动加载插件 rules/ 下的规则。
    将核心编码红线、用语规范、只读安全策略作为规则下发，无需 Agent 手动查阅技能。
    """
    import sys
    plugin_dir = target_plugin_dir or Path(TARGETS["antigravity"]["root"]).expanduser().parent
    rules_dir = plugin_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    content = """# Agent Skills 工作区规则

本文件由 aisk link 生成；具体项目规则由目标仓库维护。

- 工作前读取当前工作区的 AGENT-WORKTREE.md（若有）和 AGENTS.md；
  按其中的路径、分支、验证与授权范围执行，不套用其他项目的固定值。
- Skills 提供流程；CLI/MCP 提供能力。仅调用本会话实际可用的工具；
  未连接 MCP 时可用已安装 CLI，不能假定工具名可调用。
- 查询事实先明确项目和环境。部署仓 manifest 是配置证据，不代表线上已部署；
  需要判断运行状态时另外读取当前运行证据。
- 凭据由 broker 代持，遵循项目只读与脱敏约束；工具存在不代表获准执行写操作。
- 验证与交付按当前请求和仓库规则完成。用户已授权实施时继续推进；
  只在缺少必要选择或权限时询问，不额外增加普遍适用的审批步骤。
- 后端、前端、SQL 规范按需读取对应技能与仓库规则，不在全局插件里复制一套。
"""

    rules_file = rules_dir / "AGENTS.md"
    rules_file.write_text(content.strip() + "\n", encoding="utf-8")
    return rules_file


def write_antigravity_mcp(target_plugin_dir=None):
    """为 Antigravity 插件生成 mcp_config.json，接入原生 aisk MCP 工具。"""
    import sys
    plugin_dir = target_plugin_dir or Path(TARGETS["antigravity"]["root"]).expanduser().parent
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # 确定 Python 解释器路径：优先选择 aisk venv，兜底使用当前环境 python
    is_win = sys.platform.startswith("win")
    venv_py = Path(os.environ.get("AISK_HOME") or (Path.home() / ".aisk")) / "venv" / ("Scripts" if is_win else "bin") / ("python.exe" if is_win else "python")
    python_cmd = str(venv_py) if venv_py.is_file() else sys.executable

    kernel_root = Path(__file__).resolve().parent.parent

    cfg = {
        "mcpServers": {
            "aisk": {
                "command": python_cmd,
                "args": ["-m", "engine.mcp_server"],
                "env": {
                    "PYTHONPATH": str(kernel_root),
                    "AISK_HOME": os.environ.get("AISK_HOME", ""),
                    "AISK_PRIVATE_ROOT": os.environ.get("AISK_PRIVATE_ROOT", ""),
                    "AISK_HUB_ROOT": os.environ.get("AISK_HUB_ROOT", ""),
                },
            }
        }
    }
    mcp_file = plugin_dir / "mcp_config.json"
    mcp_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mcp_file


def write_claude_plugin(manifest_meta):
    """Claude 的插件清单（可选，但有它才能带 hooks/userConfig）。"""
    root = Path(TARGETS["claude"]["root"]).expanduser().parent / "plugins" / "agent-skills"
    return None  # P1 只做技能分发，插件清单留到 P2 接凭据时一起做

def drift_report(kernel_skills):
    """各端分发状态体检：内容是否落后于内核、是否还留着已退役的旧技能。

    2026-08-27 的教训：Codex 端整整一天读的是旧内容，还留着 14 个已被取代的
    旧技能——其中 v2-prod-ops / dev-ops 那批**明确指示模型去读明文口令文件**。
    没人发现，因为 `aisk doctor` 当时只看 profile 解析，**完全不看端上状态**。
    分发是「写完就忘」的操作，没有回读就等于没有反馈回路。

    返回 [(端名, 落后数, 残留退役数, 说明), ...]。只读，不改任何东西。
    """
    source_map = _kernel_sources(kernel_skills)
    want_names = set(source_map)
    out = []
    for tool, cfg in TARGETS.items():
        root = Path(cfg["root"]).expanduser()
        if not root.is_dir():
            out.append((tool, 0, 0, "端上目录不存在（未分发过）"))
            continue
        existing = {d.name for d in root.iterdir() if d.is_dir()}
        # 端上明确标了「这份是故意保留的在制品」的技能不参与「落后」判定：
        # 否则每次体检都报同一个永远消不掉的警，人就学会无视这个检查了。
        overlaid = set(_managed_meta(root).get("overlaid", []))
        # **必须用 target_name()，不能自己重抄映射**：Claude 有改名约定、
        # WorkBuddy 有 methodology- 前缀。第一版这里只抄了改名那一半，结果
        # 把 Claude 的正式名 db-design/sec-review 报成「残留旧技能」，
        # 又把 WorkBuddy 的 4 个方法论技能报成「缺失」——两次都是假警。
        # 报假警比不报警更糟：人会开始无视这个检查。
        legit = {target_name(tool, n) for n in want_names}
        retired_left = sorted(((RETIRED | set(CODEX_SHADOWS)) & existing) - legit)

        behind = 0
        for sname in want_names:
            tname = target_name(tool, sname)
            if tname in overlaid:
                continue
            dst = root / tname / "SKILL.md"
            src = kernel_skills / source_map[sname] / "SKILL.md"
            if not src.is_file() or not dst.is_file():
                behind += 1
            elif not _matches_payload(dst.parent, _payload(tool, sname, src.parent)):
                behind += 1
        bits = []
        if behind:
            bits.append(f"{behind} 个技能落后于内核")
        if retired_left:
            bits.append("残留已退役: " + ", ".join(retired_left[:6])
                        + (" …" if len(retired_left) > 6 else ""))
        if overlaid:
            bits.append(f"{len(overlaid)} 个在制品叠加（不在内核里，体检不判落后）")
        note = ""
        if behind or retired_left:
            note = "；".join(bits) + " → 跑 aisk link"
        elif overlaid:
            note = "；".join(bits)
        out.append((tool, behind, len(retired_left), note))
    return out
