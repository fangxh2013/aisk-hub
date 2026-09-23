# -*- coding: utf-8 -*-
"""档案 `worktrees:` 一节 → 运行配置。

原则与内核一致：缺必填项报错，不猜；项目事实只来自档案（分支名、推送目标、钩子脚本、环境变量、交接单小节……）。
每个操作系统各有一份档案（数据根、仓库对象库、工具路径各写各的），这里不做 Windows 特例推断。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import profile as profile_mod
from . import names

IS_WIN = sys.platform.startswith("win")
OS_NAME = "windows" if IS_WIN else "mac"

# 工具名是通用词表，不是项目事实：提交说明、分支、任务短语里都不允许出现
DEFAULT_AI_NAMES = (
    "cursor|cursoragent|claude|anthropic|codex|chatgpt|chat[ -]?gpt|openai|workbuddy|codebuddy|antigravity|"
    "gemini|copilot|deepseek|qwen|tongyi|wenxin|erniebot|moonshot|kimi|grok|perplexity|windsurf|devin|trae|hermes"
)
DEFAULT_AI_EMAILS = r"@cursor\.com|@anthropic\.com|@openai\.com|@workbuddy\.|cursoragent@"
DEFAULT_MESSAGE_PATTERN = r"^[a-z]+(?:\([^)]+\))?: \S"
# WorkBuddy 桌面版与 AI 版各有自己的规则文件（`~/.workbuddy/MEMORY.md` /
# `~/.workbuddy-ai/MEMORY.md`），两端都要注入规则块——只列桌面版会让 AI 端拿不到 aisk 约定。
DEFAULT_RULE_FILES = ["~/.claude/CLAUDE.md", "~/.codex/AGENTS.md", "~/.gemini/GEMINI.md",
                      "~/.workbuddy/MEMORY.md", "~/.workbuddy-ai/MEMORY.md"]
DEFAULT_HANDOFF_SECTIONS = ["改了什么", "验证到哪一步", "未验证的风险", "数据库变更", "配置变更", "需要在共享环境验证的点"]


class WtError(Exception):
    """可预期的失败：打印中文说明，退出码 1。"""


@dataclass
class RepoCfg:
    alias: str
    path: Path
    profile_repo: str
    trunk: str
    push_branch: str
    promote: str
    anchors: list
    gate: dict
    hooks_required: bool = False
    hooks_path: str = ""
    mac_source: str = ""
    promote_hint: str = ""
    # 只登记位置、不检出的受保护分支：位置照样审计，但不为它常驻一个工作区
    audited: list = field(default_factory=list)
    # Windows 显式集成模式下，任务裸仓与可写的集成工作区必须分开。
    integration_path: Path = None


@dataclass
class WtConfig:
    profile_name: str
    profile_path: Path
    os: str
    data_root: Path
    hub: Path
    integration: str
    protected: list
    task_prefix: str
    branch_prefix: str
    push_block_prefixes: list
    idle_minutes: int
    claimable_minutes: int
    port_base: int
    port_block: int
    quota_active: int
    quota_builds: int
    sensitive_paths: list
    ai_re: "re.Pattern"
    ai_email_re: "re.Pattern"
    message_re: "re.Pattern"
    message_hint: str
    max_msg_chars: int
    repos: dict
    repo_order: list
    extra_guards: list
    task_env: dict = field(default_factory=dict)
    forbidden_commands: dict = field(default_factory=dict)
    task_rules: list = field(default_factory=list)
    handoff_sections: list = field(default_factory=lambda: list(DEFAULT_HANDOFF_SECTIONS))
    legacy_root: Path = None
    legacy_exclude: list = field(default_factory=list)
    legacy_admin_globs: list = field(default_factory=list)
    rule_files: list = field(default_factory=list)
    retention: dict = field(default_factory=dict)
    windows_integration: bool = False
    raw: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- 路径
    @property
    def tasks_dir(self):
        return self.data_root / "tasks"

    @property
    def anchors_dir(self):
        return self.data_root / "anchors"

    @property
    def state_dir(self):
        return self.data_root / "state"

    @property
    def task_state_dir(self):
        return self.state_dir / "tasks"

    @property
    def locks_dir(self):
        return self.state_dir / "locks"

    @property
    def refs_dir(self):
        return self.state_dir / "refs"

    @property
    def archive_dir(self):
        return self.state_dir / "archive"

    @property
    def logs_dir(self):
        return self.data_root / "logs"

    @property
    def salvage_dir(self):
        return self.data_root / "salvage"

    @property
    def board_file(self):
        return self.data_root / names.BOARD

    @property
    def win_state_dir(self):
        return self.hub / "state" / "win"

    def repo(self, alias):
        if alias not in self.repos:
            raise WtError(f"未知仓库别名 {alias}，可用：{', '.join(self.repo_order)}")
        return self.repos[alias]

    def integration_repo(self, alias):
        """返回实际执行 land/promote 的工作区；Windows 默认没有此权限。"""
        repo = self.repo(alias)
        if self.os != "windows":
            return repo.path
        if not self.windows_integration:
            raise WtError("Windows 默认不能执行集成操作；请在 worktrees.windows_integration 显式开启")
        if not repo.integration_path:
            raise WtError(f"Windows 集成模式缺少 worktrees.repos.{alias}.integration_repo")
        return repo.integration_path

    def anchor_path(self, alias, name):
        return self.anchors_dir / alias / name

    def ports_for(self, block_index):
        base = self.port_base + block_index * self.port_block
        return {"base": base, "web": base, "gateway": base + 1,
                "services_from": base + 2, "services_to": base + self.port_block - 1}

    def task_branch(self, name):
        return f"{self.branch_prefix}{name}"


def _req(d, key, where):
    if key not in d or d[key] in (None, ""):
        raise WtError(f"档案 {where} 缺少必填项 {key}")
    return d[key]


def _as_list(v, where):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    raise WtError(f"档案 {where} 应为列表")


def _as_map(v, where):
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    raise WtError(f"档案 {where} 应为映射")


def build_config(prof, os_name=None):
    """把已加载的档案字典转换为 WtConfig。"""
    os_name = os_name or OS_NAME
    wt = prof.get("worktrees")
    if not isinstance(wt, dict):
        raise WtError(f"档案 {prof.get('project')} 没有 worktrees 一节（见 WORKTREE.md §7）")
    expand = profile_mod._expand
    data_root = expand(_req(wt, "data_root", "worktrees"))
    integration = str(_req(wt, "integration_branch", "worktrees"))
    protected = _as_list(_req(wt, "protected", "worktrees"), "worktrees.protected")
    if integration not in protected:
        raise WtError("worktrees.protected 必须包含集成分支本身")
    lease = _as_map(wt.get("lease"), "worktrees.lease")
    ports = _as_map(wt.get("ports"), "worktrees.ports")
    quotas = _as_map(wt.get("quotas"), "worktrees.quotas")
    commit = _as_map(wt.get("commit"), "worktrees.commit")
    legacy = _as_map(wt.get("legacy"), "worktrees.legacy")
    idle = int(lease.get("idle_minutes", 30))
    claimable = int(lease.get("claimable_minutes", 120))
    if not 0 < idle < claimable:
        raise WtError("worktrees.lease 需满足 0 < idle_minutes < claimable_minutes")
    hub = wt.get("hub")
    hub = expand(hub) if hub else data_root / "hub"
    branch_prefix = str(wt.get("branch_prefix", "ai/"))
    if branch_prefix and not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/", branch_prefix):
        raise WtError("worktrees.branch_prefix 应形如 ai/ 或 agents/tasks/（以 / 结尾，不含空格）")

    repos_decl = _as_map(_req(wt, "repos", "worktrees"), "worktrees.repos")
    profile_repos = prof.get("repos") or {}
    windows_integration = wt.get("windows_integration", False)
    if isinstance(windows_integration, dict):
        windows_integration = windows_integration.get("enabled", False)
    if not isinstance(windows_integration, bool):
        raise WtError("worktrees.windows_integration 必须是 true 或 false")
    repos = {}
    for alias, rd in repos_decl.items():
        rd = _as_map(rd, f"worktrees.repos.{alias}")
        where = f"worktrees.repos.{alias}"
        profile_repo = str(rd.get("profile_repo", alias))
        promote = str(_req(rd, "promote", where))
        if promote not in ("ff-trunk", "push-only"):
            raise WtError(f"{where}.promote 只支持 ff-trunk / push-only")
        if os_name == "windows":
            path = data_root / "repos" / f"{alias}.git"
        else:
            if profile_repo not in profile_repos:
                raise WtError(f"{where}.profile_repo={profile_repo} 在档案 repos 中不存在")
            path = expand(profile_repos[profile_repo])
        gate = rd.get("gate") or {"kind": "none"}
        if isinstance(gate, str):
            gate = {"kind": gate}
        gate = _as_map(gate, f"{where}.gate")
        if gate.get("kind", "none") not in ("none", "maven-modules", "npm-build", "command"):
            raise WtError(f"{where}.gate.kind 不支持：{gate.get('kind')}")
        integration_key = str(rd.get("integration_repo") or "")
        if os_name == "windows" and windows_integration and not integration_key:
            raise WtError(f"{where} 在 Windows 集成模式下必须声明 integration_repo")
        if os_name == "windows" and windows_integration and integration_key not in profile_repos:
            raise WtError(f"{where}.integration_repo={integration_key} 不在档案 repos 中")
        repos[alias] = RepoCfg(
            alias=alias, path=path, profile_repo=profile_repo,
            trunk=str(_req(rd, "trunk", where)), push_branch=str(_req(rd, "push_branch", where)),
            promote=promote, anchors=_as_list(rd.get("anchors"), f"{where}.anchors"), gate=gate,
            hooks_required=bool(rd.get("hooks_required", False)), hooks_path=str(rd.get("hooks_path") or ""),
            mac_source=str(rd.get("mac_source") or ""), promote_hint=str(rd.get("promote_hint") or ""),
            audited=_as_list(rd.get("audited"), f"{where}.audited"),
            integration_path=expand(profile_repos[integration_key]) if integration_key else None,
        )
        both = set(repos[alias].anchors) & set(repos[alias].audited)
        if both:
            raise WtError(f"{where}: {'、'.join(sorted(both))} 同时写在 anchors 与 audited 里，二选一"
                          f"（anchors 会检出一个工作区，audited 只登记位置）")
    order = _as_list(wt.get("repo_order"), "worktrees.repo_order") or list(repos)
    for a in order:
        if a not in repos:
            raise WtError(f"worktrees.repo_order 含未声明的仓库 {a}")
    if os_name == "windows" and not wt.get("hub"):
        raise WtError("Windows 档案必须声明 worktrees.hub（mac 侧 hub 的共享路径）")
    ret = _as_map(wt.get("retention"), "worktrees.retention")
    retention = {
        "promoted_hours": int(ret.get("promoted_hours", 72)),
        "archive_days": int(ret.get("archive_days", 30)),
        "disk_budget_gb": float(ret.get("disk_budget_gb", 10)),
        "build_dirs": _as_list(ret.get("build_dirs"), "worktrees.retention.build_dirs")
        or ["node_modules", "target", "dist", ".next", "build"],
    }
    if retention["promoted_hours"] < 1:
        raise WtError("worktrees.retention.promoted_hours 至少 1 小时")
    task_env = {str(k): str(v) for k, v in _as_map(wt.get("task_env"), "worktrees.task_env").items()}
    for key in task_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise WtError(f"worktrees.task_env 的键 {key} 不是合法的环境变量名")
    forbidden = {str(k): str(v) for k, v in _as_map(wt.get("forbidden_commands"), "worktrees.forbidden_commands").items()}
    for pattern in forbidden:
        try:
            re.compile(pattern)
        except re.error as e:
            raise WtError(f"worktrees.forbidden_commands 的正则 {pattern!r} 无效：{e}") from e
    sections = _as_list(wt.get("handoff_sections"), "worktrees.handoff_sections") or list(DEFAULT_HANDOFF_SECTIONS)
    # 阶段 2 档案使用扁平键；迁移期同时读取新 legacy 映射。
    legacy_root = legacy.get("root", wt.get("legacy_root"))
    prefixes = wt.get("push_block_prefixes")
    if prefixes is None:
        prefixes = wt.get("push_block_prefix")
    return WtConfig(
        profile_name=str(prof.get("project")), profile_path=Path(prof.get("_path", "")), os=os_name,
        data_root=data_root, hub=hub, integration=integration, protected=protected,
        task_prefix=str(wt.get("task_prefix") or ("W" if os_name == "windows" else "T")),
        branch_prefix=branch_prefix,
        push_block_prefixes=_as_list(prefixes, "worktrees.push_block_prefixes"),
        idle_minutes=idle, claimable_minutes=claimable,
        port_base=int(ports.get("base", 20000)), port_block=int(ports.get("block", 10)),
        quota_active=int(quotas.get("active", 8)), quota_builds=int(quotas.get("builds", 2)),
        sensitive_paths=_as_list(wt.get("sensitive_paths"), "worktrees.sensitive_paths"),
        ai_re=re.compile(str(commit.get("ai_names") or DEFAULT_AI_NAMES), re.I),
        ai_email_re=re.compile(DEFAULT_AI_EMAILS, re.I),
        message_re=re.compile(str(commit.get("pattern") or DEFAULT_MESSAGE_PATTERN)),
        message_hint=str(commit.get("hint") or "type: 说明 或 type(scope): 说明"),
        max_msg_chars=int(commit.get("max_chars", 140)),
        repos=repos, repo_order=order,
        extra_guards=_as_list(wt.get("extra_guards"), "worktrees.extra_guards"),
        task_env=task_env, forbidden_commands=forbidden,
        task_rules=_as_list(wt.get("task_rules"), "worktrees.task_rules"), handoff_sections=sections,
        legacy_root=expand(legacy_root) if legacy_root else None,
        legacy_exclude=_as_list(legacy.get("exclude", wt.get("legacy_exclude")), "worktrees.legacy.exclude"),
        legacy_admin_globs=_as_list(legacy.get("admin_globs", wt.get("legacy_admin_globs")), "worktrees.legacy.admin_globs"),
        rule_files=_as_list(wt.get("rule_files"), "worktrees.rule_files") or list(DEFAULT_RULE_FILES),
        retention=retention,
        windows_integration=windows_integration,
        raw=wt,
    )


def resolve_profile(explicit=None, start=None):
    """显式/环境变量 > 当前目录位于某档案的数据根 > 内核通用的按仓库反查。"""
    explicit = explicit or os.environ.get("AISK_PROFILE")
    if explicit:
        return profile_mod.resolve(explicit, start)
    cwd = Path(start or Path.cwd()).resolve()
    hits = []
    for p in profile_mod.list_profiles():
        try:
            prof = profile_mod.load(p)
        except Exception:  # noqa: BLE001  坏档案不该拖垮别的项目
            continue
        wt = prof.get("worktrees")
        if isinstance(wt, dict) and wt.get("data_root"):
            root = profile_mod._expand(wt["data_root"]).resolve()
            if cwd == root or root in cwd.parents:
                hits.append((p, prof))
    if len(hits) == 1:
        return hits[0][1], f"当前目录位于 {hits[0][0].stem} 的 worktrees.data_root"
    if len(hits) > 1:
        raise profile_mod.ProfileError("当前目录同时位于多个档案的 data_root，拒绝猜测，请用 --profile 指定")
    return profile_mod.resolve(None, start)


def load_config(explicit=None, start=None):
    prof, why = resolve_profile(explicit, start)
    return build_config(prof), why
