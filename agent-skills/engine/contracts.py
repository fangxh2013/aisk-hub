"""Machine-checkable contracts for migration, overlays and evidence.

This module intentionally uses the repository's dependency-free YAML subset.  The
public kernel must be able to validate its own contracts before optional packages
or private profiles are installed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import miniyaml


class ContractError(ValueError):
    """A contract is invalid or cannot be proven from the current tree."""


ACTORS = {"codex", "claude", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human"}
AVAILABILITY = {"live", "offline_cached", "offline_snapshot", "offline_unavailable"}
EVENTS = {
    "task.created", "task.claimed", "task.heartbeat", "task.noted", "task.checked",
    "task.ready", "task.landed", "task.promoted", "task.released", "task.aborted",
    "task.conflicted", "task.expired", "task.paused",
}
REPO_ALIASES = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PATH_VALUE = re.compile(r"^(?:profiles|rules|skills)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
SECRET_VALUE = re.compile(r"(?i)(?:^|\b)(?:curl|wget|python|sh|bash|powershell|cmd|exec|eval)\b|\$\(|`|\n|\r")


def _root(root):
    return Path(root).resolve()


def load_yaml(root, name):
    path = _root(root) / "spec" / name
    if not path.is_file():
        raise ContractError(f"缺少规范文件: {path}")
    data = miniyaml.load_file(path)
    if not isinstance(data, dict):
        raise ContractError(f"规范顶层必须是映射: {path}")
    return data


def load_json(root, name):
    path = _root(root) / "spec" / name
    if not path.is_file():
        raise ContractError(f"缺少规范文件: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"JSON 规范无法读取: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"JSON 规范顶层必须是对象: {path}")
    return data


def _required(mapping, keys, where):
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ContractError(f"{where} 缺少字段: {', '.join(missing)}")


def _ensure_string(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} 必须是非空字符串")


def skill_inventory(root):
    directory = _root(root) / "agent-skills" / "skills"
    if not directory.is_dir():
        raise ContractError(f"技能目录不存在: {directory}")
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def validate_skill_migration(root):
    data = load_yaml(root, "skill-migration-map.yaml")
    _required(data, ("version", "strategy", "skills"), "skill-migration-map")
    skills = data["skills"]
    if not isinstance(skills, dict) or not skills:
        raise ContractError("skill-migration-map.skills 必须是非空映射")
    actual = set(skill_inventory(root))
    mapped = []
    alias_owner = {}
    for target, entry in skills.items():
        _ensure_string(target, "技能目标名")
        if not isinstance(entry, dict):
            raise ContractError(f"{target} 映射必须是对象")
        _required(entry, ("legacy_sources", "aliases", "retained_capabilities", "gates"), target)
        legacy = entry["legacy_sources"]
        aliases = entry["aliases"]
        if not isinstance(legacy, list) or not legacy or not all(isinstance(x, str) for x in legacy):
            raise ContractError(f"{target}.legacy_sources 必须是非空字符串列表")
        if not isinstance(aliases, list) or not aliases or not all(isinstance(x, str) for x in aliases):
            raise ContractError(f"{target}.aliases 必须是非空字符串列表")
        if not isinstance(entry["retained_capabilities"], list) or not entry["retained_capabilities"]:
            raise ContractError(f"{target}.retained_capabilities 不能为空")
        if not isinstance(entry["gates"], list) or not entry["gates"]:
            raise ContractError(f"{target}.gates 不能为空")
        for old in legacy:
            mapped.append(old)
        for alias in aliases:
            previous = alias_owner.setdefault(alias, target)
            if previous != target:
                raise ContractError(f"技能别名冲突: {alias} -> {previous}/{target}")
    canonical = set(skills)
    if actual != canonical:
        missing = sorted(canonical - actual)
        unknown = sorted(actual - canonical)
        raise ContractError(f"技能迁移不完整: missing={missing}, unknown={unknown}")
    if len(mapped) != len(set(mapped)):
        raise ContractError("同一个旧技能被多个目标重复归属")
    if "security" not in skills:
        raise ContractError("必须保留独立 security 技能或提供等价强制门禁")
    return {"legacy_count": len(actual), "target_count": len(skills), "alias_count": len(alias_owner)}


def resolve_skill_alias(root, name):
    data = load_yaml(root, "skill-migration-map.yaml")
    for target, entry in data["skills"].items():
        if name == target or name in (entry.get("aliases") or []):
            return target
    raise ContractError(f"未注册技能或别名: {name}")


def validate_overlay(manifest):
    if not isinstance(manifest, dict):
        raise ContractError("Overlay 必须是对象")
    _required(manifest, ("version", "type", "public_kernel", "skill_slots"), "Overlay")
    if manifest["version"] != 2 or manifest["type"] != "aisk-private-overlay":
        raise ContractError("Overlay 只支持 version=2/type=aisk-private-overlay")
    kernel = manifest["public_kernel"]
    if not isinstance(kernel, dict):
        raise ContractError("Overlay.public_kernel 必须是对象")
    _required(kernel, ("repository", "required_version", "content_digest"), "Overlay.public_kernel")
    _ensure_string(kernel["repository"], "Overlay.public_kernel.repository")
    if not re.match(r"^>=[0-9]+\.[0-9]+\.[0-9]+$", str(kernel["required_version"] or "")):
        raise ContractError("Overlay.public_kernel.required_version 必须是 >=x.y.z")
    digest = str(kernel["content_digest"])
    if digest != "<placeholder>" and not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ContractError("Overlay.public_kernel.content_digest 必须是 sha256 或明确 placeholder")
    for key in ("profiles", "project_rules", "custom_skills"):
        values = manifest.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and PATH_VALUE.match(v) for v in values):
            raise ContractError(f"Overlay.{key} 含非法相对路径")
        if any(".." in Path(v).parts for v in values):
            raise ContractError(f"Overlay.{key} 禁止路径穿越")
    slots = manifest["skill_slots"]
    if not isinstance(slots, dict):
        raise ContractError("Overlay.skill_slots 必须是对象")
    allowed = {
        "backend-engineering": {"package_prefix", "module_prefix", "common_module", "api_module"},
        "web-engineering": {"menu_icon_prefix", "permission_directive", "api_base_url_env"},
        "ops-workbench": {"k8s_cluster_type", "nacos_nodeport"},
    }
    for skill, values in slots.items():
        if skill not in allowed:
            raise ContractError(f"unknown skill slot: {skill}")
        if not isinstance(values, dict):
            raise ContractError(f"Overlay.skill_slots.{skill} 必须是对象")
        unknown = set(values) - allowed[skill]
        if unknown:
            raise ContractError(f"{skill} 未授权插槽: {', '.join(sorted(unknown))}")
        for key, value in values.items():
            if not isinstance(value, (str, int, bool)) or isinstance(value, (bytes, float)):
                raise ContractError(f"{skill}.{key} 只能是字符串、整数或布尔值")
            if isinstance(value, str) and SECRET_VALUE.search(value):
                raise ContractError(f"{skill}.{key} 含可执行内容或换行")
    return True


def overlay_digest(manifest):
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_overlay_lock(manifest, path, *, kernel_commit="", generated_at=""):
    validate_overlay(manifest)
    lock = {
        "lock_version": 1,
        "kernel_commit": kernel_commit,
        "overlay_digest": overlay_digest(manifest),
        "generated_at": generated_at,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lock


def validate_adapter(data):
    actors = data.get("actors") if isinstance(data, dict) else None
    if not isinstance(actors, dict) or set(actors) != ACTORS:
        raise ContractError(f"adapter actors 必须精确覆盖: {sorted(ACTORS)}")
    for actor, entry in actors.items():
        _required(entry, ("runtime_type", "capabilities", "trust_level"), f"adapter.{actor}")
        if not isinstance(entry["capabilities"], dict):
            raise ContractError(f"adapter.{actor}.capabilities 必须是对象")
    dialog = data.get("dialog_protocol") or {}
    _required(dialog, ("title_format", "fallback_no_model", "required_fields", "headless_fallback", "ci_automated_behavior"), "dialog_protocol")
    for field in ("action", "tool", "task_id"):
        if field not in dialog["required_fields"]:
            raise ContractError(f"dialog_protocol 缺少必填字段: {field}")
    if "{tool}" not in dialog["title_format"] or "{task_id}" not in dialog["title_format"]:
        raise ContractError("弹窗标题必须包含工具名和任务号")
    return {"actor_count": len(actors)}


def validate_coordination_specs(root):
    event = load_json(root, "coordination-event.schema.json")
    payloads = load_json(root, "event-payloads.schema.json")
    machine = load_yaml(root, "state-machine.yaml")
    event_types = set(event.get("properties", {}).get("event_type", {}).get("enum", []))
    if not EVENTS <= event_types:
        raise ContractError(f"事件枚举缺失: {sorted(EVENTS - event_types)}")
    definitions = payloads.get("definitions") or payloads.get("$defs") or {}
    required_payload_names = {
        "TaskCreatedPayload", "TaskClaimedPayload", "TaskHeartbeatPayload", "TaskNotedPayload", "TaskCheckedPayload",
        "TaskReadyPayload", "TaskLandedPayload", "TaskPromotedPayload", "TaskConflictedPayload",
        "TaskExpiredPayload", "TaskPausedPayload", "TaskReleasedPayload", "TaskAbortedPayload",
    }
    if not required_payload_names <= set(definitions):
        raise ContractError(f"事件 payload 定义缺失: {sorted(required_payload_names - set(definitions))}")
    transition_events = {str(item.get("event")) for item in machine.get("transitions", []) if isinstance(item, dict)}
    if not transition_events <= event_types:
        raise ContractError(f"状态机引用未知事件: {sorted(transition_events - event_types)}")
    return {"event_count": len(event_types), "transition_count": len(machine.get("transitions", []))}


def validate_mcp_response(response):
    if not isinstance(response, dict):
        raise ContractError("MCP response 必须是对象")
    _required(response, ("ok", "source", "data_availability", "as_of", "is_live", "redacted", "data"), "MCP response")
    if response["data_availability"] not in AVAILABILITY:
        raise ContractError("未知 MCP data_availability")
    if response["is_live"] != (response["data_availability"] == "live"):
        raise ContractError("MCP is_live 与 data_availability 不一致")
    if response["data_availability"] == "offline_unavailable" and response["ok"]:
        raise ContractError("offline_unavailable 不得报告 ok=true")
    return True


def validate_report(report):
    _required(report, ("report_id", "report_type", "run_id", "git_commit", "worktree_dirty", "command", "started_at", "finished_at", "duration_ms", "offline_validated", "passed", "score", "checks", "input_digest", "spec_digest", "content_digest"), "evidence report")
    if not isinstance(report["checks"], list) or not report["checks"]:
        raise ContractError("evidence report.checks 不能为空")
    if not isinstance(report["score"], (int, float)) or not 0 <= report["score"] <= 100:
        raise ContractError("evidence report.score 必须在 0..100")
    for check in report["checks"]:
        _required(check, ("check_id", "command", "exit_code", "duration_ms", "stdout_digest", "stderr_digest", "passed", "details"), "evidence check")
    return True


def validate_token_audit(audit):
    """Validate the public shape of the deterministic Token audit."""
    _required(audit, ("passed", "budgets", "files", "selected_files",
                      "selected_skill_tokens", "estimated_task_context_tokens",
                      "duplicate_line_ratio", "oversized_files"), "token audit")
    if not isinstance(audit["files"], list) or not isinstance(audit["selected_files"], list):
        raise ContractError("token audit files/selected_files 必须是列表")
    if not 0 <= float(audit["duplicate_line_ratio"]) <= 1:
        raise ContractError("token audit duplicate_line_ratio 必须在 0..1")
    for item in audit["files"]:
        _required(item, ("path", "characters", "estimated_tokens", "sha256"), "token audit file")
        if not re.fullmatch(r"[a-f0-9]{64}", str(item["sha256"])):
            raise ContractError("token audit 文件摘要必须是 sha256")
    return True
