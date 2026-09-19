"""Deterministic token/context efficiency audit for the public kernel.

This is intentionally a conservative character-based estimator.  It does not
pretend to know a provider's tokenizer; it catches avoidable prompt growth,
duplicate instructions and oversized always-loaded skill files before a real
client is connected.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from . import miniyaml


class TokenEfficiencyError(ValueError):
    pass


def _root(root):
    return Path(root).resolve()


def load_policy(root):
    path = _root(root) / "spec" / "token-efficiency.yaml"
    if not path.is_file():
        raise TokenEfficiencyError(f"缺少 Token 效率策略: {path}")
    policy = miniyaml.load_file(path)
    if not isinstance(policy, dict) or policy.get("version") != "1.0.0":
        raise TokenEfficiencyError("token-efficiency.yaml 版本不受支持")
    for section in ("estimator", "budgets", "public_instruction_files"):
        if section not in policy:
            raise TokenEfficiencyError(f"Token 策略缺少 {section}")
    return policy


def estimate_tokens(text, chars_per_token=4):
    if not isinstance(text, str):
        raise TokenEfficiencyError("只能估算文本")
    if int(chars_per_token) < 1:
        raise TokenEfficiencyError("chars_per_token 必须为正整数")
    return int(math.ceil(len(text) / int(chars_per_token)))


def _public_files(root, policy):
    root = _root(root)
    files = []
    for item in policy["public_instruction_files"]:
        path = (root / str(item)).resolve()
        if root not in path.parents:
            raise TokenEfficiencyError(f"技能路径越界: {item}")
        if not path.is_file():
            raise TokenEfficiencyError(f"技能文件不存在: {item}")
        files.append(path)
    if len(files) > int(policy["budgets"]["max_policy_files"]):
        raise TokenEfficiencyError("常驻技能文件数量超过预算")
    return files


def _duplicate_line_ratio(texts):
    lines = []
    for text in texts:
        for line in text.splitlines():
            normalized = " ".join(line.split())
            if normalized and len(normalized) >= 24:
                lines.append(normalized)
    if not lines:
        return 0.0
    return round(1 - len(set(lines)) / len(lines), 4)


def audit(root, selected=None):
    """Return a machine-readable audit; no model or network call is needed."""
    root = _root(root)
    policy = load_policy(root)
    files = _public_files(root, policy)
    chars_per_token = int(policy["estimator"]["chars_per_token"])
    envelope = int(policy["estimator"]["fixed_envelope_tokens"])
    max_skill = int(policy["budgets"]["max_single_skill_tokens"])
    max_context = int(policy["budgets"]["max_task_context_tokens"])
    max_duplicate = float(policy["budgets"]["max_duplicate_line_ratio"])
    selected_names = list(selected or [str(policy["public_instruction_files"][0])])
    by_name = {str(path.relative_to(root)): path for path in files}
    selected_paths = []
    for name in selected_names:
        path = by_name.get(str(name))
        if path is None:
            raise TokenEfficiencyError(f"未授权或未列入 allowlist 的技能: {name}")
        selected_paths.append(path)

    metrics = []
    texts = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        texts.append(text)
        metrics.append({
            "path": str(path.relative_to(root)),
            "characters": len(text),
            "estimated_tokens": estimate_tokens(text, chars_per_token),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    selected_tokens = sum(
        estimate_tokens(path.read_text(encoding="utf-8"), chars_per_token) for path in selected_paths
    )
    duplicate_ratio = _duplicate_line_ratio(texts)
    oversized = [item["path"] for item in metrics if item["estimated_tokens"] > max_skill]
    context_tokens = selected_tokens + envelope
    passed = bool(
        not oversized
        and context_tokens <= max_context
        and duplicate_ratio <= max_duplicate
    )
    return {
        "passed": passed,
        "estimator": {"chars_per_token": chars_per_token, "fixed_envelope_tokens": envelope},
        "budgets": {
            "max_single_skill_tokens": max_skill,
            "max_task_context_tokens": max_context,
            "max_duplicate_line_ratio": max_duplicate,
        },
        "files": metrics,
        "selected_files": [str(path.relative_to(root)) for path in selected_paths],
        "selected_skill_tokens": selected_tokens,
        "estimated_task_context_tokens": context_tokens,
        "duplicate_line_ratio": duplicate_ratio,
        "oversized_files": oversized,
        "safety_note": "Token budget does not authorize removal of security, privacy or verification rules.",
    }
