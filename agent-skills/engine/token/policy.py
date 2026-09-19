"""Deterministic safety policy for token/context routing."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

MODES = ("lite", "standard", "deep", "emergency")


@dataclass(frozen=True)
class Trigger:
    rule: str
    severity: str
    matches: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity,
                "matches": list(self.matches), "reason": self.reason}


_PATTERNS = (
    ("database", r"数据库|\bdatabase\b|\bdb\b|\bsql\b|\bmysql\b|\bpostgres(?:ql)?\b", "database work"),
    ("permission", r"权限|\bpermissions?\b|\bauth(?:entication|orization)?\b|\brbac\b|\bcredential\b", "permission or credential work"),
    ("privacy", r"隐私|\bprivacy\b|\bpii\b|\bpersonal data\b|\bsecrets?\b|\bsensitive\b", "privacy or sensitive data"),
    ("production", r"生产|线上|\bproduction\b|\bprod\b|\blive\b|\bincident\b|\boutage\b", "production or incident work"),
    ("git_push", r"git\s+push|推送|\bpush\s+to\s+(?:main|master|prod(?:uction)?)\b", "repository transfer"),
    ("git_merge", r"git\s+merge|合并|\bmerge\s+(?:into|to)\s+(?:main|master|prod(?:uction)?)\b", "branch integration"),
)
_COMPILED = tuple((name, re.compile(pattern, re.I), reason)
                 for name, pattern, reason in _PATTERNS)


def _task_text(task: Any) -> str:
    if isinstance(task, str):
        return task
    if isinstance(task, dict):
        return " ".join(str(task.get(k, "")) for k in ("title", "description", "task", "action"))
    return str(task or "")


def detect_hard_triggers(task: Any) -> list[Trigger]:
    text = _task_text(task)
    result = []
    for name, pattern, reason in _COMPILED:
        matches = tuple(dict.fromkeys(m.group(0) for m in pattern.finditer(text)))
        if matches:
            result.append(Trigger(name, "hard", matches, reason))
    return result


def _task_value(task: Any, *keys: str) -> Any:
    if isinstance(task, dict):
        for key in keys:
            if key in task:
                return task[key]
    for key in keys:
        if hasattr(task, key):
            return getattr(task, key)
    return None


def evaluate_policy(task: Any, *, complexity: Any = None,
                    required_sources: Iterable[str] = (),
                    available_sources: Iterable[str] = (),
                    conflicts: Iterable[str] = ()) -> dict[str, Any]:
    complexity = complexity if complexity is not None else _task_value(task, "complexity", "task_complexity")
    uncertain = complexity in (None, "", "uncertain", "unknown", "ambiguous")
    if isinstance(task, dict):
        uncertain = uncertain or bool(task.get("complexity_uncertain"))
    required = {str(x) for x in required_sources}
    available = {str(x) for x in available_sources}
    missing = sorted(required - available)
    conflicts = sorted({str(x) for x in conflicts if str(x)})
    if isinstance(task, dict):
        missing = sorted(set(missing) | {str(x) for x in (task.get("missing_context") or ())})
        conflicts = sorted(set(conflicts) | {str(x) for x in (task.get("conflicts") or ())})
    hard = detect_hard_triggers(task)
    if complexity in ("emergency", "critical"):
        mode = "emergency"
    elif hard or complexity in ("complex", "deep"):
        mode = "deep"
    elif complexity in ("simple", "lite"):
        mode = "lite"
    else:
        mode = "standard"
    reasons = []
    if uncertain:
        reasons.append("task complexity is uncertain")
        hard.append(Trigger("complexity_uncertain", "fail-to-full", reason=reasons[-1]))
    if conflicts:
        reasons.append("policy or task context conflict")
        hard.append(Trigger("rule_conflict", "fail-to-full", tuple(conflicts), reasons[-1]))
    if missing:
        reasons.append("required context is missing")
        hard.append(Trigger("missing_required_context", "fail-to-full", tuple(missing), reasons[-1]))
    return {"mode": mode, "triggers": [x.as_dict() for x in hard],
            "fail_to_full": bool(reasons), "fallback_reason": "; ".join(reasons) or None,
            "missing_context": missing, "conflicts": conflicts}


def route(task: Any, **kwargs: Any) -> dict[str, Any]:
    return evaluate_policy(task, **kwargs)
