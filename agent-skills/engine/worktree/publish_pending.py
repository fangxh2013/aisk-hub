# -*- coding: utf-8 -*-
"""Persisted state for publish retries and human escalation.

This module deliberately performs no Git/network operation and sends no
notifications. Callers record outcomes and deliver the returned events through
their own transport. Defaults are retries after 1, 5, then 15 minutes,
needs_attention after three attempts or fifteen minutes, and blocked after
sixty minutes. Repository policy may override those values. Blocked transient
operations continue retrying hourly; deterministic failures do not auto-retry.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .registry import atomic_json, file_lock

SCHEMA_VERSION = 1

TRANSIENT = "transient"
NON_FAST_FORWARD = "non_fast_forward"
PERMISSION = "permission"
PROTECTION = "protection"
UNKNOWN = "unknown"

IDLE = "idle"
PUSH_PENDING = "push_pending"
NEEDS_ATTENTION = "needs_attention"
BLOCKED = "blocked"
PUBLISHED = "published"

ATTENTION_AFTER_SECONDS = 15 * 60
BLOCKED_AFTER_SECONDS = 60 * 60
BLOCKED_RETRY_INTERVAL_SECONDS = 60 * 60
ATTENTION_AFTER_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS_SECONDS = (60, 5 * 60, 15 * 60)


@dataclass(frozen=True)
class RetryPolicy:
    retry_delays_seconds: tuple[int, ...] = DEFAULT_RETRY_DELAYS_SECONDS
    needs_attention_after_seconds: int = ATTENTION_AFTER_SECONDS
    needs_attention_after_attempts: int = ATTENTION_AFTER_ATTEMPTS
    blocked_after_seconds: int = BLOCKED_AFTER_SECONDS


def resolve_policy(value=None):
    """Validate profile policy and convert minute settings into runtime seconds."""
    if isinstance(value, RetryPolicy):
        return value
    raw = value or {}
    if not isinstance(raw, dict):
        raise ValueError("publish_pending_policy must be a mapping")

    def positive_int(raw_value, label):
        if isinstance(raw_value, bool):
            raise ValueError(f"{label} must be a positive integer")
        try:
            resolved = int(raw_value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be a positive integer") from error
        if resolved < 1 or str(resolved) != str(raw_value).strip():
            raise ValueError(f"{label} must be a positive integer")
        return resolved

    if raw.get("retry_only_transient_failures", True) is not True:
        raise ValueError("automatic retries are restricted to transient failures")
    dedupe_mode = raw.get("notify_deduplication", True)
    if dedupe_mode is not True and dedupe_mode != "task_repo_remote_stage":
        raise ValueError("publish escalation deduplication must remain enabled")

    delays_minutes = raw.get(
        "retry_delays_minutes",
        tuple(delay // 60 for delay in DEFAULT_RETRY_DELAYS_SECONDS),
    )
    if not isinstance(delays_minutes, (list, tuple)) or not delays_minutes:
        raise ValueError("retry_delays_minutes must be a non-empty positive integer list")
    delays = tuple(
        positive_int(value, "retry_delays_minutes item") * 60
        for value in delays_minutes
    )
    attention_minutes = positive_int(
        raw.get("needs_attention_after_minutes", ATTENTION_AFTER_SECONDS // 60),
        "needs_attention_after_minutes",
    )
    attention_attempts = positive_int(
        raw.get("needs_attention_after_attempts", ATTENTION_AFTER_ATTEMPTS),
        "needs_attention_after_attempts",
    )
    blocked_minutes = positive_int(
        raw.get("blocked_after_minutes", BLOCKED_AFTER_SECONDS // 60),
        "blocked_after_minutes",
    )
    if blocked_minutes <= attention_minutes:
        raise ValueError("blocked_after_minutes must exceed needs_attention_after_minutes")
    return RetryPolicy(
        retry_delays_seconds=delays,
        needs_attention_after_seconds=attention_minutes * 60,
        needs_attention_after_attempts=attention_attempts,
        blocked_after_seconds=blocked_minutes * 60,
    )

_FAILURE_ALIASES = {
    "transient": TRANSIENT,
    "network": TRANSIENT,
    "timeout": TRANSIENT,
    "transport": TRANSIENT,
    "temporary": TRANSIENT,
    "server_error": TRANSIENT,
    "service_unavailable": TRANSIENT,
    "rate_limited": TRANSIENT,
    "non_fast_forward": NON_FAST_FORWARD,
    "nonfastforward": NON_FAST_FORWARD,
    "non_ff": NON_FAST_FORWARD,
    "permission": PERMISSION,
    "permission_denied": PERMISSION,
    "authentication": PERMISSION,
    "auth": PERMISSION,
    "protection": PROTECTION,
    "protected": PROTECTION,
    "protected_branch": PROTECTION,
    "policy_rejected": PROTECTION,
    "unknown": UNKNOWN,
}


@dataclass(frozen=True)
class Transition:
    """A new persisted state and notification intents for the caller."""

    state: dict
    events: tuple[str, ...] = ()


def _as_utc(value=None):
    if value is None:
        value = dt.datetime.now(dt.timezone.utc)
    elif isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, dt.datetime):
        raise TypeError("time must be a datetime, ISO timestamp, or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value):
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value):
    return _as_utc(value) if value else None


def _text(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def classify_failure(kind=None, message=None):
    """Classify a publish error; anything not recognized fails closed as unknown."""
    token = _text(kind)
    if token in _FAILURE_ALIASES and _FAILURE_ALIASES[token] != UNKNOWN:
        return _FAILURE_ALIASES[token]

    evidence = f"{kind or ''} {message or ''}".lower()
    # Deterministic remote rejections take precedence over generic transport text.
    if any(part in evidence for part in (
        "non-fast-forward", "non fast forward", "fetch first",
        "tip of your current branch is behind",
        "updates were rejected because the remote contains work",
        "cannot be fast-forwarded",
    )):
        return NON_FAST_FORWARD
    if any(part in evidence for part in (
        "protected branch", "pre-receive hook declined", "hook declined",
        "branch policy", "prohibited by repository policy", "not allowed to update",
    )):
        return PROTECTION
    if any(part in evidence for part in (
        "permission denied", "authentication failed", "could not read username",
        "access denied", "insufficient permission", "write access to repository not granted",
        "repository not found", "http 401", "http 403", "status code 401", "status code 403",
    )):
        return PERMISSION
    if any(part in evidence for part in (
        "timed out", "timeout", "connection reset", "connection refused",
        "could not resolve host", "temporary failure in name resolution",
        "network is unreachable", "no route to host", "remote end hung up",
        "http 429", "http 502", "http 503", "http 504", "502 bad gateway",
        "503 service unavailable", "504 gateway timeout", "rate limit",
        "returned error: 500", "returned error: 502", "returned error: 503",
        "returned error: 504", "500 internal server error",
    )):
        return TRANSIENT
    return UNKNOWN


def new_state(operation_key):
    key = str(operation_key or "").strip()
    if not key:
        raise ValueError("operation_key is required")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_key": key,
        "status": IDLE,
        "failure_class": None,
        "first_pending_at": None,
        "last_attempt_at": None,
        "attempt_timestamps": [],
        "attempt_count": 0,
        "next_retry_at": None,
        "needs_attention_at": None,
        "blocked_at": None,
        "resolved_at": None,
        "remote_sha": None,
        "last_error": None,
        "escalations_emitted": [],
    }


def _copy_state(state):
    if state is None:
        raise ValueError("state is required")
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported publish-pending state")
    copied = json.loads(json.dumps(state))
    if not copied.get("operation_key"):
        raise ValueError("state operation_key is required")
    copied.setdefault("escalations_emitted", [])
    copied.setdefault("remote_sha", None)
    return copied


def _emit(state, *events):
    emitted = set(state.get("escalations_emitted") or [])
    fresh = []
    for event in events:
        if event not in emitted:
            emitted.add(event)
            fresh.append(event)
    state["escalations_emitted"] = [
        event for event in ("needs_attention", "blocked", "resolved")
        if event in emitted
    ]
    return tuple(fresh)


def _advance(state, now, policy=None):
    policy = resolve_policy(policy)
    first = _parse_iso(state.get("first_pending_at"))
    if first is None or state.get("status") in (IDLE, PUBLISHED):
        return ()
    age = max(0.0, (now - first).total_seconds())
    if state.get("status") == BLOCKED:
        blocked_at = _parse_iso(state.get("blocked_at"))
        if blocked_at is None:
            blocked_at = first + dt.timedelta(seconds=policy.blocked_after_seconds)
            state["blocked_at"] = _iso(blocked_at)
        if state.get("failure_class") == TRANSIENT:
            state["next_retry_at"] = state.get("next_retry_at") or _iso(
                blocked_at + dt.timedelta(seconds=BLOCKED_RETRY_INTERVAL_SECONDS)
            )
        else:
            state["next_retry_at"] = None
        return _emit(state, "blocked")
    if age >= policy.blocked_after_seconds:
        state["status"] = BLOCKED
        blocked_at = _parse_iso(state.get("blocked_at"))
        if blocked_at is None:
            blocked_at = first + dt.timedelta(seconds=policy.blocked_after_seconds)
            state["blocked_at"] = _iso(blocked_at)
        if state.get("failure_class") == TRANSIENT:
            state["next_retry_at"] = _iso(
                blocked_at + dt.timedelta(seconds=BLOCKED_RETRY_INTERVAL_SECONDS)
            )
        else:
            state["next_retry_at"] = None
        if state.get("needs_attention_at") is None:
            state["needs_attention_at"] = _iso(
                first + dt.timedelta(seconds=policy.needs_attention_after_seconds)
            )
        return _emit(state, "blocked")
    if (age >= policy.needs_attention_after_seconds
            or state.get("attempt_count", 0) >= policy.needs_attention_after_attempts):
        state["status"] = NEEDS_ATTENTION
        state["needs_attention_at"] = state.get("needs_attention_at") or _iso(
            max(now, first + dt.timedelta(seconds=policy.needs_attention_after_seconds))
            if age >= policy.needs_attention_after_seconds else now
        )
        return _emit(state, "needs_attention")
    return ()


def record_failure(state, kind=None, message=None, *, now=None,
                   retry_delays_seconds=None, policy=None):
    """Record one completed publish attempt and apply retry/escalation policy."""
    updated = _copy_state(state)
    current = _as_utc(now)
    selected_policy = resolve_policy(policy)
    delays = (tuple(max(0, int(delay)) for delay in retry_delays_seconds)
              if retry_delays_seconds is not None else selected_policy.retry_delays_seconds)
    failure_class = classify_failure(kind, message)
    if updated["status"] == PUBLISHED:
        updated = new_state(updated["operation_key"])
    if updated["status"] == IDLE:
        updated["first_pending_at"] = _iso(current)
    updated["last_attempt_at"] = _iso(current)
    updated["attempt_count"] = int(updated.get("attempt_count") or 0) + 1
    updated.setdefault("attempt_timestamps", []).append(_iso(current))
    updated["failure_class"] = failure_class
    updated["last_error"] = str(message or kind or "publish failed")
    updated["resolved_at"] = None

    if updated["status"] == BLOCKED:
        if failure_class == TRANSIENT:
            updated["next_retry_at"] = _iso(current + dt.timedelta(
                seconds=BLOCKED_RETRY_INTERVAL_SECONDS,
            ))
        else:
            updated["next_retry_at"] = None
        return Transition(updated, ())

    events = ()
    if failure_class == TRANSIENT:
        delays = delays or (0,)
        delay = delays[min(updated["attempt_count"] - 1, len(delays) - 1)]
        updated["status"] = PUSH_PENDING
        updated["next_retry_at"] = _iso(current + dt.timedelta(seconds=delay))
    else:
        updated["status"] = NEEDS_ATTENTION
        updated["next_retry_at"] = None
        updated["needs_attention_at"] = updated.get("needs_attention_at") or _iso(current)
        events += _emit(updated, "needs_attention")

    events += _advance(updated, current, selected_policy)
    return Transition(updated, events)


def advance_state(state, *, now=None, policy=None):
    """Apply due 15-minute/60-minute escalation and return new events once."""
    updated = _copy_state(state)
    events = _advance(updated, _as_utc(now), resolve_policy(policy))
    return Transition(updated, events)


def should_retry(state, *, now=None, policy=None):
    """Return whether a transient publish retry is due, including blocked cadence."""
    if not state or state.get("failure_class") != TRANSIENT:
        return False
    status = state.get("status")
    if status not in (PUSH_PENDING, NEEDS_ATTENTION, BLOCKED):
        return False
    current = _as_utc(now)
    selected_policy = resolve_policy(policy)
    first = _parse_iso(state.get("first_pending_at"))
    if status != BLOCKED and first is not None and (
        current - first
    ).total_seconds() >= selected_policy.blocked_after_seconds:
        return False
    due = _parse_iso(state.get("next_retry_at"))
    return due is not None and current >= due


def record_success(state, *, now=None, remote_sha=None):
    """Close an outstanding publish alert after the caller confirms success."""
    updated = _copy_state(state)
    current = _as_utc(now)
    confirmed_sha = str(remote_sha or "").strip().lower() or None
    existing_sha = str(updated.get("remote_sha") or "").strip().lower() or None
    if existing_sha and confirmed_sha and existing_sha != confirmed_sha:
        raise ValueError("published remote_sha cannot be replaced")
    if confirmed_sha:
        updated["remote_sha"] = confirmed_sha
    was_open = updated.get("status") in (PUSH_PENDING, NEEDS_ATTENTION, BLOCKED)
    if updated.get("status") == PUBLISHED:
        return Transition(updated, ())
    updated["status"] = PUBLISHED
    updated["next_retry_at"] = None
    updated["resolved_at"] = _iso(current)
    events = _emit(updated, "resolved") if was_open else ()
    return Transition(updated, events)


class PublishPendingStore:
    """Atomic JSON store keyed by a task/repository publish operation identifier."""

    def __init__(self, root, policy=None):
        self.root = Path(root)
        self.policy = resolve_policy(policy)

    @staticmethod
    def _filename(operation_key):
        key = str(operation_key or "").strip()
        if not key:
            raise ValueError("operation_key is required")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _paths(self, operation_key):
        digest = self._filename(operation_key)
        return self.root / f"{digest}.json", self.root / f"{digest}.lock"

    @contextmanager
    def operation_lock(self, operation_key):
        """Serialize the complete publish attempt per task/repo/SHA key."""
        _, state_lock = self._paths(operation_key)
        operation_lock = state_lock.with_name(state_lock.stem + ".operation.lock")
        with file_lock(operation_lock):
            yield

    def get(self, operation_key):
        path, lock_path = self._paths(operation_key)
        if not path.exists():
            return None
        with file_lock(lock_path):
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
            if state.get("operation_key") != str(operation_key).strip():
                raise ValueError("publish-pending key does not match persisted state")
            _copy_state(state)
            return state

    def _update(self, operation_key, transform, *, create=True):
        key = str(operation_key or "").strip()
        if not key:
            raise ValueError("operation_key is required")
        path, lock_path = self._paths(key)
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(lock_path):
            if path.exists():
                with path.open("r", encoding="utf-8") as stream:
                    state = json.load(stream)
                if state.get("operation_key") != key:
                    raise ValueError("publish-pending key does not match persisted state")
                _copy_state(state)
            elif create:
                state = new_state(key)
            else:
                return None
            result = transform(state)
            atomic_json(path, result.state)
            return result

    def record_failure(self, operation_key, kind=None, message=None, *, now=None,
                       retry_delays_seconds=None, policy=None):
        selected_policy = resolve_policy(policy) if policy is not None else self.policy
        return self._update(
            operation_key,
            lambda state: record_failure(state, kind, message, now=now,
                                         retry_delays_seconds=retry_delays_seconds,
                                         policy=selected_policy),
        )

    def advance(self, operation_key, *, now=None, policy=None):
        selected_policy = resolve_policy(policy) if policy is not None else self.policy
        return self._update(
            operation_key, lambda state: advance_state(
                state, now=now, policy=selected_policy,
            ), create=False
        )

    def record_success(self, operation_key, *, now=None, remote_sha=None):
        return self._update(
            operation_key, lambda state: record_success(
                state, now=now, remote_sha=remote_sha,
            )
        )
