"""Strict, replayable digests for rules and other versioned sources."""
from __future__ import annotations

import datetime as _dt
import hashlib
from pathlib import Path
from typing import Any, Mapping


class DigestError(ValueError):
    """Raised when a source cannot be safely loaded or described."""


_CLASSIFICATIONS = {"PUBLIC", "INTERNAL_TEMPLATE", "CONFIDENTIAL", "SECRET"}


def _loaded_at(value: str | None) -> str:
    if value is None:
        return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise DigestError("loaded_at 必须是非空 ISO-8601 字符串")
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DigestError("loaded_at 必须是合法 ISO-8601 时间") from exc
    return value


def _classification(value: str) -> str:
    if not isinstance(value, str) or value not in _CLASSIFICATIONS:
        raise DigestError("classification 必须是已声明的隐私分类")
    return value


def source_digest(data: bytes | str) -> str:
    """Return a lowercase SHA-256 digest without retaining source content."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise DigestError("digest 输入必须是 bytes 或 str")
    return hashlib.sha256(data).hexdigest()


def source_record(path: str | Path, digest: str, classification: str, *, loaded_at: str | None = None) -> dict[str, str]:
    """Build the metadata needed to replay a source version."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise DigestError("path 必须是非空路径")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise DigestError("digest 必须是 64 位小写 sha256")
    # Audit metadata must not persist a developer's absolute local path.
    # Relative repository labels remain useful for replay; absolute inputs are
    # reduced to their basename rather than leaking home/workspace structure.
    safe_path = Path(path).as_posix()
    if Path(path).is_absolute():
        safe_path = Path(path).name
    return {
        "path": safe_path,
        "sha256": digest,
        "digest": digest,
        "loaded_at": _loaded_at(loaded_at),
        "classification": _classification(classification),
    }


def load_rule_source(path: str | Path, classification: str, *, encoding: str = "utf-8", loaded_at: str | None = None) -> tuple[str, dict[str, str]]:
    """Load a rule source and return its content plus replay metadata.

    The caller may use the returned content for parsing, while audit records
    should retain only the accompanying metadata and digest.
    """
    if not isinstance(encoding, str) or not encoding.strip():
        raise DigestError("encoding 必须是非空字符串")
    path = Path(path)
    try:
        raw = path.read_bytes()
        content = raw.decode(encoding)
    except (OSError, UnicodeError) as exc:
        raise DigestError(f"规则源无法安全读取: {path}") from exc
    return content, source_record(path, source_digest(raw), classification, loaded_at=loaded_at)


def version_digest(sources: Mapping[str, Mapping[str, Any]]) -> str:
    """Digest source metadata deterministically, retaining source digests."""
    if not isinstance(sources, Mapping) or not sources:
        raise DigestError("sources 必须是非空映射")
    import json

    normalized = {}
    for name, record in sorted(sources.items()):
        if not isinstance(name, str) or not name.strip() or not isinstance(record, Mapping):
            raise DigestError("source metadata 结构非法")
        required = {"path", "sha256", "digest", "loaded_at", "classification"}
        if set(record) != required or record["sha256"] != record["digest"]:
            raise DigestError("source metadata 必须完整且 sha256/digest 一致")
        normalized[name] = dict(record)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return source_digest(encoded)
