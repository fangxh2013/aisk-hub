#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LaunchAgent wiring and failure-path tests; no launchctl or Git is run."""
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from subprocess import CompletedProcess

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine import cli as engine_cli  # noqa: E402
from engine.worktree import publish_scheduler  # noqa: E402
from engine.worktree.config import WtError  # noqa: E402


class PublishSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launcher = self.root / "bin" / "aisk"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.agent_dir = self.root / "LaunchAgents"
        self.agent_dir.mkdir()
        self.plist_path = self.agent_dir / f"{publish_scheduler.LABEL}.plist"
        self.cfg = SimpleNamespace(
            profile_name="xinhua", os="mac", logs_dir=self.root / "logs",
            state_dir=self.root / "state", locks_dir=self.root / "locks",
        )
        self.target = (
            self.root, self.launcher, self.agent_dir, self.plist_path,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write_owned_plist(self):
        payload = {
            "Label": publish_scheduler.LABEL,
            "ProgramArguments": [str(self.launcher), "--profile", "xinhua", "task", "publish-due"],
        }
        self.plist_path.write_bytes(plistlib.dumps(payload))

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "linux")
    def test_publish_due_rejects_non_macos_even_when_config_labels_it_mac(self):
        with mock.patch.object(publish_scheduler.publish_worker, "run_due_publications") as worker:
            with self.assertRaises(WtError):
                publish_scheduler.cmd_publish_due(self.cfg, None)
        worker.assert_not_called()

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "linux")
    def test_launchd_target_rejects_non_macos_install_context(self):
        with self.assertRaises(WtError):
            publish_scheduler._launchd_target(self.cfg)

    def test_owned_plist_rejects_non_dictionary_and_malformed_program_arguments(self):
        self.plist_path.write_bytes(plistlib.dumps(["not", "a", "dictionary"]))
        with self.assertRaisesRegex(WtError, "顶层必须是字典"):
            publish_scheduler._owned_plist(self.plist_path, self.launcher)

        self.plist_path.write_bytes(plistlib.dumps({
            "Label": publish_scheduler.LABEL,
            "ProgramArguments": str(self.launcher),
        }))
        with self.assertRaisesRegex(WtError, "ProgramArguments"):
            publish_scheduler._owned_plist(self.plist_path, self.launcher)

        self.plist_path.write_bytes(plistlib.dumps({
            "Label": publish_scheduler.LABEL,
            "ProgramArguments": [str(self.launcher), 42],
        }))
        with self.assertRaisesRegex(WtError, "ProgramArguments"):
            publish_scheduler._owned_plist(self.plist_path, self.launcher)

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "darwin")
    def test_remove_preserves_plist_when_launchctl_query_fails_ambiguously(self):
        self._write_owned_plist()
        args = SimpleNamespace(scheduler_action="remove")
        query_error = CompletedProcess(
            args=[], returncode=5, stdout="", stderr="Operation not permitted",
        )
        with mock.patch.object(publish_scheduler, "_launchd_target", return_value=self.target), \
                mock.patch.object(publish_scheduler, "_launchctl", return_value=query_error) as launchctl:
            with self.assertRaisesRegex(WtError, "保留配置文件"):
                publish_scheduler.cmd_scheduler(self.cfg, None, args)

        self.assertTrue(self.plist_path.exists())
        launchctl.assert_called_once_with("print", f"gui/{publish_scheduler.os.getuid()}/{publish_scheduler.LABEL}")

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "darwin")
    def test_remove_deletes_plist_only_for_explicit_missing_service_result(self):
        self._write_owned_plist()
        args = SimpleNamespace(scheduler_action="remove")
        not_loaded = CompletedProcess(
            args=[], returncode=113, stdout="", stderr="Could not find service in domain",
        )
        with mock.patch.object(publish_scheduler, "_launchd_target", return_value=self.target), \
                mock.patch.object(publish_scheduler, "_launchctl", return_value=not_loaded) as launchctl, \
                mock.patch.object(publish_scheduler, "say"):
            result = publish_scheduler.cmd_scheduler(self.cfg, None, args)

        self.assertEqual(result, 0)
        self.assertFalse(self.plist_path.exists())
        launchctl.assert_called_once_with("print", f"gui/{publish_scheduler.os.getuid()}/{publish_scheduler.LABEL}")

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "darwin")
    def test_install_does_not_overwrite_or_bootstrap_when_load_state_is_unknown(self):
        args = SimpleNamespace(scheduler_action="install")
        query_error = CompletedProcess(
            args=[], returncode=5, stdout="", stderr="Operation not permitted",
        )
        with mock.patch.object(publish_scheduler, "_launchd_target", return_value=self.target), \
                mock.patch.object(publish_scheduler, "_launchctl", return_value=query_error) as launchctl:
            with self.assertRaisesRegex(WtError, "未覆盖配置"):
                publish_scheduler.cmd_scheduler(self.cfg, None, args)

        self.assertFalse(self.plist_path.exists())
        launchctl.assert_called_once_with("print", f"gui/{publish_scheduler.os.getuid()}/{publish_scheduler.LABEL}")

    def test_global_profile_argument_is_forwarded_to_task_parser(self):
        self.assertEqual(
            engine_cli._split_task([
                "--profile", "xinhua", "task", "publish-due",
            ]),
            ("xinhua", ["publish-due"]),
        )

    @mock.patch("engine.worktree.publish_scheduler.sys.platform", "darwin")
    def test_no_worker_events_and_no_pending_log_do_not_send_notifications(self):
        report = SimpleNamespace(results=(), events=())
        with mock.patch.object(
                publish_scheduler.publish_worker, "run_due_publications", return_value=report), \
                mock.patch("engine.worktree.publish_notify.subprocess.run") as osascript, \
                mock.patch.object(publish_scheduler, "say"):
            result = publish_scheduler.cmd_publish_due(self.cfg, None)

        self.assertEqual(result, 0)
        osascript.assert_not_called()


if __name__ == "__main__":
    unittest.main()
