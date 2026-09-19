"""脱敏 Token Runtime telemetry built on the audit ledger."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .ledger import AuditLedger, LedgerError


class TelemetryError(ValueError):
    pass


class Telemetry:
    def __init__(self, ledger: AuditLedger | str | Path):
        self.ledger = ledger if isinstance(ledger, AuditLedger) else AuditLedger(ledger)

    def record(self, *, session_id: str, token_count: int, mode: str, trigger: str, fallback: str, result: str,
               source_digest: str, metadata: Mapping[str, Any] | None = None,
               token_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        try:
            record = self.ledger.make_record(
                session_id=session_id, token_count=token_count, mode=mode, trigger=trigger,
                fallback=fallback, result=result, source_digest=source_digest,
                metadata=metadata, token_counts=token_counts,
            )
            return self.ledger.append(record)
        except LedgerError as exc:
            raise TelemetryError(str(exc)) from exc

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise TelemetryError("session_id 必须是非空字符串")
        try:
            records = self.ledger.read()
        except LedgerError as exc:
            raise TelemetryError(str(exc)) from exc
        selected = [r for r in records if session_id is None or r["session_id"] == session_id]
        modes = Counter(r["mode"] for r in selected)
        results = Counter(r["result"] for r in selected)
        total = sum(r["token_count"] for r in selected)
        return {
            "schema_version": 1,
            "session_id": session_id,
            "event_count": len(selected),
            "token_count": total,
            "by_mode": dict(sorted(modes.items())),
            "by_result": dict(sorted(results.items())),
            "source_digests": sorted({r["source_digest"] for r in selected}),
        }
