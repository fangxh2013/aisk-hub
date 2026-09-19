# -*- coding: utf-8 -*-
"""Resolve legacy skill names to public canonical skill entries.

The router is filesystem based and never creates aliases with symbolic links.
The migration map is optional metadata; the built-in route table keeps
compatibility working when a checkout does not carry that map.  Only names and
paths are exposed by this module, so project-specific migration prose does not
enter the public loading path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import miniyaml


CANONICAL_SKILLS = (
    "web-engineering",
    "backend-engineering",
    "ops-workbench",
    "engineering-discipline",
    "security",
    "ai-worktree",
    "dev-release",
    "prod-log-analysis",
    "db-workbench",
)

DEFAULT_ALIASES = {
    "dev-web-code": "web-engineering",
    "element-plus-patterns": "web-engineering",
    "vue3-crud-page": "web-engineering",
    "state-management": "web-engineering",
    "frontend-review": "web-engineering",
    "frontend-testing": "web-engineering",
    "dev-code": "backend-engineering",
    "api-design": "backend-engineering",
    "ruoyi-module-scaffold": "backend-engineering",
    "backend-testing": "backend-engineering",
    "ops": "ops-workbench",
    "k3s-deployment": "ops-workbench",
    "docker-build": "ops-workbench",
    "jenkins-ci": "ops-workbench",
    "brainstorming": "engineering-discipline",
    "change-safety": "engineering-discipline",
    "systematic-debugging": "engineering-discipline",
    "pre-commit-review": "engineering-discipline",
    "dev-rules": "engineering-discipline",
    "git-flow": "engineering-discipline",
    "security-review": "security",
    "sec-review": "security",
    "ai-worktree": "ai-worktree",
    "dev-release": "dev-release",
    "prod-log-analysis": "prod-log-analysis",
    "database-design": "db-workbench",
    "db-design": "db-workbench",
    "db-schema-backup": "db-workbench",
}


class SkillRouterError(Exception):
    """Raised when a route or canonical skill cannot be loaded safely."""


class SkillRouter:
    """Logical alias router with canonical file loading and validation."""

    def __init__(self, migration_map_path: Optional[Path] = None):
        if migration_map_path is None:
            migration_map_path = Path(__file__).resolve().parents[2] / "spec" / "skill-migration-map.yaml"
        self.map_path = Path(migration_map_path)
        self._aliases: dict[str, str] = {name: name for name in CANONICAL_SKILLS}
        self._aliases.update(DEFAULT_ALIASES)
        self._load()

    def _load(self):
        """Load route names from the optional map without exposing its prose."""
        if not self.map_path.exists():
            return
        try:
            data = miniyaml.load_file(self.map_path)
        except Exception as exc:  # noqa: BLE001
            raise SkillRouterError(f"迁移规范无法解析: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
            raise SkillRouterError("迁移规范缺少 skills 映射")

        discovered: dict[str, str] = {}
        for canonical_name, entry in data["skills"].items():
            if not isinstance(canonical_name, str) or not canonical_name.strip():
                raise SkillRouterError("迁移规范包含空技能名")
            if not isinstance(entry, dict):
                raise SkillRouterError(f"技能映射必须是对象: {canonical_name}")
            aliases = list(entry.get("aliases") or []) + list(entry.get("legacy_sources") or [])
            discovered[canonical_name] = canonical_name
            for alias in aliases:
                if not isinstance(alias, str) or not alias.strip():
                    raise SkillRouterError(f"技能映射包含非法别名: {canonical_name}")
                old = discovered.get(alias)
                if old and old != canonical_name:
                    raise SkillRouterError(f"别名冲突: {alias} 已指向 {old}，又尝试指向 {canonical_name}")
                discovered[alias] = canonical_name
        if discovered:
            self._aliases = discovered

    def resolve(self, skill_name: str) -> str:
        """Return the canonical name, or the cleaned input if unknown."""
        cleaned = str(skill_name).strip()
        return self._aliases.get(cleaned, cleaned)

    def require(self, skill_name: str) -> str:
        target = self.resolve(skill_name)
        if target not in self.canonical_names():
            raise SkillRouterError(f"未注册技能或别名: {skill_name}")
        return target

    def is_alias(self, skill_name: str) -> bool:
        cleaned = str(skill_name).strip()
        return cleaned in self._aliases and self._aliases[cleaned] != cleaned

    def canonical_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, target in self._aliases.items() if name == target))

    def aliases_for(self, skill_name: str) -> tuple[str, ...]:
        target = self.require(skill_name)
        return tuple(sorted(name for name, mapped in self._aliases.items()
                            if mapped == target and name != target))

    def get_skill_info(self, skill_name: str) -> Optional[dict]:
        """Return route-only metadata; migration prose is never returned."""
        try:
            target = self.require(skill_name)
        except SkillRouterError:
            return None
        return {"name": target, "aliases": list(self.aliases_for(target))}

    def skill_path(self, skill_name: str, skills_root: Optional[Path] = None) -> Path:
        target = self.require(skill_name)
        root = Path(skills_root) if skills_root is not None else Path(__file__).resolve().parent.parent / "skills"
        directory = root / target
        path = directory / "SKILL.md"
        if directory.is_symlink() or path.is_symlink():
            raise SkillRouterError(f"技能目录禁止使用符号链接: {directory}")
        if not path.is_file():
            raise SkillRouterError(f"canonical 技能缺少 SKILL.md: {target}")
        return path

    def load(self, skill_name: str, skills_root: Optional[Path] = None) -> str:
        """Load the canonical SKILL.md selected by a legacy or canonical name."""
        try:
            return self.skill_path(skill_name, skills_root).read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillRouterError(f"无法读取技能 {skill_name}: {exc}") from exc

    load_skill = load

    def inspect(self, skill_name: str, skills_root: Optional[Path] = None) -> dict:
        target = self.require(skill_name)
        path = self.skill_path(target, skills_root)
        text = path.read_text(encoding="utf-8")
        references = sorted(str(p.relative_to(path.parent).parent)
                           for p in path.parent.glob("references/*") if p.is_file())
        return {
            "name": target,
            "requested": str(skill_name).strip(),
            "path": str(path),
            "lines": len(text.splitlines()),
            "aliases": list(self.aliases_for(target)),
            "references": references,
        }

    def check(self, skills_root: Optional[Path] = None) -> list[str]:
        """Return deterministic validation errors for all canonical entries."""
        root = Path(skills_root) if skills_root is not None else Path(__file__).resolve().parent.parent / "skills"
        errors: list[str] = []
        for target in self.canonical_names():
            directory = root / target
            path = directory / "SKILL.md"
            if directory.is_symlink() or path.is_symlink():
                errors.append(f"{target}: 禁止符号链接")
                continue
            if not path.is_file():
                errors.append(f"{target}: 缺少 SKILL.md")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{target}: 无法读取 ({exc})")
                continue
            if len(text.splitlines()) >= 500:
                errors.append(f"{target}: SKILL.md 必须少于 500 行")
            if not text.startswith("---\n") or "\nname:" not in text:
                errors.append(f"{target}: 缺少 frontmatter")
            elif f"\nname: {target}\n" not in text:
                errors.append(f"{target}: frontmatter name 不匹配")
            if "/" in target or target.startswith("."):
                errors.append(f"{target}: 名称不是安全 canonical 名称")
        return errors

    def all_aliases(self) -> dict[str, str]:
        return dict(self._aliases)
