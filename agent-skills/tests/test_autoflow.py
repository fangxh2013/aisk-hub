"""Finish orchestration contracts, with no Git, network, or worktree effects.

The stage implementations have their own integration tests. These tests exercise
the real cmd_finish dispatcher against persisted task snapshots, and establish
which later stages may run after each success or failure.
"""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.worktree import autoflow, publish_notify, publish_pending, publish_scheduler  # noqa: E402


class FinishOrchestration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-finish-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_root = self.root / "task-be"
        (self.task_root / "src").mkdir(parents=True)
        self.alias = "be"
        self.tip = "a" * 40
        self.remote = "b" * 40
        self.stage_names = ["commit", "check", "ready", "land", "archive", "publish"]
        self.events = []
        self.fail_at = None
        self.failure_code = 7
        self.publish_result = SimpleNamespace(
            status="published", remote_sha=self.remote, published=True, events=(),
        )
        self.task = {
            "id": "T200", "state": "active", "scope": ["src"],
            "repos": {"be": {"path": str(self.task_root),
                              "branch": "cx/T200-be"}},
        }
        self.repo_cfg = SimpleNamespace(
            path=self.root / "integration-be",
            workspace_mode="task-worktree",
            automatic={"commit_task_branch": True, "land_to_local": "fxh",
                       "push_only": "fxh-dev", "archive_after_verified_land": True},
        )
        self.cfg = SimpleNamespace(
            profile_name="xinhua", integration="fxh", repo=lambda alias: self.repo_cfg,
            state_dir=self.root / "state", locks_dir=self.root / "locks", os="mac",
        )
        self.args = SimpleNamespace(
            task="T200", repo=self.alias, paths=["src/change.py"], all=False,
            message="feat: scoped change", tool="codex", session="test-session",
        )
        self.reg = Mock()
        self.reg.find_by_ref.side_effect = self.load
        self.reg.load.side_effect = self.load
        self.reg.all.side_effect = lambda **kwargs: [deepcopy(self.task)]
        self.reg.save.side_effect = self.save
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stages = {}
        for name, module, attribute, callback in (
            ("commit", autoflow.tasks, "cmd_commit", self.commit),
            ("check", autoflow.tasks, "cmd_check", self.check),
            ("ready", autoflow.tasks, "cmd_ready", self.ready),
            ("land", autoflow.integrate, "land_automatic", self.land),
            ("archive", autoflow.tasks, "cmd_archive", self.archive),
            ("publish", autoflow.publish_driver, "publish_task_branch", self.publish),
        ):
            self.stages[name] = self.stack.enter_context(
                patch.object(module, attribute, side_effect=callback))
        self.stack.enter_context(patch.object(autoflow.tasks, "caller", return_value=("codex", {"test-session"})))
        self.stack.enter_context(patch.object(autoflow.tasks, "refuse_if_foreign"))
        self.board = self.stack.enter_context(patch.object(autoflow.tasks, "refresh_board"))
        self.stack.enter_context(patch.object(autoflow, "say"))
        self.diff = self.stack.enter_context(patch.object(autoflow.git, "out", return_value="src/change.py\0"))
        self.sha = self.stack.enter_context(patch.object(autoflow.git, "sha", return_value=self.tip))
        self.ancestor = self.stack.enter_context(patch.object(autoflow.git, "is_ancestor", return_value=True))
        self.dirty = self.stack.enter_context(patch.object(autoflow.git, "dirty", return_value=""))
        self.stack.enter_context(patch.object(
            autoflow.git, "current_branch", side_effect=lambda repo: self.task["repos"][self.alias]["branch"]))
        self.stack.enter_context(patch.object(
            autoflow.git, "run", side_effect=AssertionError("unexpected real Git operation")))

    def load(self, task_id, **kwargs):
        self.assertEqual(task_id, self.task["id"])
        return deepcopy(self.task)

    def save(self, task):
        self.task = deepcopy(task)

    def enter_stage(self, name):
        self.events.append(name)
        return self.failure_code if self.fail_at == name else 0

    def commit(self, cfg, reg, args):
        self.assertIs(cfg, self.cfg)
        self.assertIs(reg, self.reg)
        self.assertEqual(args.repos, self.alias)
        self.assertEqual(args.paths, ["src/change.py"])
        self.assertEqual(args.message, "feat: scoped change")
        self.assertFalse(args.all)
        self.assertTrue(args.autoflow)
        rc = self.enter_stage("commit")
        if not rc:
            self.task["repos"][self.alias]["autoflow_checkpoint"] = {
                "version": 1, "branch": self.task["repos"][self.alias]["branch"],
                "sha": self.tip, "paths": list(args.paths), "message": args.message,
            }
        return rc

    def check(self, cfg, reg, args):
        rc = self.enter_stage("check")
        self.task["check"] = {"results": {self.alias: {"sha": self.tip, "ok": not rc}}}
        return rc

    def ready(self, cfg, reg, args):
        rc = self.enter_stage("ready")
        if not rc:
            self.assertTrue(self.task["check"]["results"][self.alias]["ok"])
            self.task["state"] = "ready"
            self.task["repos"][self.alias]["ready_sha"] = self.tip
        return rc

    def land(self, cfg, reg, task, alias):
        self.assertEqual(alias, self.alias)
        self.assertEqual(task["state"], "ready")
        self.assertEqual(task["repos"][alias]["ready_sha"], self.tip)
        rc = self.enter_stage("land")
        if not rc:
            task["state"] = "landed"
            task["repos"][alias]["landed_sha"] = self.tip
            reg.save(task)
        return rc

    def archive(self, cfg, reg, args):
        # Publication recovery metadata and local containment must precede removal.
        self.assertEqual(self.task["state"], "landed")
        self.assertEqual(self.task["repos"][self.alias]["publish_operation_key"],
                         f"T200/{self.alias}/{self.tip}")
        self.assertTrue(self.task["repos"][self.alias]["automatic_publish_started_at"])
        self.ancestor.assert_called_once_with(self.repo_cfg.path, self.tip, "fxh")
        self.assertEqual(args.task, "T200")
        self.assertFalse(args.abandon)
        rc = self.enter_stage("archive")
        if not rc:
            self.task["state"] = "archived"
        return rc

    def publish(self, cfg, reg, task, alias, sha):
        self.assertEqual(task["state"], "archived")
        self.assertEqual(alias, self.alias)
        self.assertEqual(sha, self.tip)
        self.enter_stage("publish")
        return self.publish_result

    def finish(self):
        return autoflow.cmd_finish(self.cfg, self.reg, self.args)

    def assert_stops_at(self, phase, state):
        self.fail_at = phase
        self.assertEqual(self.finish(), self.failure_code)
        index = self.stage_names.index(phase)
        self.assertEqual(self.events, self.stage_names[:index + 1])
        for name in self.stage_names[index + 1:]:
            self.stages[name].assert_not_called()
        self.assertEqual(self.task["state"], state)
        self.assertNotIn("remote_sha", self.task["repos"][self.alias])

    def assert_happy_path(self):
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.events, self.stage_names)
        for stage in self.stages.values():
            stage.assert_called_once()
        self.diff.assert_called_once_with(
            ["diff", "--cached", "--name-only", "-z"],
            cwd=self.task["repos"][self.alias]["path"],
        )
        self.sha.assert_any_call(self.repo_cfg.path, "fxh")
        self.assertEqual(self.task["state"], "archived")
        row = self.task["repos"][self.alias]
        self.assertEqual(row["landed_sha"], self.tip)
        self.assertEqual(row["publish_status"], "published")
        self.assertEqual(row["remote_sha"], self.remote)
        self.assertNotIn("publish_error", row)
        self.board.assert_called_once_with(self.cfg, self.reg)

    def test_active_backend_finishes_in_order_and_persists_published_result(self):
        self.assert_happy_path()

    def test_active_frontend_finishes_as_its_own_repository(self):
        self.task["repos"]["web"] = self.task["repos"].pop("be")
        web_root = self.root / "task-web"
        (web_root / "src").mkdir(parents=True)
        self.task["repos"]["web"].update(path=str(web_root), branch="cx/T200-web")
        self.alias = self.args.repo = "web"
        self.repo_cfg.path = self.root / "integration-web"
        self.assert_happy_path()

    def test_commit_failure_stops_before_gate(self):
        self.assert_stops_at("commit", "active")

    def test_gate_failure_never_readies_lands_archives_or_publishes(self):
        self.assert_stops_at("check", "active")
        self.assertFalse(self.task["check"]["results"][self.alias]["ok"])
        self.assertNotIn("publish_operation_key", self.task["repos"][self.alias])

    def test_ready_failure_stops_before_land(self):
        self.assert_stops_at("ready", "active")

    def test_land_failure_preserves_ready_checkpoint_without_archive_or_publish(self):
        self.assert_stops_at("land", "ready")

    def test_fxh_missing_task_commit_prevents_archive_and_publish(self):
        self.ancestor.return_value = False
        with self.assertRaisesRegex(autoflow.WtError, "保留 worktree"):
            self.finish()
        self.assertEqual(self.events, self.stage_names[:4])
        self.assertEqual(self.task["state"], "landed")
        self.stages["archive"].assert_not_called()
        self.stages["publish"].assert_not_called()
        self.assertNotIn("publish_operation_key", self.task["repos"][self.alias])

    def test_archive_failure_keeps_recovery_metadata_and_never_publishes(self):
        self.assert_stops_at("archive", "landed")
        self.assertEqual(self.task["repos"][self.alias]["publish_operation_key"],
                         f"T200/{self.alias}/{self.tip}")

    def test_pending_push_returns_failure_and_preserves_archived_task_for_retry(self):
        self.publish_result = SimpleNamespace(
            status="push_pending", remote_sha=None, published=False,
            message="temporary network failure", events=(),
        )
        self.assertEqual(self.finish(), 1)
        self.assertEqual(self.events, self.stage_names)
        self.assertEqual(self.task["state"], "archived")
        row = self.task["repos"][self.alias]
        self.assertEqual(row["publish_status"], "push_pending")
        self.assertEqual(row["publish_error"], "temporary network failure")
        self.assertNotIn("remote_sha", row)
        self.board.assert_called_once_with(self.cfg, self.reg)

    def test_completed_finish_reentry_does_not_repeat_any_delivery_stage(self):
        self.assert_happy_path()
        before = deepcopy(self.task)
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.events, self.stage_names)
        self.assertEqual(self.task, before)
        for stage in self.stages.values():
            stage.assert_called_once()

    def test_path_normalization_blocks_parent_traversal_and_scope_escaping_symlink(self):
        outside_scope = self.task_root / "outside-scope"
        outside_scope.mkdir()
        (outside_scope / "private.txt").write_text("unrelated draft\n", encoding="utf-8")
        (self.task_root / "src" / "link").symlink_to(outside_scope, target_is_directory=True)
        for path in ("src/..", "src/../AGENTS.md", "src/../../escape.txt", "src/link/private.txt"):
            with self.subTest(path=path):
                self.args.paths = [path]
                self.diff.return_value = ""
                with self.assertRaises(autoflow.WtError):
                    self.finish()
                self.assertEqual(self.events, [])
                for stage in self.stages.values():
                    stage.assert_not_called()

    def test_gate_failure_retry_reuses_committed_head_and_does_not_attempt_empty_commit(self):
        self.fail_at = "check"
        self.assertEqual(self.finish(), self.failure_code)
        self.assertEqual(self.events, ["commit", "check"])
        self.stages["commit"].side_effect = autoflow.WtError("empty commit must not be attempted")
        self.diff.return_value = ""
        self.fail_at = None

        self.assertEqual(self.finish(), 0)

        self.assertEqual(self.events, ["commit", "check", "check", "ready", "land", "archive", "publish"])
        self.stages["commit"].assert_called_once()
        self.assertEqual(self.stages["check"].call_count, 2)
        self.assertEqual(self.task["repos"][self.alias]["publish_status"], "published")

    def prepare_archived_task(self):
        key = f"T200/{self.alias}/{self.tip}"
        self.task["state"] = "archived"
        self.task["repos"][self.alias].update(
            ready_sha=self.tip, landed_sha=self.tip, publish_operation_key=key,
        )
        return key

    def test_finish_initial_escalation_is_durable_before_desktop_notification(self):
        key = self.prepare_archived_task()
        current = dt.datetime.now(dt.timezone.utc)
        for status in ("needs_attention", "blocked"):
            with self.subTest(status=status):
                self.cfg.state_dir = self.root / status
                store = publish_pending.PublishPendingStore(self.cfg.state_dir / "publish-pending")
                result = store.record_failure(key, publish_pending.PERMISSION, "permission denied", now=current)
                if status == "blocked":
                    result = store.advance(key, now=current + dt.timedelta(minutes=61))
                self.publish_result = SimpleNamespace(
                    status=status, remote_sha=None, published=False, message="permission denied",
                    events=(status,), operation_key=key, state=result.state,
                )
                log = self.cfg.state_dir / "publish-notifications.json"

                def deliver(argv, **kwargs):
                    records = json.loads(log.read_text(encoding="utf-8"))["events"].values()
                    matching = [record for record in records if record["event"]["type"] == status]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(matching[0]["notification_status"], "pending")
                    return CompletedProcess(argv, 0, stdout="", stderr="")

                with patch.object(publish_notify.sys, "platform", "darwin"), \
                        patch.object(publish_notify.shutil, "which", return_value="/fake/osascript"), \
                        patch.object(publish_notify.subprocess, "run", side_effect=deliver) as transport, \
                        patch.object(publish_notify, "notify_publish_event", wraps=publish_notify.notify_publish_event) as notify:
                    self.assertEqual(self.finish(), 1)
                notify.assert_called()
                transport.assert_called_once()
                records = json.loads(log.read_text(encoding="utf-8"))["events"].values()
                self.assertTrue(any(record["event"]["type"] == status
                                    and record["notification_status"] == "notified" for record in records))

    def test_scheduler_recovers_escalation_when_finish_crashes_after_pending_save(self):
        key = self.prepare_archived_task()
        store = publish_pending.PublishPendingStore(self.cfg.state_dir / "publish-pending")

        def interrupted_publish(*args, **kwargs):
            store.record_failure(key, publish_pending.PERMISSION, "permission denied")
            raise KeyboardInterrupt("process ended after durable pending transition")

        self.stages["publish"].side_effect = interrupted_publish
        with self.assertRaises(KeyboardInterrupt):
            self.finish()
        state = store.get(key)
        self.assertEqual(state["status"], "needs_attention")
        self.assertIn("needs_attention", state["escalations_emitted"])
        log = self.cfg.state_dir / "publish-notifications.json"
        self.assertFalse(log.exists())
        self.stages["publish"].reset_mock()

        def deliver(argv, **kwargs):
            records = json.loads(log.read_text(encoding="utf-8"))["events"].values()
            self.assertTrue(any(record["event"]["type"] == "needs_attention" for record in records))
            return CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(publish_notify.sys, "platform", "darwin"), \
                patch.object(publish_notify.shutil, "which", return_value="/fake/osascript"), \
                patch.object(publish_notify.subprocess, "run", side_effect=deliver) as transport, \
                patch.object(publish_scheduler, "say"):
            self.assertEqual(publish_scheduler.cmd_publish_due(self.cfg, self.reg), 0)
            self.assertEqual(publish_scheduler.cmd_publish_due(self.cfg, self.reg), 0)
        self.stages["publish"].assert_not_called()
        transport.assert_called_once()
        records = json.loads(log.read_text(encoding="utf-8"))["events"].values()
        self.assertTrue(any(record["event"]["type"] == "needs_attention"
                            and record["notification_status"] == "notified" for record in records))


class AutoflowCommitCheckpoint(unittest.TestCase):
    def test_commit_saves_finish_checkpoint_with_commit_intent_clear(self):
        from engine.worktree import tasks

        base, head, tree = "b" * 40, "a" * 40, "c" * 40
        repo = Path("/tmp/aisk-autoflow-checkpoint-repo")
        task = {
            "id": "T201", "state": "active", "owner": None,
            "repos": {"be": {"branch": "cx/T201-be", "path": str(repo), "ready_sha": None}},
        }
        cfg = SimpleNamespace(repo_order=["be"], message_hint="type: summary")
        reg = Mock()
        reg.lock.return_value = nullcontext()
        reg.find_by_ref.return_value = task
        snapshots = []
        reg.save.side_effect = lambda saved: snapshots.append(deepcopy(saved))
        args = SimpleNamespace(
            task="T201", repos="be", message="feat: checkpoint finish", paths=["src/change.py"],
            all=False, autoflow=True, tool="codex", session="test-session",
        )

        def git_out(argv, cwd=None):
            if argv[:3] == ["diff", "--cached", "--name-only"]:
                return "src/change.py\0"
            if argv == ["write-tree"]:
                return tree
            if argv[:3] == ["show", "-s", "--format=%P"]:
                return base
            if argv[:2] == ["rev-parse", "--verify"]:
                return tree
            raise AssertionError(argv)

        with ExitStack() as stack:
            stack.enter_context(patch.object(tasks, "caller", return_value=("codex", {"test-session"})))
            stack.enter_context(patch.object(tasks, "require_local"))
            stack.enter_context(patch.object(tasks, "refuse_if_foreign"))
            stack.enter_context(patch.object(tasks, "select_repos", return_value=["be"]))
            stack.enter_context(patch.object(tasks.model, "check_message", return_value=None))
            stack.enter_context(patch.object(tasks.git, "current_branch", return_value="cx/T201-be"))
            stack.enter_context(patch.object(tasks.git, "sha", side_effect=[base, head]))
            stack.enter_context(patch.object(tasks.git, "out", side_effect=git_out))
            git_run = stack.enter_context(patch.object(tasks.git, "run"))
            stack.enter_context(patch.object(tasks, "append_progress"))
            stack.enter_context(patch.object(tasks, "refresh_board"))

            self.assertEqual(tasks.cmd_commit(cfg, reg, args), 0)

        self.assertTrue(any(call.args[0][0] == "commit" for call in git_run.call_args_list))
        final = snapshots[-1]["repos"]["be"]
        self.assertNotIn("commit_intent", final)
        self.assertEqual(final["autoflow_checkpoint"], {
            "version": 1, "branch": "cx/T201-be", "sha": head,
            "paths": ["src/change.py"], "message": "feat: checkpoint finish",
        })


if __name__ == "__main__":
    unittest.main()
