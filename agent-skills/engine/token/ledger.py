"""Privacy-safe JSONL audit ledger for Token Runtime events."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


class LedgerError(ValueError):
    """Raised when an audit event is incomplete or contains unsafe data."""


_MODES = {"lite", "standard", "deep", "emergency"}
_SENSITIVE = {"body", "content", "prompt", "response", "text", "sql", "credential", "password", "secret", "token", "environment", "env"}
_METADATA = {"event_id", "tool", "task_id", "path", "version", "classification", "source_digest", "loaded_at"}
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{name} 必须是非空字符串")
    return value


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerError(f"{name} 必须是非负整数")
    return value


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LedgerError("metadata 必须是对象")
    clean = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.lower() in _SENSITIVE or key not in _METADATA:
            raise LedgerError("metadata 含未授权或敏感字段")
        if not isinstance(item, (str, int, bool)) or isinstance(item, float):
            raise LedgerError("metadata 只允许标量元数据")
        if key == "path":
            path = Path(item)
            if path.is_absolute() or ".." in path.parts:
                raise LedgerError("metadata.path 必须是安全的相对路径")
        if key == "source_digest" and (not isinstance(item, str) or not _SHA256.fullmatch(item)):
            raise LedgerError("metadata.source_digest 必须是 64 位小写 sha256")
        clean[key] = item
    return clean


class AuditLedger:
    """Append and validate metadata-only audit records."""

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise LedgerError("ledger path 必须是非空路径")
        self.path = Path(path)

    def make_record(self, *, session_id: str, token_count: int, mode: str, trigger: str, fallback: str, result: str,
                    source_digest: str, metadata: Mapping[str, Any] | None = None,
                    token_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        session_id = _nonempty(session_id, "session_id")
        if mode not in _MODES:
            raise LedgerError("mode 不受支持")
        record = {
            "schema_version": 1,
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "session_id": session_id,
            "metadata": _metadata(metadata),
            "token_count": _count(token_count, "token_count"),
            "mode": mode,
            "trigger": _nonempty(trigger, "trigger"),
            "fallback": _nonempty(fallback, "fallback"),
            "result": _nonempty(result, "result"),
            "source_digest": _nonempty(source_digest, "source_digest"),
        }
        if token_counts is not None:
            if not isinstance(token_counts, Mapping) or set(token_counts) != {"input", "output", "total"}:
                raise LedgerError("token_counts 必须包含 input/output/total")
            record["token_counts"] = {key: _count(value, f"token_counts.{key}") for key, value in token_counts.items()}
        return record

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise LedgerError("audit record 必须是对象")
        normalized = self._validate_without_write(record)
        line = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise LedgerError(f"audit ledger 无法写入: {self.path}") from exc
        return normalized

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise LedgerError(f"audit ledger 无法读取: {self.path}") from exc
        records = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                raise LedgerError(f"audit ledger 第 {number} 行为空")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"audit ledger 第 {number} 行不是 JSON") from exc
            records.append(self.append_validate(value))
        return records

    @staticmethod
    def append_validate(record: Mapping[str, Any]) -> dict[str, Any]:
        return AuditLedger._validate_without_write(record)

    @staticmethod
    def _validate_without_write(record: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"schema_version", "at", "session_id", "metadata", "token_count", "token_counts", "mode", "trigger", "fallback", "result", "source_digest"}
        required = {"schema_version", "at", "session_id", "metadata", "token_count", "mode", "trigger", "fallback", "result", "source_digest"}
        if not isinstance(record, Mapping) or set(record) - allowed or not required <= set(record):
            raise LedgerError("audit record 含未授权字段")
        result = dict(record)
        if result.get("schema_version") != 1 or result.get("mode") not in _MODES:
            raise LedgerError("audit record schema 或 mode 非法")
        _nonempty(result.get("session_id"), "session_id")
        _count(result.get("token_count"), "token_count")
        for key in ("at", "trigger", "fallback", "result", "source_digest"):
            _nonempty(result.get(key), key)
        if not _SHA256.fullmatch(result["source_digest"]):
            raise LedgerError("source_digest 必须是 64 位小写 sha256")
        result["metadata"] = _metadata(result.get("metadata"))
        if "token_counts" in result:
            counts = result["token_counts"]
            if not isinstance(counts, Mapping) or set(counts) != {"input", "output", "total"}:
                raise LedgerError("token_counts 结构非法")
            result["token_counts"] = {key: _count(value, f"token_counts.{key}") for key, value in counts.items()}
        return result
