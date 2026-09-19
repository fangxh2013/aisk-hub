"""Local transactional coordination store.

SQLite is the source of truth for one physical machine.  State changes and
outbox events commit in the same transaction.  JSONL is only an export format;
consumers must deduplicate by event_id/idempotency_key.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from pathlib import Path


class CoordinationError(RuntimeError):
    pass


ACTORS = {"codex", "claude", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human"}
TRANSITIONS = {
    ("Claimed", "InCheck", "task.checked"),
    ("InCheck", "Ready", "task.ready"),
    ("Ready", "Landed", "task.landed"),
    ("Landed", "Promoted", "task.promoted"),
    ("Claimed", "Paused", "task.paused"),
    ("Claimed", "Expired", "task.expired"),
    ("Claimed", "Conflicted", "task.conflicted"),
    ("Paused", "Unassigned", "task.released"),
    ("Conflicted", "Unassigned", "task.released"),
    ("Claimed", "Unassigned", "task.released"),
}


def now():
    return _dt.datetime.now(_dt.timezone.utc)


def iso(value=None):
    return (value or now()).isoformat(timespec="seconds")


def parse_time(value):
    return _dt.datetime.fromisoformat(value)


class CoordinationStore:
    """ACID task state with CAS, leases, fencing and transactional outbox."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        if self.path.name != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self._init()

    def close(self):
        self.db.close()

    def _init(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              version INTEGER NOT NULL,
              fencing_token INTEGER NOT NULL DEFAULT 0,
              owner_tool TEXT,
              owner_session TEXT,
              lease_until TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox_events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              version INTEGER NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              actor_tool TEXT NOT NULL,
              actor_session TEXT NOT NULL,
              model TEXT,
              occurred_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              exported_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events(exported_at, seq);
            """
        )

    @staticmethod
    def _actor(actor):
        if not isinstance(actor, dict) or actor.get("tool") not in ACTORS or not actor.get("session_id"):
            raise CoordinationError("actor 必须包含合法 tool 和非空 session_id")
        return actor

    def _task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"任务不存在: {task_id}")
        return row

    def create(self, task_id, actor, payload, *, idempotency_key=None):
        self._actor(actor)
        if not task_id or not isinstance(payload, dict):
            raise CoordinationError("task_id 和 payload 必填")
        event_id = str(uuid.uuid4())
        key = idempotency_key or f"create:{task_id}"
        stamp = iso()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO tasks(task_id,status,version,created_at,updated_at) VALUES(?,?,?,?,?)",
                (task_id, "Unassigned", 1, stamp, stamp),
            )
            self._insert_event(event_id, "task.created", task_id, 1, key, actor, stamp, payload)
            self.db.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self.db.execute("ROLLBACK")
            raise CoordinationError(f"任务或幂等键已存在: {task_id}") from exc
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.snapshot(task_id)

    def claim(self, task_id, actor, *, lease_ttl=1800, payload=None, idempotency_key=None):
        self._actor(actor)
        if not 60 <= int(lease_ttl) <= 7200:
            raise CoordinationError("lease_ttl 必须在 60..7200 秒")
        payload = dict(payload or {})
        stamp = iso()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._task(task_id)
            if row["status"] not in {"Unassigned", "Expired", "Paused", "Conflicted"}:
                raise CoordinationError(f"当前状态不可认领: {row['status']}")
            token = int(row["fencing_token"]) + 1
            version = int(row["version"]) + 1
            key = idempotency_key or f"claim:{task_id}:{actor['tool']}:{actor['session_id']}:{token}"
            payload.update({"fencing_token": token, "lease_ttl_seconds": int(lease_ttl),
                            "worktree_path": payload.get("worktree_path", "")})
            self.db.execute(
                "UPDATE tasks SET status='Claimed',version=?,fencing_token=?,owner_tool=?,owner_session=?,lease_until=?,updated_at=? WHERE task_id=? AND version=?",
                (version, token, actor["tool"], actor["session_id"], iso(now() + _dt.timedelta(seconds=lease_ttl)), stamp, task_id, row["version"]),
            )
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise CoordinationError("CAS 认领冲突")
            self._insert_event(str(uuid.uuid4()), "task.claimed", task_id, version, key, actor, stamp, payload)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.snapshot(task_id)

    def heartbeat(self, task_id, actor, fencing_token, *, lease_ttl=1800, idempotency_key=None):
        self._actor(actor)
        stamp = iso()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._task(task_id)
            self._assert_owner(row, actor, fencing_token)
            if row["status"] != "Claimed":
                raise CoordinationError(f"当前状态不可续租: {row['status']}")
            version = int(row["version"]) + 1
            key = idempotency_key or f"heartbeat:{task_id}:{fencing_token}:{version}"
            self.db.execute(
                "UPDATE tasks SET version=?,lease_until=?,updated_at=? WHERE task_id=? AND version=? AND fencing_token=?",
                (version, iso(now() + _dt.timedelta(seconds=lease_ttl)), stamp, task_id, row["version"], fencing_token),
            )
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise CoordinationError("心跳 CAS 冲突")
            self._insert_event(str(uuid.uuid4()), "task.heartbeat", task_id, version, key, actor, stamp,
                               {"fencing_token": int(fencing_token), "client_timestamp": stamp})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.snapshot(task_id)

    def transition(self, task_id, actor, fencing_token, new_status, event_type, payload=None, *, idempotency_key=None):
        self._actor(actor)
        payload = dict(payload or {})
        stamp = iso()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._task(task_id)
            self._assert_owner(row, actor, fencing_token)
            if new_status not in {"Unassigned", "InCheck", "Ready", "Landed", "Promoted", "Paused", "Conflicted", "Aborted", "Expired", "Claimed"}:
                raise CoordinationError(f"未知目标状态: {new_status}")
            if event_type != "task.aborted" and (row["status"], new_status, event_type) not in TRANSITIONS:
                raise CoordinationError(f"非法状态迁移: {row['status']} -> {new_status} ({event_type})")
            version = int(row["version"]) + 1
            key = idempotency_key or f"{event_type}:{task_id}:{version}"
            payload.setdefault("fencing_token", int(fencing_token))
            self.db.execute(
                "UPDATE tasks SET status=?,version=?,updated_at=?,lease_until=? WHERE task_id=? AND version=? AND fencing_token=?",
                (new_status, version, stamp, None if new_status in {"Unassigned", "Paused", "Ready", "Landed", "Promoted", "Aborted", "Expired"} else row["lease_until"], task_id, row["version"], fencing_token),
            )
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise CoordinationError("状态迁移 CAS 冲突")
            self._insert_event(str(uuid.uuid4()), event_type, task_id, version, key, actor, stamp, payload)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.snapshot(task_id)

    def expire(self, task_id, *, now_value=None, idempotency_key=None):
        current = self._task(task_id)
        if current["status"] != "Claimed" or not current["lease_until"]:
            raise CoordinationError("任务没有可过期的活跃租约")
        if parse_time(current["lease_until"]) >= (now_value or now()):
            raise CoordinationError("租约尚未过期")
        actor = {"tool": current["owner_tool"], "session_id": current["owner_session"]}
        return self.transition(task_id, actor, int(current["fencing_token"]), "Expired", "task.expired",
                               {"expired_actor": current["owner_session"], "last_heartbeat_at": current["updated_at"]},
                               idempotency_key=idempotency_key)

    def _assert_owner(self, row, actor, token):
        if int(row["fencing_token"]) != int(token):
            raise CoordinationError("StaleFencingTokenException")
        if row["owner_tool"] != actor["tool"] or row["owner_session"] != actor["session_id"]:
            raise CoordinationError("当前 actor 不是租约持有者")

    def _insert_event(self, event_id, event_type, task_id, version, key, actor, occurred_at, payload):
        self.db.execute(
            "INSERT INTO outbox_events(event_id,event_type,task_id,version,idempotency_key,actor_tool,actor_session,model,occurred_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, event_type, task_id, version, key, actor["tool"], actor["session_id"], actor.get("model"), occurred_at,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def snapshot(self, task_id):
        row = self._task(task_id)
        return dict(row)

    def pending_events(self, limit=100):
        rows = self.db.execute("SELECT * FROM outbox_events WHERE exported_at IS NULL ORDER BY seq LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def export_jsonl(self, destination, limit=100):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rows = self.pending_events(limit)
        if not rows:
            return 0
        with destination.open("a", encoding="utf-8") as fh:
            for row in rows:
                item = {
                    "event_id": row["event_id"], "event_type": row["event_type"], "task_id": row["task_id"],
                    "version": row["version"], "idempotency_key": row["idempotency_key"],
                    "actor": {"tool": row["actor_tool"], "session_id": row["actor_session"], "model": row["model"]},
                    "occurred_at": row["occurred_at"], "payload": json.loads(row["payload_json"]),
                }
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
        ids = [row["event_id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self.db.execute(f"UPDATE outbox_events SET exported_at=? WHERE event_id IN ({placeholders})", (iso(), *ids))
        return len(ids)
