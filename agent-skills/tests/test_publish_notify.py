#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable publish notification tests; never invokes the real osascript."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from subprocess import CompletedProcess

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import publish_notify  # noqa: E402


def event(task_id="T042", *, alias="web", event_type="needs_attention"):
    return {
        "task_id": task_id,
        "alias": alias,
        "operation_key": f"{task_id}/{alias}/" + "a" * 40,
        "type": event_type,
    }


class PublishNotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp.name) / "notifications.json"

    def tearDown(self):
        self.temp.cleanup()

    def _store(self):
        return json.loads(self.log_path.read_text(encoding="utf-8"))

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.shutil.which", return_value="/usr/bin/osascript")
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_notification_uses_static_script_and_separate_safe_arguments(
        self, run, _which,
    ):
        run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        malicious_task_id = 'T1"; do shell script "touch /tmp/should-not-exist"'

        result = publish_notify.notify_publish_event(
            event(malicious_task_id), log_path=self.log_path,
        )

        self.assertEqual(result.status, "notified")
        self.assertTrue(result.persisted)
        self.assertTrue(result.notified)
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[:2], ["/usr/bin/osascript", "-e"])
        self.assertEqual(command[2], publish_notify._NOTIFICATION_SCRIPT)
        self.assertNotIn(malicious_task_id, command[2])
        self.assertIn(malicious_task_id, command[3])
        self.assertEqual(command[4], "web · 发布需要人工关注")
        self.assertNotIn("shell", run.call_args.kwargs)

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.shutil.which", return_value="/usr/bin/osascript")
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_repeated_event_is_persistently_deduped(self, run, _which):
        run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        first = publish_notify.notify_publish_event(event(), log_path=self.log_path)
        second = publish_notify.notify_publish_event(event(), log_path=self.log_path)

        self.assertEqual(first.status, "notified")
        self.assertEqual(second.status, "duplicate")
        self.assertTrue(second.duplicate)
        self.assertTrue(second.success)
        run.assert_called_once()
        store = self._store()
        self.assertEqual(len(store["events"]), 1)
        record = next(iter(store["events"].values()))
        self.assertEqual(record["notification_status"], "notified")
        self.assertEqual(record["attempts"], 1)

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.shutil.which", return_value=None)
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_missing_osascript_falls_back_to_persistent_event_log(self, run, _which):
        first = publish_notify.notify_publish_event(event(), log_path=self.log_path)
        second = publish_notify.notify_publish_event(event(), log_path=self.log_path)

        self.assertEqual(first.status, "logged_only")
        self.assertFalse(first.success)
        self.assertTrue(first.persisted)
        self.assertIn("osascript was not found", first.error)
        self.assertEqual(second.status, "logged_only")
        self.assertTrue(second.duplicate)
        run.assert_not_called()
        store = self._store()
        self.assertEqual(len(store["events"]), 1)
        record = next(iter(store["events"].values()))
        self.assertEqual(record["notification_status"], "pending")
        self.assertIn("last_error", record)

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.shutil.which", return_value="/usr/bin/osascript")
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_delivery_failure_is_reported_and_event_can_be_retried(self, run, _which):
        run.side_effect = [
            CompletedProcess(args=[], returncode=1, stdout="", stderr="permission denied"),
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        failed = publish_notify.notify_publish_event(event(), log_path=self.log_path)
        retried = publish_notify.notify_publish_event(event(), log_path=self.log_path)

        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.persisted)
        self.assertFalse(failed.notified)
        self.assertIn("permission denied", failed.error)
        self.assertEqual(retried.status, "notified")
        self.assertTrue(retried.duplicate)
        run.assert_has_calls([mock.call(
            mock.ANY, capture_output=True, text=True,
            timeout=publish_notify.DEFAULT_TIMEOUT_SECONDS, check=False,
        )] * 2)
        store = self._store()
        self.assertEqual(len(store["events"]), 1)
        record = next(iter(store["events"].values()))
        self.assertEqual(record["notification_status"], "notified")
        self.assertEqual(record["attempts"], 2)

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_pending_event_drain_retries_after_original_worker_emission(self, run):
        run.side_effect = [
            CompletedProcess(args=[], returncode=1, stdout="", stderr="temporary failure"),
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
        first = publish_notify.notify_publish_event(
            event(), log_path=self.log_path, osascript_path="/fake/osascript",
        )
        drained = publish_notify.drain_pending_notifications(
            log_path=self.log_path, osascript_path="/fake/osascript",
        )

        self.assertEqual(first.status, "failed")
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].event_id, first.event_id)
        self.assertEqual(drained[0].status, "notified")
        run.assert_has_calls([mock.ANY, mock.ANY])
        record = self._store()["events"][first.event_id]
        self.assertEqual(record["notification_status"], "notified")
        self.assertEqual(record["attempts"], 2)

    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_empty_pending_log_drain_does_not_attempt_notification(self, run):
        self.assertEqual(
            publish_notify.drain_pending_notifications(log_path=self.log_path), (),
        )
        run.assert_not_called()

    @mock.patch("engine.worktree.publish_notify.sys.platform", "darwin")
    @mock.patch("engine.worktree.publish_notify.shutil.which", return_value=None)
    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_explicit_id_cannot_alias_different_event_data(self, run, _which):
        first = publish_notify.notify_publish_event(
            event("T050"), event_id="caller-event-1", log_path=self.log_path,
        )
        collision = publish_notify.notify_publish_event(
            event("T051"), event_id="caller-event-1", log_path=self.log_path,
        )

        self.assertEqual(first.status, "logged_only")
        self.assertEqual(collision.status, "failed")
        self.assertIn("different event data", collision.error)
        self.assertEqual(len(self._store()["events"]), 1)
        run.assert_not_called()

    @mock.patch("engine.worktree.publish_notify.subprocess.run")
    def test_non_escalation_worker_event_is_rejected(self, run):
        result = publish_notify.notify_publish_event(
            event(event_type="push_pending"), log_path=self.log_path,
        )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.persisted)
        self.assertIn("event.type", result.error)
        self.assertFalse(self.log_path.exists())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
