"""Autoflow recovery tests never create Git worktrees or change refs."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.worktree import autoflow  # noqa: E402


class QueuedAutoflowRecovery(unittest.TestCase):
    def test_rejected_finish_with_valid_checkpoint_resumes_land_without_new_paths(self):
        tip = "a" * 40
        task = {
            "id": "T102", "state": "rejected", "os": "mac", "dir": "/tmp/autoflow-task",
            "title": "recover rejected land", "scope": ["src"],
            "repos": {"be": {"path": "/tmp/autoflow-repo", "branch": "cx/T102-be",
                              "ready_sha": tip}},
            "check": {"results": {"be": {"sha": tip, "ok": True}}},
        }
        repo_cfg = SimpleNamespace(
            workspace_mode="task-worktree",
            automatic={"commit_task_branch": True, "land_to_local": "fxh", "push_only": "fxh-dev",
                       "archive_after_verified_land": True},
            path=Path("/tmp/autoflow-repo"),
        )
        cfg = SimpleNamespace(profile_name="xinhua", integration="fxh", repo=lambda alias: repo_cfg)
        reg = Mock()
        reg.find_by_ref.return_value = task
        args = SimpleNamespace(task="T102", repo="be", paths=[], all=False, message="")
        with patch.object(autoflow.integrate, "land_automatic", return_value=1) as land, \
             patch.object(autoflow.tasks, "cmd_commit") as commit:
            self.assertEqual(autoflow.cmd_finish(cfg, reg, args), 1)

        land.assert_called_once_with(cfg, reg, task, "be")
        commit.assert_not_called()

    def test_rejected_finish_without_valid_checkpoint_requires_repair_path(self):
        task = {
            "id": "T103", "state": "rejected", "os": "mac", "dir": "/tmp/autoflow-task",
            "title": "repair rejected task", "scope": ["src"],
            "repos": {"be": {"path": "/tmp/autoflow-repo", "branch": "cx/T103-be",
                              "ready_sha": "a" * 40}},
            "check": {"results": {"be": {"sha": "stale", "ok": True}}},
        }
        repo_cfg = SimpleNamespace(workspace_mode="task-worktree", automatic={
            "commit_task_branch": True, "land_to_local": "fxh", "push_only": "fxh-dev",
            "archive_after_verified_land": True,
        }, path=Path("/tmp/autoflow-repo"))
        cfg = SimpleNamespace(profile_name="xinhua", integration="fxh", repo=lambda alias: repo_cfg)
        reg = Mock()
        reg.find_by_ref.return_value = task
        args = SimpleNamespace(task="T103", repo="be", paths=[], all=False, message="")
        with patch.object(autoflow.integrate, "land_automatic") as land:
            with self.assertRaisesRegex(autoflow.WtError, "逐个声明 --path"):
                autoflow.cmd_finish(cfg, reg, args)
        land.assert_not_called()

    def test_queued_finish_reuses_ready_checkpoint_without_commit_or_new_paths(self):
        tip = "a" * 40
        remote = "b" * 40
        task = {
            "id": "T100", "state": "queued", "os": "mac", "dir": "/tmp/autoflow-task",
            "title": "recover queued land", "scope": ["src"],
            "repos": {"be": {"path": "/tmp/autoflow-repo", "branch": "cx/T100-be",
                              "ready_sha": tip}},
            "check": {"results": {"be": {"sha": tip, "ok": True}}},
        }
        repo_cfg = SimpleNamespace(
            workspace_mode="task-worktree",
            automatic={"commit_task_branch": True, "land_to_local": "fxh", "push_only": "fxh-dev",
                       "archive_after_verified_land": True},
            path=Path("/tmp/autoflow-repo"),
        )
        cfg = SimpleNamespace(profile_name="xinhua", integration="fxh", repo=lambda alias: repo_cfg)
        reg = Mock()
        reg.find_by_ref.return_value = task
        reg.load.return_value = task

        def land(_cfg, _reg, current, alias):
            current["repos"][alias]["landed_sha"] = tip
            current["state"] = "landed"
            return 0

        def archive(_cfg, _reg, _args):
            task["state"] = "archived"
            return 0

        published = SimpleNamespace(status="published", remote_sha=remote, published=True, events=())
        args = SimpleNamespace(task="T100", repo="be", paths=[], all=False, message="")
        with patch.object(autoflow.integrate, "land_automatic", side_effect=land) as land_call, \
             patch.object(autoflow.tasks, "cmd_commit") as commit, \
             patch.object(autoflow.tasks, "cmd_check") as check, \
             patch.object(autoflow.tasks, "cmd_ready") as ready, \
             patch.object(autoflow.git, "sha", return_value=tip), \
             patch.object(autoflow.git, "is_ancestor", return_value=True), \
             patch.object(autoflow.tasks, "cmd_archive", side_effect=archive), \
             patch.object(autoflow.publish_driver, "publish_task_branch", return_value=published), \
             patch.object(autoflow.tasks, "refresh_board"):
            self.assertEqual(autoflow.cmd_finish(cfg, reg, args), 0)

        land_call.assert_called_once()
        commit.assert_not_called()
        check.assert_not_called()
        ready.assert_not_called()
        self.assertEqual(task["state"], "archived")
        self.assertEqual(task["repos"]["be"]["remote_sha"], remote)

    def test_queued_finish_refuses_missing_or_stale_ready_checkpoint(self):
        task = {
            "id": "T101", "state": "queued", "os": "mac", "dir": "/tmp/autoflow-task",
            "title": "bad checkpoint", "repos": {"be": {"path": "/tmp/autoflow-repo", "ready_sha": "a" * 40}},
            "check": {"results": {"be": {"sha": "different", "ok": True}}},
        }
        repo_cfg = SimpleNamespace(workspace_mode="task-worktree", automatic={
            "commit_task_branch": True, "land_to_local": "fxh", "push_only": "fxh-dev",
            "archive_after_verified_land": True,
        }, path=Path("/tmp/autoflow-repo"))
        cfg = SimpleNamespace(profile_name="xinhua", integration="fxh", repo=lambda alias: repo_cfg)
        reg = Mock()
        reg.find_by_ref.return_value = task
        args = SimpleNamespace(task="T101", repo="be", paths=[], all=False, message="")
        with patch.object(autoflow.integrate, "land_automatic") as land:
            with self.assertRaisesRegex(autoflow.WtError, "ready SHA"):
                autoflow.cmd_finish(cfg, reg, args)
        land.assert_not_called()


if __name__ == "__main__":
    unittest.main()
