# -*- coding: utf-8 -*-
"""Regression tests for the unconditional, non-configurable main write guard."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.worktree import gitops as git  # noqa: E402
from engine.worktree import guards, integrate  # noqa: E402


class MainBranchGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-main-guard-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def repo(self, name, branch):
        path = self.root / name
        path.mkdir()
        git.run(["init", "--quiet", "-b", branch], cwd=path)
        return path

    @staticmethod
    def cfg(*, integration="fxh", raw=None):
        return SimpleNamespace(os="mac", integration=integration, raw=raw or {})

    def test_engine_main_rejection_is_independent_of_profile_switch(self):
        for raw in ({}, {"merge_policy": {"forbid_main_operations": False}}):
            with self.subTest(raw=raw), self.assertRaises(integrate.Reject):
                integrate.reject_main_target(self.cfg(raw=raw), "refs/heads/main")

    def test_land_and_promote_reject_main_before_git_or_registry_access(self):
        with self.assertRaises(integrate.Reject):
            integrate.cmd_land(self.cfg(integration="main"), Mock(), SimpleNamespace(dry_run=False))

        rc = SimpleNamespace(push_branch="main", trunk="dev", promote="push-only")
        with self.assertRaises(integrate.Reject):
            integrate.promote_one(self.cfg(raw={}), Mock(), "be", rc, dry=False)

    def test_revert_refuses_main_integration_before_loading_task(self):
        reg = Mock()
        with self.assertRaises(integrate.Reject):
            integrate.cmd_revert(self.cfg(integration="main"), reg, SimpleNamespace(task="T001"))
        reg.find_by_ref.assert_not_called()

    def test_master_behavior_is_unchanged(self):
        integrate.reject_main_target(self.cfg(raw={}), "master")
        with self.assertRaises(integrate.Reject):
            integrate.reject_main_target(
                self.cfg(raw={"merge_policy": {"forbid_main_operations": True}}), "master")

    def test_task_tool_guard_rejects_explicit_main_target_without_policy(self):
        repo = self.repo("feature", "feature/test")
        cfg = SimpleNamespace(
            anchors_dir=self.root / "anchors", hub=self.root / "hub", state_dir=self.root / "state",
            tasks_dir=self.root / "tasks", os="mac", repos={}, legacy_root=None,
            raw={"merge_policy": {"forbid_main_operations": False}}, protected=[], forbidden_commands={})
        with self.assertRaises(guards.Deny):
            guards.check_bash(cfg, "git merge origin/main", str(repo), str(repo))

    def test_tool_guard_rejects_file_edits_and_git_writes_on_main(self):
        repo = self.repo("main-checkout", "main")
        edit = {"tool_name": "Edit", "cwd": str(repo),
                "tool_input": {"file_path": str(repo / "src" / "change.py")}}
        reason = guards.evaluate(edit, "codex")
        self.assertIn("禁止 AI 修改 main", reason)

        commit = {"tool_name": "Bash", "cwd": str(repo),
                  "tool_input": {"command": "git commit -m 'test: change'"}}
        reason = guards.evaluate(commit, "codex")
        self.assertIn("禁止 AI 在 main 分支", reason)

    def test_tool_guard_rejects_push_refspec_to_main_and_allows_read_only(self):
        repo = self.repo("push-checkout", "feature/test")
        push = {"tool_name": "Bash", "cwd": str(repo),
                "tool_input": {"command": "git push origin HEAD:refs/heads/main"}}
        reason = guards.evaluate(push, "codex")
        self.assertIn("禁止 AI 修改 main", reason)

        main_repo = self.repo("read-only-main", "main")
        status = {"tool_name": "Bash", "cwd": str(main_repo),
                  "tool_input": {"command": "git status --short"}}
        self.assertIsNone(guards.evaluate(status, "codex"))


if __name__ == "__main__":
    unittest.main()
