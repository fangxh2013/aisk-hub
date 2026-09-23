#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guarded publish driver tests using a fake Git adapter; no network is used."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import publish_driver, publish_pending  # noqa: E402


UTC = dt.timezone.utc
START = dt.datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
LANDED_SHA = "a" * 40
TIP_SHA = "b" * 40
TASK_BRANCH = "ai/T031-safe-publish"
REMOTE_URL = "ssh://git.example.invalid/hbxhzt/xinhua-platform.git"


class FakeGit:
    def __init__(self, integration_path, task_path):
        self.integration_path = Path(integration_path)
        self.task_path = Path(task_path)
        self.calls = []
        self.config = {
            "branch.fxh.remote": ["origin"],
            "branch.fxh.merge": ["refs/heads/fxh-dev"],
            "branch.fxh.pushRemote": [],
            "remote.pushDefault": [],
            "remote.origin.url": [REMOTE_URL],
            "remote.origin.pushurl": [],
            "remote.origin.mirror": [],
        }
        self.upstream = "origin/fxh-dev"
        self.fetch_urls = [REMOTE_URL]
        self.push_urls = [REMOTE_URL]
        self.local_tip = TIP_SHA
        self.contains_landed = True
        self.integration_branch = "fxh"
        self.push_returncode = 0
        self.push_stderr = ""
        self.push_stdout = ""
        self.remote_sha = TIP_SHA
        self.ls_remote_returncode = 0
        self.ls_remote_stdout = None

    def current_branch(self, path):
        path = Path(path)
        if path.resolve() == self.integration_path.resolve():
            return self.integration_branch
        if path.resolve() == self.task_path.resolve():
            return TASK_BRANCH
        return None

    def sha(self, path, ref):
        if ref == "refs/heads/fxh":
            return self.local_tip
        if ref == LANDED_SHA:
            return LANDED_SHA
        return None

    def is_ancestor(self, path, ancestor, descendant):
        return self.contains_landed and ancestor == LANDED_SHA and descendant == self.local_tip

    def run(self, args, cwd=None, check=True, **kwargs):
        argv = [str(value) for value in args]
        self.calls.append((argv, Path(cwd) if cwd else None))
        if len(argv) >= 4 and argv[:3] == ["config", "--local", "--get-all"]:
            values = self.config.get(argv[3], [])
            return SimpleNamespace(
                returncode=0 if values else 1,
                stdout="".join(f"{value}\n" for value in values),
                stderr="",
            )
        if argv == ["remote", "get-url", "--all", "origin"]:
            return SimpleNamespace(returncode=0, stdout="".join(f"{url}\n" for url in self.fetch_urls), stderr="")
        if argv == ["remote", "get-url", "--push", "--all", "origin"]:
            return SimpleNamespace(returncode=0, stdout="".join(f"{url}\n" for url in self.push_urls), stderr="")
        if argv == ["ls-remote", "--heads", "origin", "refs/heads/fxh-dev"]:
            output = self.ls_remote_stdout
            if output is None:
                output = f"{self.remote_sha}\trefs/heads/fxh-dev\n" if self.remote_sha else ""
            return SimpleNamespace(
                returncode=self.ls_remote_returncode, stdout=output, stderr=self.push_stderr,
            )
        if argv == ["rev-parse", "--abbrev-ref", "fxh@{upstream}"]:
            return SimpleNamespace(
                returncode=0 if self.upstream else 1,
                stdout=f"{self.upstream}\n" if self.upstream else "",
                stderr="",
            )
        if argv and argv[0] == "push":
            return SimpleNamespace(
                returncode=self.push_returncode,
                stdout=self.push_stdout,
                stderr=self.push_stderr,
            )
        raise AssertionError(f"unexpected Git command: {argv}")

    @property
    def push_calls(self):
        return [argv for argv, _ in self.calls if argv and argv[0] == "push"]


class FakeRepoCfg:
    def __init__(self, path, *, automatic=None, workspace_mode="task-worktree"):
        self.path = Path(path)
        self.workspace_mode = workspace_mode
        self.automatic = automatic or {
            "land_to_local": "fxh",
            "push_only": "fxh-dev",
            "expected_origin_url": REMOTE_URL,
        }


class FakeRegistry:
    def __init__(self, task):
        self.task = task

    def load(self, task_id, must=True):
        return self.task if self.task.get("id") == task_id else None


class FakeCfg:
    def __init__(self, root, *, integration="fxh", automatic=None, workspace_mode="task-worktree"):
        self.profile_name = "xinhua"
        self.integration = integration
        self.state_dir = Path(root) / "engine-state"
        self.repo_cfg = FakeRepoCfg(
            Path(root) / "backend",
            automatic=automatic,
            workspace_mode=workspace_mode,
        )

    def repo(self, alias):
        if alias != "be":
            raise KeyError(alias)
        return self.repo_cfg

    def integration_repo(self, alias):
        return self.repo(alias).path


class PublishDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="publish-driver-")
        self.root = Path(self.temp.name)
        self.integration_path = self.root / "backend"
        self.task_path = self.root / "tasks" / "T031" / "backend"
        self.integration_path.mkdir(parents=True)
        self.task_path.mkdir(parents=True)
        self.cfg = FakeCfg(self.root)
        self.task = {
            "id": "T031",
            "state": "landed",
            "repos": {
                "be": {
                    "branch": TASK_BRANCH,
                    "path": str(self.task_path),
                    "landed_sha": LANDED_SHA,
                }
            },
        }
        self.reg = FakeRegistry(self.task)
        self.git = FakeGit(self.integration_path, self.task_path)
        self.store = publish_pending.PublishPendingStore(self.root / "publish-state")

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, **kwargs):
        return publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git, now=START, **kwargs,
        )

    def test_success_pushes_only_the_exact_fxh_dev_refspec(self):
        report = self.publish()

        self.assertTrue(report.attempted)
        self.assertTrue(report.published)
        self.assertEqual(report.status, publish_pending.PUBLISHED)
        self.assertEqual(report.operation_key, f"T031/be/{LANDED_SHA}")
        self.assertEqual(report.remote_sha, TIP_SHA)
        self.assertEqual(
            self.git.push_calls,
            [["push", "--no-follow-tags", "origin", "fxh:refs/heads/fxh-dev"]],
        )
        self.assertFalse(any(argv[0] == "fetch" for argv, _ in self.git.calls))
        self.assertEqual(
            [argv for argv, _ in self.git.calls if argv[0] == "ls-remote"],
            [["ls-remote", "--heads", "origin", "refs/heads/fxh-dev"]],
        )
        self.assertEqual(
            self.store.get(report.operation_key)["status"],
            publish_pending.PUBLISHED,
        )
        self.assertEqual(self.store.get(report.operation_key)["remote_sha"], TIP_SHA)

    def test_profile_policy_must_match_exact_task_worktree_contract(self):
        cases = [
            ({"land_to_local": "dev", "push_only": "fxh-dev"}, "fxh", "task-worktree"),
            ({"land_to_local": "fxh", "push_only": "dev"}, "fxh", "task-worktree"),
            ({"land_to_local": "fxh", "push_only": "fxh-dev"}, "dev", "task-worktree"),
            ({"land_to_local": "fxh", "push_only": "fxh-dev"}, "fxh", "direct"),
        ]
        for index, (automatic, integration, workspace_mode) in enumerate(cases):
            with self.subTest(automatic=automatic, integration=integration, mode=workspace_mode):
                self.store = publish_pending.PublishPendingStore(self.root / f"policy-{index}")
                self.cfg.integration = integration
                self.cfg.repo_cfg.automatic = automatic
                self.cfg.repo_cfg.workspace_mode = workspace_mode
                report = self.publish()
                self.assertFalse(report.attempted)
                self.assertFalse(report.published)
                self.assertEqual(report.failure_class, publish_pending.PROTECTION)
                self.assertEqual(report.status, publish_pending.NEEDS_ATTENTION)
                self.assertEqual(self.git.push_calls, [])

    def test_task_sha_must_be_registered_and_present_on_current_local_fxh(self):
        mismatch = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", TIP_SHA,
            store=self.store, git_ops=self.git, now=START,
        )
        self.assertFalse(mismatch.attempted)
        self.assertEqual(self.git.push_calls, [])

        self.git.contains_landed = False
        missing = self.publish()
        self.assertFalse(missing.attempted)
        self.assertEqual(missing.failure_class, publish_pending.NON_FAST_FORWARD)
        self.assertEqual(self.git.push_calls, [])

    def test_upstream_and_branch_configuration_must_be_exact(self):
        self.git.config["branch.fxh.merge"] = ["refs/heads/dev"]
        self.git.upstream = "origin/dev"

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertEqual(report.failure_class, publish_pending.PROTECTION)
        self.assertEqual(self.git.push_calls, [])

    def test_integration_checkout_must_be_on_fxh(self):
        self.git.integration_branch = "dev"

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertEqual(report.failure_class, publish_pending.PROTECTION)
        self.assertEqual(self.git.push_calls, [])

    def test_registry_landed_sha_is_reloaded_and_must_match_argument(self):
        self.reg.task["repos"]["be"]["landed_sha"] = TIP_SHA

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertEqual(report.failure_class, publish_pending.UNKNOWN)
        self.assertEqual(self.git.push_calls, [])

    def test_ambiguous_or_mismatched_remote_urls_fail_closed(self):
        cases = [
            ("multiple fetch urls", [REMOTE_URL, REMOTE_URL + ".mirror"], [REMOTE_URL]),
            ("read and write differ", [REMOTE_URL], [REMOTE_URL + ".other"]),
        ]
        for index, (label, fetch_urls, push_urls) in enumerate(cases):
            with self.subTest(label=label):
                self.store = publish_pending.PublishPendingStore(self.root / f"remote-{index}")
                self.git.fetch_urls = fetch_urls
                self.git.push_urls = push_urls
                report = self.publish()
                self.assertFalse(report.attempted)
                self.assertEqual(report.failure_class, publish_pending.UNKNOWN)
                self.assertEqual(self.git.push_calls, [])

    def test_origin_url_must_match_explicit_alias_profile_identity(self):
        wrong_url = "http://attacker.example.invalid/hbxhzt/xinhua-platform.git"
        self.git.config["remote.origin.url"] = [wrong_url]
        self.git.fetch_urls = [wrong_url]
        self.git.push_urls = [wrong_url]

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertFalse(report.published)
        self.assertEqual(report.failure_class, publish_pending.PROTECTION)
        self.assertIn("与 xinhua.be 档案 automatic.expected_origin_url 不匹配", report.message)
        self.assertEqual(self.git.push_calls, [])

    def test_origin_change_after_initial_validation_is_persisted_and_never_pushed(self):
        wrong_url = "http://attacker.example.invalid/hbxhzt/xinhua-platform.git"
        original_is_ancestor = self.git.is_ancestor

        def change_origin_after_initial_validation(path, ancestor, descendant):
            result = original_is_ancestor(path, ancestor, descendant)
            self.git.config["remote.origin.url"] = [wrong_url]
            self.git.fetch_urls = [wrong_url]
            self.git.push_urls = [wrong_url]
            return result

        self.git.is_ancestor = change_origin_after_initial_validation

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertFalse(report.published)
        self.assertEqual(report.failure_class, publish_pending.PROTECTION)
        self.assertIn("推送前 origin URL", report.message)
        self.assertEqual(self.git.push_calls, [])
        state = self.store.get(report.operation_key)
        self.assertEqual(state["failure_class"], publish_pending.PROTECTION)
        self.assertEqual(state["status"], publish_pending.NEEDS_ATTENTION)

    def test_missing_profile_expected_origin_url_fails_closed(self):
        self.cfg.repo_cfg.automatic.pop("expected_origin_url")

        report = self.publish()

        self.assertFalse(report.attempted)
        self.assertFalse(report.published)
        self.assertEqual(report.failure_class, publish_pending.PROTECTION)
        self.assertIn("必须显式配置 automatic.expected_origin_url", report.message)
        self.assertEqual(self.git.push_calls, [])

    def test_non_fast_forward_push_failure_is_persisted_without_force_retry(self):
        self.git.push_returncode = 1
        self.git.push_stderr = (
            "! [rejected] fxh-dev -> fxh-dev (non-fast-forward)\n"
            "error: failed to push some refs"
        )

        report = self.publish()

        self.assertTrue(report.attempted)
        self.assertFalse(report.published)
        self.assertEqual(report.failure_class, publish_pending.NON_FAST_FORWARD)
        self.assertEqual(report.status, publish_pending.NEEDS_ATTENTION)
        self.assertEqual(report.events, ("needs_attention",))
        self.assertEqual(len(self.git.push_calls), 1)
        self.assertFalse(any("force" in part or part.startswith("+") for part in self.git.push_calls[0]))
        state = self.store.get(report.operation_key)
        self.assertEqual(state["failure_class"], publish_pending.NON_FAST_FORWARD)
        later = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(days=1),
        )
        self.assertFalse(later.attempted)
        self.assertEqual(len(self.git.push_calls), 1)

    def test_transient_failure_retries_hourly_after_blocked_escalation(self):
        self.git.push_returncode = 1
        self.git.push_stderr = "fatal: unable to access remote: connection timed out"
        first = self.publish()
        self.assertEqual(first.status, publish_pending.PUSH_PENDING)

        blocked = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(minutes=60),
        )
        self.assertFalse(blocked.attempted)
        self.assertEqual(blocked.status, publish_pending.BLOCKED)
        self.assertEqual(blocked.events, ("blocked",))
        self.assertEqual(len(self.git.push_calls), 1)

        self.git.push_returncode = 0
        self.git.push_stderr = ""
        retried = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(minutes=120),
        )
        self.assertTrue(retried.attempted)
        self.assertTrue(retried.published)
        self.assertEqual(retried.events, ("resolved",))
        self.assertEqual(len(self.git.push_calls), 2)

    def test_driver_uses_profile_publish_pending_policy_overrides(self):
        self.cfg.repo_cfg.automatic["publish_pending_policy"] = {
            "retry_delays_minutes": [2, 7],
            "needs_attention_after_minutes": 8,
            "needs_attention_after_attempts": 2,
            "blocked_after_minutes": 30,
        }
        self.git.push_returncode = 1
        self.git.push_stderr = "fatal: unable to access remote: connection timed out"

        first = self.publish()
        self.assertEqual(first.state["next_retry_at"], "2026-09-23T10:02:00Z")

        self.git.push_stderr = "fatal: connection reset by peer"
        second = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(minutes=2),
        )
        self.assertEqual(second.state["status"], publish_pending.NEEDS_ATTENTION)
        self.assertEqual(second.state["next_retry_at"], "2026-09-23T10:09:00Z")
        self.assertEqual(second.events, ("needs_attention",))

        blocked = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(minutes=30),
        )
        self.assertFalse(blocked.attempted)
        self.assertEqual(blocked.status, publish_pending.BLOCKED)
        self.assertEqual(blocked.state["blocked_at"], "2026-09-23T10:30:00Z")
        self.assertEqual(blocked.state["next_retry_at"], "2026-09-23T11:30:00Z")
        self.assertEqual(blocked.events, ("blocked",))

    def test_transient_failure_waits_until_persisted_retry_time(self):
        self.git.push_returncode = 1
        self.git.push_stderr = "fatal: unable to access remote: connection timed out"
        first = self.publish()
        self.assertEqual(first.status, publish_pending.PUSH_PENDING)
        self.assertEqual(first.failure_class, publish_pending.TRANSIENT)

        not_due = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(seconds=30),
        )
        self.assertFalse(not_due.attempted)
        self.assertEqual(len(self.git.push_calls), 1)

        self.git.push_returncode = 0
        due = publish_driver.publish_task_branch(
            self.cfg, self.reg, self.task, "be", LANDED_SHA,
            store=self.store, git_ops=self.git,
            now=START + dt.timedelta(minutes=1),
        )
        self.assertTrue(due.published)
        self.assertEqual(len(self.git.push_calls), 2)
        self.assertTrue(all(call == [
            "push", "--no-follow-tags", "origin", "fxh:refs/heads/fxh-dev",
        ] for call in self.git.push_calls))

    def test_success_is_idempotent_for_same_task_repo_sha(self):
        first = self.publish()
        second = self.publish()

        self.assertTrue(first.published)
        self.assertTrue(second.published)
        self.assertFalse(second.attempted)
        self.assertEqual(second.remote_sha, TIP_SHA)
        self.assertEqual(len(self.git.push_calls), 1)
        self.assertIsNone(self.reg.task["repos"]["be"].get("remote_sha"))

    def test_concurrent_same_operation_serializes_to_one_publish(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(executor.map(lambda _index: self.publish(), range(2)))

        self.assertTrue(all(report.published for report in reports))
        self.assertEqual(sum(report.attempted for report in reports), 1)
        self.assertEqual(len(self.git.push_calls), 1)

    def test_remote_tip_mismatch_after_push_is_not_reported_as_published(self):
        self.git.remote_sha = "c" * 40

        report = self.publish()

        self.assertTrue(report.attempted)
        self.assertFalse(report.published)
        self.assertIsNone(report.remote_sha)
        self.assertEqual(report.failure_class, publish_pending.UNKNOWN)
        self.assertEqual(report.status, publish_pending.NEEDS_ATTENTION)
        self.assertEqual(report.events, ("needs_attention",))
        self.assertEqual(
            [call[0] for call in self.git.calls if call[0][0] == "ls-remote"],
            [["ls-remote", "--heads", "origin", "refs/heads/fxh-dev"]],
        )

    def test_remote_ref_confirmation_must_return_exactly_one_expected_branch(self):
        self.git.ls_remote_stdout = (
            f"{TIP_SHA}\trefs/heads/fxh-dev\n{LANDED_SHA}\trefs/heads/dev\n"
        )

        report = self.publish()

        self.assertFalse(report.published)
        self.assertIsNone(report.remote_sha)
        self.assertEqual(report.failure_class, publish_pending.UNKNOWN)
        self.assertEqual(self.git.push_calls, [
            ["push", "--no-follow-tags", "origin", "fxh:refs/heads/fxh-dev"],
        ])


if __name__ == "__main__":
    unittest.main()
