#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scheduled publish worker tests using local fake task records only."""
import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import publish_pending, publish_worker  # noqa: E402


UTC = dt.timezone.utc
START = dt.datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
LANDED_SHA = "a" * 40
REMOTE_SHA = "b" * 40


class FakeRepoCfg:
    def __init__(self, *, automatic=None, workspace_mode="task-worktree"):
        self.workspace_mode = workspace_mode
        self.automatic = automatic or {
            "commit_task_branch": True,
            "land_to_local": "fxh",
            "push_only": "fxh-dev",
            "archive_after_verified_land": True,
        }


class FakeConfig:
    profile_name = "xinhua"
    integration = "fxh"

    def __init__(self, root, repos=None):
        self.state_dir = Path(root) / "state"
        self.repos = repos or {alias: FakeRepoCfg() for alias in ("be", "web")}

    def repo(self, alias):
        return self.repos[alias]


def task(task_id="T001", alias="be", *, state="archived", sha=LANDED_SHA,
         operation_key=None, status=None, remote_sha=None):
    sha = str(sha).lower() if sha else ""
    row = {"landed_sha": sha}
    row["publish_operation_key"] = operation_key or (f"{task_id}/{alias}/{sha}" if sha else "")
    if status:
        row["publish_status"] = status
    if remote_sha:
        row["remote_sha"] = remote_sha
    return {"id": task_id, "state": state, "repos": {alias: row}}


class FakeRegistry:
    def __init__(self, rows):
        self.rows = {row["id"]: copy.deepcopy(row) for row in rows}
        self.saved = []

    def all(self, *, include_archived=False, include_hub=True):
        assert include_archived is True
        assert include_hub is False
        return [copy.deepcopy(row) for row in self.rows.values()]

    def load(self, task_id, must=False):
        return copy.deepcopy(self.rows.get(task_id))

    def save(self, row):
        self.rows[row["id"]] = copy.deepcopy(row)
        self.saved.append(row["id"])
        return row


class FakePublisher:
    def __init__(self, *, status="published", remote_sha=REMOTE_SHA, message="confirmed"):
        self.calls = []
        self.status = status
        self.remote_sha = remote_sha
        self.message = message

    def __call__(self, cfg, reg, row, alias, landed_sha, **kwargs):
        self.calls.append((row["id"], alias, landed_sha, kwargs))
        return SimpleNamespace(
            status=self.status,
            remote_sha=self.remote_sha,
            message=self.message,
            attempted=True,
            events=(),
        )


class PublishWorkerTests(unittest.TestCase):
    def test_filters_archived_rows_by_alias_policy_sha_and_persisted_key(self):
        with tempfile.TemporaryDirectory() as temp:
            good = task("T001")
            rows = [
                good,
                task("T002", state="active"),
                task("T003", alias="docs"),
                task("T004", operation_key="T004/be/not-the-landed-sha"),
                task("T005", sha="bad-sha"),
                task("T006", alias="web"),
            ]
            repos = {
                "be": FakeRepoCfg(),
                "web": FakeRepoCfg(automatic={"land_to_local": "fxh", "push_only": "dev"}),
            }
            reg = FakeRegistry(rows)
            cfg = FakeConfig(temp, repos)
            publisher = FakePublisher()

            report = publish_worker.run_due_publications(cfg, reg, now=START, publisher=publisher)

            self.assertEqual([call[:3] for call in publisher.calls], [("T001", "be", LANDED_SHA)])
            self.assertEqual(publisher.calls[0][3], {"now": START})  # never opts into retry=True
            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].status, "published")
            self.assertEqual(reg.rows["T001"]["repos"]["be"]["remote_sha"], REMOTE_SHA)
            self.assertEqual(reg.rows["T001"]["repos"]["be"]["publish_status"], "published")

    def test_success_is_idempotently_skipped_after_task_row_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            reg = FakeRegistry([task("T007")])
            cfg = FakeConfig(temp)
            publisher = FakePublisher()

            first = publish_worker.run_due_publications(cfg, reg, now=START, publisher=publisher)
            second = publish_worker.run_due_publications(cfg, reg, now=START, publisher=publisher)

            self.assertEqual(len(first.results), 1)
            self.assertEqual(len(publisher.calls), 1)
            self.assertEqual(second.results[0].skipped_reason, "already_published")

    def test_transient_attempt_runs_only_when_persisted_retry_is_due(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = FakeConfig(temp)
            reg = FakeRegistry([task("T008")])
            store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
            key = f"T008/be/{LANDED_SHA}"
            store.record_failure(key, publish_pending.TRANSIENT, "network timeout", now=START)
            publisher = FakePublisher(status="push_pending", remote_sha=None, message="still offline")

            early = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(seconds=30), publisher=publisher,
            )
            due = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(minutes=1), publisher=publisher,
            )

            self.assertEqual(len(publisher.calls), 1)
            self.assertEqual(early.results[0].skipped_reason, "not_due")
            self.assertTrue(due.results[0].attempted)

    def test_blocked_escalation_is_returned_without_notification_side_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = FakeConfig(temp)
            reg = FakeRegistry([task("T009")])
            store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
            key = f"T009/be/{LANDED_SHA}"
            store.record_failure(key, publish_pending.PERMISSION, "permission denied", now=START)
            publisher = FakePublisher()

            report = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(minutes=60), publisher=publisher,
            )

            self.assertEqual(publisher.calls, [])
            self.assertIn({
                "task_id": "T009", "alias": "be", "operation_key": key, "type": "blocked",
            }, report.events)
            self.assertEqual(reg.rows["T009"]["repos"]["be"]["publish_status"], "blocked")

    def test_blocked_transient_retries_at_hourly_cadence_without_repeating_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = FakeConfig(temp)
            reg = FakeRegistry([task("T010")])
            store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
            key = f"T010/be/{LANDED_SHA}"
            store.record_failure(key, publish_pending.TRANSIENT, "network timeout", now=START)
            publisher = FakePublisher()

            blocked = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(minutes=60), publisher=publisher,
            )
            not_due = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(minutes=119), publisher=publisher,
            )
            due = publish_worker.run_due_publications(
                cfg, reg, now=START + dt.timedelta(minutes=120), publisher=publisher,
            )

            self.assertIn("blocked", [event["type"] for event in blocked.events])
            self.assertEqual(not_due.events, ())
            self.assertEqual(len(publisher.calls), 1)
            self.assertTrue(due.results[0].attempted)
            self.assertEqual(due.events, ())

    def test_recovers_remote_sha_from_published_state_after_task_row_write_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = FakeConfig(temp)
            reg = FakeRegistry([task("T011")])
            store = publish_pending.PublishPendingStore(cfg.state_dir / "publish-pending")
            key = f"T011/be/{LANDED_SHA}"
            store.record_success(key, now=START, remote_sha=REMOTE_SHA)
            publisher = FakePublisher()

            report = publish_worker.run_due_publications(
                cfg, reg, now=START, publisher=publisher,
            )

            self.assertEqual(publisher.calls, [])
            self.assertEqual(reg.rows["T011"]["repos"]["be"]["publish_status"], "published")
            self.assertEqual(reg.rows["T011"]["repos"]["be"]["remote_sha"], REMOTE_SHA)
            self.assertEqual(report.results[0].remote_sha, REMOTE_SHA)


if __name__ == "__main__":
    unittest.main()
