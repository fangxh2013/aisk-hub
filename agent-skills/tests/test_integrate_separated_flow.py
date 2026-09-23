# -*- coding: utf-8 -*-
"""Regression tests for CAS landing and separated Xinhua delivery operations."""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.worktree import gitops as git  # noqa: E402
from engine.worktree import integrate  # noqa: E402


class FastForwardCas(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-land-cas-")
        self.repo = Path(self.temp.name).resolve()
        git.run(["init", "--quiet", "-b", "fxh"], cwd=self.repo)
        git.run(["config", "user.name", "Aisk test"], cwd=self.repo)
        git.run(["config", "user.email", "aisk-test@example.invalid"], cwd=self.repo)
        (self.repo / "changed.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "unrelated.txt").write_text("base\n", encoding="utf-8")
        git.run(["add", "."], cwd=self.repo)
        git.run(["commit", "--quiet", "-m", "test: base"], cwd=self.repo)
        self.old = git.sha(self.repo, "fxh")

        git.run(["checkout", "--quiet", "-b", "task/candidate"], cwd=self.repo)
        (self.repo / "changed.txt").write_text("candidate\n", encoding="utf-8")
        git.run(["commit", "--quiet", "-am", "test: candidate"], cwd=self.repo)
        self.candidate = git.sha(self.repo, "HEAD")
        git.run(["checkout", "--quiet", "fxh"], cwd=self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def test_cas_fast_forward_preserves_unrelated_staged_work(self):
        (self.repo / "unrelated.txt").write_text("staged local draft\n", encoding="utf-8")
        git.run(["add", "unrelated.txt"], cwd=self.repo)

        landed = integrate.ff_compare_and_swap(self.repo, "fxh", self.old, self.candidate)

        self.assertEqual(landed, self.candidate)
        self.assertEqual(git.sha(self.repo, "fxh"), self.candidate)
        self.assertEqual((self.repo / "changed.txt").read_text(encoding="utf-8"), "candidate\n")
        self.assertEqual((self.repo / "unrelated.txt").read_text(encoding="utf-8"), "staged local draft\n")
        self.assertEqual(git.dirty_paths(self.repo), ["unrelated.txt"])

    def test_cas_refuses_to_overwrite_a_conflicting_local_edit(self):
        (self.repo / "changed.txt").write_text("user draft\n", encoding="utf-8")

        with self.assertRaises(integrate.Reject):
            integrate.ff_compare_and_swap(self.repo, "fxh", self.old, self.candidate)

        self.assertEqual(git.sha(self.repo, "fxh"), self.old)
        self.assertEqual((self.repo / "changed.txt").read_text(encoding="utf-8"), "user draft\n")

    def test_cas_rejects_a_branch_moved_since_the_plan_was_built(self):
        git.run(["update-ref", "refs/heads/fxh", self.candidate], cwd=self.repo)

        with self.assertRaises(integrate.Reject):
            integrate.ff_compare_and_swap(self.repo, "fxh", self.old, self.candidate)

        self.assertEqual(git.sha(self.repo, "fxh"), self.candidate)
        self.assertEqual((self.repo / "changed.txt").read_text(encoding="utf-8"), "base\n")

    def test_interrupted_checkout_is_resumed_from_durable_journal(self):
        journal = self.repo / "land-cas.json"
        journal.write_text(json.dumps({
            "version": 1, "branch": "fxh", "expected_old": self.old, "candidate": self.candidate,
        }), encoding="utf-8")
        # Model a process interruption after read-tree changed the index/worktree
        # but before the prepared ref transaction committed.
        git.run(["read-tree", "-m", "-u", self.old, self.candidate], cwd=self.repo)

        self.assertTrue(integrate.recover_land_cas(self.repo, "fxh", journal))

        self.assertEqual(git.sha(self.repo, "fxh"), self.candidate)
        self.assertFalse(journal.exists())
        self.assertEqual((self.repo / "changed.txt").read_text(encoding="utf-8"), "candidate\n")

    def test_interrupted_checkout_recovery_preserves_new_conflicting_edit(self):
        journal = self.repo / "land-cas.json"
        journal.write_text(json.dumps({
            "version": 1, "branch": "fxh", "expected_old": self.old, "candidate": self.candidate,
        }), encoding="utf-8")
        (self.repo / "changed.txt").write_text("new user edit\n", encoding="utf-8")

        with self.assertRaises(integrate.Reject):
            integrate.recover_land_cas(self.repo, "fxh", journal)

        self.assertEqual(git.sha(self.repo, "fxh"), self.old)
        self.assertEqual((self.repo / "changed.txt").read_text(encoding="utf-8"), "new user edit\n")
        self.assertTrue(journal.exists())


class SeparatedDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-delivery-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "be"
        self.anchor = self.root / "anchors" / "be" / "dev"
        self.repo.mkdir(parents=True)
        self.anchor.mkdir(parents=True)
        self.cfg = SimpleNamespace(
            os="mac", integration="fxh", profile_name="xinhua", raw={}, protected=[], logs_dir=self.root / "logs",
            locks_dir=self.root / "locks", state_dir=self.root / "state", sensitive_paths=[], repo_order=["be"],
            repos={"be": SimpleNamespace(
                alias="be", path=self.repo, push_branch="fxh-dev", trunk="dev", promote="ff-trunk",
                anchors=[], audited=[], workspace_mode="task-worktree",
                automatic={"commit_task_branch": True, "land_to_local": "fxh", "push_only": "fxh-dev",
                           "expected_origin_url": "https://git.example.invalid/group/be.git"})},
        )
        self.cfg.locks_dir.mkdir(parents=True)
        self.cfg.repo = lambda alias: self.cfg.repos[alias]
        self.cfg.integration_repo = lambda alias: self.cfg.repos[alias].path
        self.cfg.anchor_path = lambda alias, name: self.anchor

    def tearDown(self):
        self.temp.cleanup()

    def lockless(self, *_args, **_kwargs):
        return nullcontext()

    @staticmethod
    def git_result(returncode=0, stderr=""):
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    def test_personal_publish_only_pushes_fxh_dev(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()

        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate, "_validate_xinhua_origin") as validate_origin, \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", side_effect=lambda _repo, ref: {
                 "fxh": "a" * 40, "origin/fxh-dev": "b" * 40}.get(ref)), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "diff_names", return_value=["src/change.py"]), \
             patch.object(integrate, "audit_branch", return_value=[]), \
             patch.object(integrate, "gate_prepare", return_value=Path("gate")), \
             patch.object(integrate.gates, "run_gate", return_value=(True, "passed")), \
             patch.object(integrate.registry, "confirm_human") as confirm:
            self.assertEqual(integrate.publish_personal_branch(self.cfg, Mock(), "be"), 0)

        self.assertEqual(calls[-1], ["push", "origin", f"{'a' * 40}:refs/heads/fxh-dev"])
        self.assertEqual(validate_origin.call_args_list, [
            call(self.cfg, "be", self.repo), call(self.cfg, "be", self.repo),
        ])
        self.assertFalse(any("merge" in command for command in calls))
        self.assertFalse(any(command[0] == "push" and "refs/heads/dev" in " ".join(command)
                             for command in calls))
        confirm.assert_not_called()

    def test_personal_publish_rejects_origin_changed_after_gate_before_push(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()
        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate, "_validate_xinhua_origin",
                          side_effect=[None, integrate.Reject("origin changed after gate")]) as validate_origin, \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", side_effect=lambda _repo, ref: {
                 "fxh": "a" * 40, "origin/fxh-dev": "b" * 40}.get(ref)), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "diff_names", return_value=["src/change.py"]), \
             patch.object(integrate, "audit_branch", return_value=[]), \
             patch.object(integrate, "gate_prepare", return_value=Path("gate")), \
             patch.object(integrate.gates, "run_gate", return_value=(True, "passed")):
            with self.assertRaisesRegex(integrate.Reject, "origin changed after gate"):
                integrate.publish_personal_branch(self.cfg, Mock(), "be")

        self.assertEqual(validate_origin.call_count, 2)
        self.assertEqual(calls, [["fetch", "origin", "--prune"]])

    def test_personal_publish_rejects_non_xinhua_direct_or_misconfigured_repos(self):
        self.cfg.profile_name = "demo"
        with patch.object(integrate.git, "run") as run:
            with self.assertRaises(integrate.Reject):
                integrate.publish_personal_branch(self.cfg, Mock(), "be")
        run.assert_not_called()

        self.cfg.profile_name = "xinhua"
        self.cfg.repos["be"].automatic["push_only"] = "dev"
        with patch.object(integrate.git, "run") as run:
            with self.assertRaises(integrate.Reject):
                integrate.publish_personal_branch(self.cfg, Mock(), "be")
        run.assert_not_called()

    def test_legacy_promote_fxh_dev_path_stops_before_dev_logic(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()
        self.cfg.anchor_path = Mock(side_effect=AssertionError("legacy promote must not enter dev path"))
        with patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate, "_validate_xinhua_origin") as validate_origin, \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", side_effect=lambda _repo, ref: {
                 "fxh": "a" * 40, "origin/fxh-dev": "b" * 40}.get(ref)), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate, "audit_branch", return_value=[]):
            integrate.promote_one(self.cfg, Mock(), "be", self.cfg.repo("be"), dry=True)

        self.assertEqual(calls, [["fetch", "origin", "--prune"]])
        validate_origin.assert_called_once_with(self.cfg, "be", self.repo)
        self.cfg.anchor_path.assert_not_called()

    def test_legacy_personal_promote_rejects_origin_changed_before_push(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()
        with patch.object(integrate, "_validate_xinhua_origin",
                          side_effect=[None, integrate.Reject("origin changed after gate")]) as validate_origin, \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", side_effect=lambda _repo, ref: {
                 "fxh": "a" * 40, "origin/fxh-dev": "b" * 40}.get(ref)), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "diff_names", return_value=["src/change.py"]), \
             patch.object(integrate, "audit_branch", return_value=[]), \
             patch.object(integrate, "gate_prepare", return_value=Path("gate")), \
             patch.object(integrate.gates, "run_gate", return_value=(True, "passed")):
            with self.assertRaisesRegex(integrate.Reject, "origin changed after gate"):
                integrate.promote_one(self.cfg, Mock(), "be", self.cfg.repo("be"), dry=False)

        self.assertEqual(validate_origin.call_count, 2)
        self.assertEqual(calls, [["fetch", "origin", "--prune"]])

    def test_local_dev_merge_confirms_and_never_pushes(self):
        merged = {"done": False}
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["merge", "--ff-only"]:
                merged["done"] = True
            return self.git_result()
        def sha(path, ref):
            if Path(path) == self.anchor and ref == "refs/heads/dev":
                return "b" * 40 if merged["done"] else "a" * 40
            if ref == "refs/heads/fxh":
                return "b" * 40
            return None

        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate, "_validate_xinhua_origin") as validate_origin, \
             patch.object(integrate.git, "current_branch", side_effect=lambda path: "dev" if Path(path) == self.anchor else "fxh"), \
             patch.object(integrate.git, "dirty", return_value=[]), \
             patch.object(integrate.git, "sha", side_effect=sha), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate, "required_promotion_task_id", return_value="T001"), \
             patch.object(integrate, "action_context", return_value="context"), \
             patch.object(integrate, "record_refs"), \
             patch.object(integrate.registry, "confirm_human", return_value=True) as confirm:
            self.assertEqual(integrate.merge_integration_to_local_dev(self.cfg, Mock(), "be"), 0)

        confirm.assert_called_once()
        self.assertEqual(validate_origin.call_args_list, [
            call(self.cfg, "be", self.repo),
            call(self.cfg, "be", self.anchor),
        ])
        self.assertEqual(calls, [["merge", "--ff-only", "b" * 40]])
        self.assertFalse(any(command[0] == "push" for command in calls))

    def test_dev_push_confirms_and_never_merges(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()
        def sha(path, ref):
            if Path(path) == self.repo and ref == "refs/remotes/origin/dev":
                return "a" * 40
            if Path(path) == self.anchor and ref == "refs/heads/dev":
                return "b" * 40
            return None

        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate, "_validate_xinhua_origin") as validate_origin, \
             patch.object(integrate.git, "current_branch", return_value="dev"), \
             patch.object(integrate.git, "dirty", return_value=[]), \
             patch.object(integrate.git, "sha", side_effect=sha), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate, "required_promotion_task_id", return_value="T001"), \
             patch.object(integrate, "action_context", return_value="context"), \
             patch.object(integrate, "record_refs"), \
             patch.object(integrate.registry, "confirm_human", return_value=True) as confirm:
            self.assertEqual(integrate.push_local_dev(self.cfg, Mock(), "be"), 0)

        confirm.assert_called_once()
        self.assertEqual(validate_origin.call_args_list, [
            call(self.cfg, "be", self.repo),
            call(self.cfg, "be", self.anchor),
            call(self.cfg, "be", self.repo),
            call(self.cfg, "be", self.anchor),
        ])
        self.assertEqual(calls[0], ["fetch", "origin", "refs/heads/dev:refs/remotes/origin/dev"])
        self.assertEqual(calls[-1], ["push", "origin", "refs/heads/dev:refs/heads/dev"])
        self.assertFalse(any(command[0] == "merge" for command in calls))

    def test_dev_push_rejects_origin_changed_during_confirmation(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.git_result()
        def sha(path, ref):
            if Path(path) == self.repo and ref == "refs/remotes/origin/dev":
                return "a" * 40
            if Path(path) == self.anchor and ref == "refs/heads/dev":
                return "b" * 40
            return None

        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate, "_validate_xinhua_origin",
                          side_effect=[None, None, None, integrate.Reject("origin changed during confirmation")]) as validate_origin, \
             patch.object(integrate.git, "current_branch", return_value="dev"), \
             patch.object(integrate.git, "dirty", return_value=[]), \
             patch.object(integrate.git, "sha", side_effect=sha), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate.git, "out", return_value="abc123 commit"), \
             patch.object(integrate.git, "run", side_effect=run), \
             patch.object(integrate, "required_promotion_task_id", return_value="T001"), \
             patch.object(integrate, "action_context", return_value="context"), \
             patch.object(integrate, "record_refs"), \
             patch.object(integrate.registry, "confirm_human", return_value=True) as confirm:
            with self.assertRaisesRegex(integrate.Reject, "origin changed during confirmation"):
                integrate.push_local_dev(self.cfg, Mock(), "be")

        confirm.assert_called_once()
        self.assertEqual(validate_origin.call_count, 4)
        self.assertEqual(calls, [["fetch", "origin", "refs/heads/dev:refs/remotes/origin/dev"]])

    def test_wrong_profile_origin_stops_each_separated_route_before_network_or_ref_write(self):
        routes = [
            lambda: integrate.publish_personal_branch(self.cfg, Mock(), "be"),
            lambda: integrate.promote_one(self.cfg, Mock(), "be", self.cfg.repo("be"), dry=True),
            lambda: integrate.merge_integration_to_local_dev(self.cfg, Mock(), "be"),
            lambda: integrate.push_local_dev(self.cfg, Mock(), "be"),
        ]
        for route in routes:
            with self.subTest(route=route), \
                 patch.object(integrate, "file_lock", side_effect=self.lockless), \
                 patch.object(integrate, "_validate_xinhua_origin",
                              side_effect=integrate.Reject("origin identity mismatch")) as validate_origin, \
                 patch.object(integrate.git, "run") as run, \
                 patch.object(integrate.registry, "confirm_human") as confirm:
                with self.assertRaisesRegex(integrate.Reject, "origin identity mismatch"):
                    route()
                validate_origin.assert_called()
                run.assert_not_called()
                confirm.assert_not_called()

    def test_origin_identity_requires_exact_single_fetch_and_push_url(self):
        expected = self.cfg.repo("be").automatic["expected_origin_url"]
        responses = {
            ("config", "--local", "--get-all", "remote.origin.url"): (0, expected + "\n"),
            ("config", "--local", "--get-all", "remote.origin.pushurl"): (1, ""),
            ("remote", "get-url", "--all", "origin"): (0, expected + "\n"),
            ("remote", "get-url", "--push", "--all", "origin"): (0, expected + "\n"),
            ("config", "--local", "--get-all", "remote.origin.mirror"): (1, ""),
        }
        def run(command, **_kwargs):
            code, stdout = responses[tuple(command)]
            return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

        with patch.object(integrate.git, "run", side_effect=run):
            integrate._validate_xinhua_origin(self.cfg, "be", self.repo)

        responses[("remote", "get-url", "--push", "--all", "origin")] = (0, expected + ".attacker\n")
        with patch.object(integrate.git, "run", side_effect=run):
            with self.assertRaisesRegex(integrate.Reject, "fetch/push 地址"):
                integrate._validate_xinhua_origin(self.cfg, "be", self.repo)

    def test_automatic_land_is_silent_and_sensitive_paths_fail_closed(self):
        task = {
            "id": "T001", "state": "ready", "os": "mac", "dir": str(self.root / "tasks" / "T001"),
            "title": "test delivery", "repos": {"be": {"branch": "cx/T001-be", "ready_sha": "t" * 40}},
        }
        reg = Mock()
        reg.load.return_value = task
        def set_state(current, state, note=""):
            current["state"] = state
        plan = {"status": "candidate", "head": "a" * 40, "tip": "t" * 40,
                "cand": "c" * 40, "changed": ["src/change.py"], "sensitive": []}
        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate.tasks, "require_local"), \
             patch.object(integrate.tasks, "refresh_board"), \
             patch.object(integrate, "plan_landing", return_value=plan), \
             patch.object(integrate, "gate_prepare", return_value=Path("gate")), \
             patch.object(integrate.gates, "run_gate", return_value=(True, "passed")), \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", return_value="a" * 40), \
             patch.object(integrate.git, "dirty_paths", return_value=[]), \
             patch.object(integrate, "ff_compare_and_swap") as cas, \
             patch.object(integrate, "record_refs"), \
             patch.object(integrate.registry, "confirm_human") as confirm:
            reg.set_state.side_effect = set_state
            self.assertEqual(integrate.land_automatic(self.cfg, reg, task, "be"), 0)
        cas.assert_called_once_with(self.repo, "fxh", "a" * 40, "c" * 40,
                                    journal_path=self.cfg.state_dir / "land-cas" / "be.json")
        confirm.assert_not_called()
        self.assertEqual(task["state"], "landed")

        task["state"] = "ready"
        reg.load.return_value = task
        sensitive = {**plan, "sensitive": ["deploy/secret.yml"]}
        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate.tasks, "require_local"), \
             patch.object(integrate.tasks, "refresh_board"), \
             patch.object(integrate, "plan_landing", return_value=sensitive), \
             patch.object(integrate, "ff_compare_and_swap") as cas, \
             patch.object(integrate.registry, "confirm_human") as confirm:
            with self.assertRaises(integrate.Reject):
                integrate.land_automatic(self.cfg, reg, task, "be")
        cas.assert_not_called()
        confirm.assert_not_called()

    def test_automatic_land_replay_after_cas_records_ready_tip_without_merge_commit(self):
        tip = "t" * 40
        task = {
            "id": "T002", "state": "ready", "os": "mac", "dir": str(self.root / "tasks" / "T002"),
            "title": "crash recovery", "repos": {"be": {"branch": "cx/T002-be", "ready_sha": tip}},
        }
        reg = Mock()
        reg.load.return_value = task

        def set_state(current, state, note=""):
            current["state"] = state

        candidate_plan = {"status": "candidate", "head": "a" * 40, "tip": tip,
                          "cand": tip, "changed": ["src/change.py"], "sensitive": []}
        already_plan = {"status": "already", "head": tip, "tip": tip,
                        "changed": [], "sensitive": []}
        reg.set_state.side_effect = set_state

        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate.tasks, "require_local"), \
             patch.object(integrate.tasks, "refresh_board"), \
             patch.object(integrate, "recover_land_cas"), \
             patch.object(integrate, "plan_landing", return_value=candidate_plan), \
             patch.object(integrate, "gate_prepare", return_value=Path("gate")), \
             patch.object(integrate.gates, "run_gate", return_value=(True, "passed")), \
             patch.object(integrate.git, "current_branch", return_value="fxh"), \
             patch.object(integrate.git, "sha", return_value="a" * 40), \
             patch.object(integrate.git, "dirty_paths", return_value=[]), \
             patch.object(integrate, "ff_compare_and_swap", side_effect=SystemExit("simulated crash after CAS")), \
             patch.object(integrate, "record_refs"):
            with self.assertRaisesRegex(SystemExit, "simulated crash"):
                integrate.land_automatic(self.cfg, reg, task, "be")

        self.assertEqual(task["state"], "queued")
        task["state"] = "ready"
        with patch.object(integrate, "file_lock", side_effect=self.lockless), \
             patch.object(integrate.tasks, "require_local"), \
             patch.object(integrate.tasks, "refresh_board"), \
             patch.object(integrate, "recover_land_cas", return_value=True), \
             patch.object(integrate, "plan_landing", return_value=already_plan), \
             patch.object(integrate.git, "is_ancestor", return_value=True), \
             patch.object(integrate, "record_refs"):
            self.assertEqual(integrate.land_automatic(self.cfg, reg, task, "be"), 0)

        self.assertEqual(task["repos"]["be"]["landed_sha"], tip)
        self.assertEqual(task["state"], "landed")


if __name__ == "__main__":
    unittest.main()
