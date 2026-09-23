# -*- coding: utf-8 -*-
"""Durable macOS desktop notifications for scheduled publish escalations.

The event is committed to a local JSON log before any GUI call is attempted.
Notification content is passed as osascript arguments to a fixed script, so
event text is never interpolated into AppleScript source.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .registry import atomic_json, file_lock

SCHEMA_VERSION = 1
DEFAULT_LOG_PATH = Path.home() / ".aisk" / "state" / "publish-notifications.json"
DEFAULT_TIMEOUT_SECONDS = 10

# Keep this source constant. osascript exposes arguments after the -e script
# through the run handler's argv list.
_NOTIFICATION_SCRIPT = (
    "on run argv\n"
    "  display notification (item 2 of argv) with title (item 1 of argv)\n"
    "end run"
)

_EVENT_FIELDS = ("task_id", "alias", "operation_key", "type")
_EVENT_LABELS = {
    "needs_attention": "发布需要人工关注",
    "blocked": "发布已阻塞",
    "resolved": "发布已恢复",
}


@dataclass(frozen=True)
class NotificationResult:
    """Outcome of logging and, when possible, displaying one event."""

    status: str
    event_id: str
    persisted: bool
    notified: bool
    duplicate: bool = False
    error: str | None = None

    @property
    def success(self):
        """True only when a desktop notification was confirmed or deduped."""
        return self.notified and self.status in ("notified", "duplicate")


def _normalize_event(event):
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    normalized = {}
    for field in _EVENT_FIELDS:
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"event.{field} must be a non-empty string")
        normalized[field] = value.strip()
    if normalized["type"] not in _EVENT_LABELS:
        raise ValueError("event.type must be needs_attention, blocked, or resolved")
    return normalized


def _event_id(event, supplied):
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied.strip():
            raise ValueError("event_id must be a non-empty string")
        return supplied.strip()
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _safe_text(value, limit=180):
    """Keep notifications on one line and bound data passed to the GUI."""
    return " ".join(str(value).split())[:limit]


def _load_store(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "events": {}}
    with path.open("r", encoding="utf-8") as stream:
        store = json.load(stream)
    if (not isinstance(store, dict)
            or store.get("schema_version") != SCHEMA_VERSION
            or not isinstance(store.get("events"), dict)):
        raise ValueError("notification event log has an unsupported or invalid format")
    return store


def _save_record(path, store, event_id, record):
    store["events"][event_id] = record
    atomic_json(path, store)


def _result(status, event_id, *, persisted, notified=False, duplicate=False, error=None):
    return NotificationResult(
        status=status,
        event_id=event_id,
        persisted=persisted,
        notified=notified,
        duplicate=duplicate,
        error=error,
    )


def notify_publish_event(event, *, log_path=None, event_id=None, osascript_path=None,
                         timeout=DEFAULT_TIMEOUT_SECONDS):
    """Persist and display a publish-worker escalation event.

    ``event`` is a ``publish_worker`` event containing ``task_id``, ``alias``,
    ``operation_key`` and ``type``. By default, the stable event contents form
    the deduplication key. ``log_path`` should point into the Aisk state
    directory when the caller has a repository configuration; otherwise a
    per-user default is used.

    Results use ``notified`` for a new successful notification, ``duplicate``
    for a previously notified event, ``logged_only`` when durable fallback
    succeeded but osascript is unavailable, and ``failed`` when delivery or
    persistence failed. A failed delivery remains in the log as pending and
    can be retried by calling this function with the same event.
    """
    try:
        normalized = _normalize_event(event)
        key = _event_id(normalized, event_id)
    except (TypeError, ValueError) as error:
        return _result("failed", "", persisted=False, error=str(error))

    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    lock_path = path.with_name(path.name + ".lock")
    persisted = False
    try:
        # Serialize dedupe, durable log write, OS notification and result
        # persistence. This prevents two concurrent callers from displaying
        # the same event while the first call is still in progress.
        with file_lock(lock_path, blocking=True) as acquired:
            if not acquired:
                return _result("failed", key, persisted=False, error="could not acquire notification log lock")
            store = _load_store(path)
            prior = store["events"].get(key)
            if prior is not None:
                persisted = True
                if prior.get("event") != normalized:
                    return _result(
                        "failed", key, persisted=True,
                        error="event_id already belongs to different event data",
                    )
                if prior.get("notification_status") == "notified":
                    return _result("duplicate", key, persisted=True, notified=True, duplicate=True)
                record = prior
                duplicate = True
            else:
                record = {
                    "event": normalized,
                    "created_at": _timestamp(),
                    "notification_status": "pending",
                    "attempts": 0,
                }
                duplicate = False
                _save_record(path, store, key, record)
                persisted = True

            binary = osascript_path or shutil.which("osascript")
            if sys.platform != "darwin" or not binary:
                reason = (
                    "desktop notifications are supported only on macOS"
                    if sys.platform != "darwin" else "osascript was not found"
                )
                record["last_error"] = reason
                try:
                    _save_record(path, store, key, record)
                except Exception as error:
                    return _result(
                        "failed", key, persisted=True, duplicate=duplicate,
                        error=f"event is logged; fallback detail could not be saved: {error}",
                    )
                return _result("logged_only", key, persisted=True, duplicate=duplicate, error=reason)

            title = _safe_text(f"Aisk 发布提醒 · {normalized['task_id']}")
            label = _EVENT_LABELS.get(normalized["type"], f"发布状态：{normalized['type']}")
            body = _safe_text(f"{normalized['alias']} · {label}")
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record.pop("last_error", None)
            try:
                # All dynamic text is an argv item; shell expansion is not used.
                completed = subprocess.run(
                    [str(binary), "-e", _NOTIFICATION_SCRIPT, title, body],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if completed.returncode != 0:
                    detail = f"osascript exited with status {completed.returncode}"
                    stderr = _safe_text(getattr(completed, "stderr", ""), 240)
                    if stderr:
                        detail += f": {stderr}"
                    raise RuntimeError(detail)
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                record["last_error"] = str(error)[:500]
                try:
                    _save_record(path, store, key, record)
                except Exception as save_error:
                    return _result(
                        "failed", key, persisted=True, duplicate=duplicate,
                        error=f"notification failed ({error}); event remains logged but retry detail failed to save ({save_error})",
                    )
                return _result("failed", key, persisted=True, duplicate=duplicate, error=str(error))

            record["notification_status"] = "notified"
            record["notified_at"] = _timestamp()
            try:
                _save_record(path, store, key, record)
            except Exception as error:
                # The event was delivered, but the log remains pending; report
                # the partial failure so callers can surface/reconcile it.
                return _result(
                    "failed", key, persisted=True, notified=True, duplicate=duplicate,
                    error=f"notification was sent but its result could not be saved: {error}",
                )
            return _result("notified", key, persisted=True, notified=True, duplicate=duplicate)
    except Exception as error:
        # Fail closed on malformed logs, filesystem errors, and locking errors.
        # Never replace an unreadable log with a fresh one.
        return _result(
            "failed", key, persisted=persisted,
            error=f"notification event log unavailable: {error}",
        )


def drain_pending_notifications(*, log_path=None, osascript_path=None, timeout=DEFAULT_TIMEOUT_SECONDS,
                                exclude_event_ids=(), limit=100):
    """Retry persisted notification intents which have not been delivered.

    The snapshot is taken under the event-log lock; each event is then sent
    through ``notify_publish_event`` which rechecks its state under that same
    lock. ``exclude_event_ids`` lets a caller avoid immediately retrying an
    event it already attempted in the current scheduler invocation.
    """
    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    try:
        limit = int(limit)
        if limit < 1:
            raise ValueError("limit must be positive")
        excluded = {str(item) for item in (exclude_event_ids or ())}
        lock_path = path.with_name(path.name + ".lock")
        with file_lock(lock_path, blocking=True) as acquired:
            if not acquired:
                return (_result("failed", "", persisted=False,
                                error="could not acquire notification log lock"),)
            store = _load_store(path)
            pending = []
            for key, record in store["events"].items():
                if not isinstance(record, dict):
                    raise ValueError(f"notification record {key} has an invalid format")
                if record.get("notification_status") == "notified" or key in excluded:
                    continue
                event = record.get("event")
                if not isinstance(event, Mapping):
                    raise ValueError(f"pending notification {key} has no valid event")
                pending.append((str(key), dict(event), str(record.get("created_at") or "")))
        pending.sort(key=lambda item: (item[2], item[0]))
    except Exception as error:
        return (_result("failed", "", persisted=False,
                        error=f"notification event log unavailable: {error}"),)

    outcomes = []
    for key, event, _created_at in pending[:limit]:
        outcomes.append(notify_publish_event(
            event,
            log_path=path,
            event_id=key,
            osascript_path=osascript_path,
            timeout=timeout,
        ))
    return tuple(outcomes)
