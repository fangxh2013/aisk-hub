"""Direct checkout task-flow tests; all repositories are disposable local fixtures."""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import direct_tasks, doctor, guards, integrate, tasks  # noqa: E402
from engine.worktree.config import WtError, build_config  # noqa: E402
from engine.worktree.registry import Registry  # noqa: E402


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(os.fsdecode(result.stderr))
    return os.fsdecode(result.stdout).strip()


def init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "fxh")
    git(repo, "config", "user.name", "Direct Test")
    git(repo, "config", "user.email", "direct-test@example.test")
    (repo / "docs-plan.md").write_text("before\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "docs: baseline")
    git(repo, "branch", "dev")
    git(repo, "branch", "main")


def profile(root, *, scope=None):
    repos = {name: str(root / "repos" / name) for name in ("be", "web", "docs")}
    declarations = {
        "be": {"profile_repo": "be", "trunk": "dev", "push_branch": "fxh-dev", "promote": "ff-trunk",
               "gate": {"kind": "none"}},
        "web": {"profile_repo": "web", "trunk": "dev", "push_branch": "fxh-dev", "promote": "ff-trunk",
                "gate": {"kind": "none"}},
        "docs": {"profile_repo": "docs", "trunk": "master", "push_branch": "fxh", "promote": "push-only",
                 "workspace_mode": "direct", "automatic": {"commit_branch": "fxh"},
                 "gate": {"kind": "none"}},
    }
    return {
        "project": "xinhua", "_path": root / "xinhua.yaml", "repos": repos,
        "worktrees": {
            "data_root": str(root / "runtime"), "integration_branch": "fxh",
            "protected": ["main", "master", "dev", "fxh"],
            "repo_order": ["be", "web", "docs"],
            "quotas": {"active": 8, "builds": 2},
            "repos": declarations,
            "retention": {"promoted_hours": 72, "archive_days": 30, "disk_budget_gb": 1},
        },
    }


class DirectTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-direct-task-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Direct Test",
            "GIT_AUTHOR_EMAIL": "direct-test@example.test",
            "GIT_COMMITTER_NAME": "Direct Test",
            "GIT_COMMITTER_EMAIL": "direct-test@example.test",
        })
        env.start()
        self.addCleanup(env.stop)
        self.prof = profile(self.root)
        for repo in self.prof["repos"].values():
            init_repo(Path(repo))
        self.cfg = build_config(self.prof, "mac")
        self.reg = Registry(self.cfg)

    def new_task(self, scope=None):
        args = SimpleNamespace(
            repo="docs", slug="direct-doc-change", title="修改工作流文档",
            goal="更新文档", accept="提交在 fxh", scope=scope or ["docs-plan.md"],
            tool="human", session=None,
        )
        direct_tasks.cmd_new(self.cfg, self.reg, args)
        return self.reg.all()[0]

    def test_direct_finish_commits_exact_scope_and_creates_no_worktree(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("after\n", encoding="utf-8")

        rc = direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: 更新工作流文档", tool="human", session=None,
        ))

        self.assertEqual(rc, 0)
        self.assertEqual(git(repo, "branch", "--show-current"), "fxh")
        self.assertEqual(git(repo, "show", "--format=%s", "-s", "HEAD"), "docs: 更新工作流文档")
        self.assertEqual(git(repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.reg.load(task["id"])["state"], "archived")
        lease_dir = self.cfg.data_root / "locks" / "direct-checkout"
        self.assertFalse(list(lease_dir.glob("*.json")))

    def test_direct_finish_can_resume_a_queued_task_after_interruption(self):
        task = self.new_task()
        self.reg.set_state(task, "queued", note="interrupted before finish")
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("queued recovery\n", encoding="utf-8")

        result = direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: 恢复排队任务", tool="human", session=None,
        ))

        self.assertEqual(result, 0)
        self.assertEqual(self.reg.load(task["id"])["state"], "archived")
        self.assertEqual(git(repo, "show", "--format=%s", "-s", "HEAD"), "docs: 恢复排队任务")

    def test_direct_finish_recovers_crash_after_exact_intent_was_staged(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("exact pending snapshot\n", encoding="utf-8")
        real_run = direct_tasks.git.run

        def crash_before_commit(args, **kwargs):
            if args and args[0] == "--literal-pathspecs" and args[1] == "commit":
                raise RuntimeError("fault injected after git add")
            return real_run(args, **kwargs)

        with patch.object(direct_tasks.git, "run", side_effect=crash_before_commit):
            with self.assertRaisesRegex(RuntimeError, "fault injected"):
                direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                    task=task["id"], message="docs: recover staged intent", tool="human", session=None,
                ))

        pending = self.reg.load(task["id"])["repos"]["docs"]["pending_commit"]
        self.assertEqual(pending["version"], 2)
        self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "docs-plan.md")
        result = direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: recover staged intent", tool="human", session=None,
        ))

        self.assertEqual(result, 0)
        self.assertEqual(git(repo, "show", "--format=%s", "-s", "HEAD"), "docs: recover staged intent")
        self.assertEqual(self.reg.load(task["id"])["state"], "archived")

    def test_pending_recovery_refuses_new_in_scope_staged_path_without_committing_it(self):
        task = self.new_task(scope=["docs-plan.md", "outside.txt"])
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("original intended change\n", encoding="utf-8")
        real_run = direct_tasks.git.run

        def crash_before_commit(args, **kwargs):
            if args and args[0] == "--literal-pathspecs" and args[1] == "commit":
                raise RuntimeError("fault injected after git add")
            return real_run(args, **kwargs)

        with patch.object(direct_tasks.git, "run", side_effect=crash_before_commit):
            with self.assertRaisesRegex(RuntimeError, "fault injected"):
                direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                    task=task["id"], message="docs: pending intent", tool="human", session=None,
                ))

        (repo / "outside.txt").write_text("concurrent staged work\n", encoding="utf-8")
        git(repo, "add", "outside.txt")
        head_before_retry = git(repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(WtError, "改动路径已变化"):
            direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                task=task["id"], message="docs: pending intent", tool="human", session=None,
            ))

        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before_retry)
        self.assertEqual({p for p in git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if p},
                         {"docs-plan.md", "outside.txt"})
        self.assertEqual((repo / "outside.txt").read_text(encoding="utf-8"), "concurrent staged work\n")
        self.assertEqual(self.reg.load(task["id"])["state"], "active")

    def test_pending_recovery_refuses_changed_snapshot_and_preserves_index(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("snapshot before crash\n", encoding="utf-8")
        real_run = direct_tasks.git.run

        def crash_before_commit(args, **kwargs):
            if args and args[0] == "--literal-pathspecs" and args[1] == "commit":
                raise RuntimeError("fault injected after git add")
            return real_run(args, **kwargs)

        with patch.object(direct_tasks.git, "run", side_effect=crash_before_commit):
            with self.assertRaisesRegex(RuntimeError, "fault injected"):
                direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                    task=task["id"], message="docs: snapshot", tool="human", session=None,
                ))

        staged_before = git(repo, "diff", "--cached", "--binary", "--full-index", "HEAD")
        (repo / "docs-plan.md").write_text("concurrent replacement\n", encoding="utf-8")
        head_before_retry = git(repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(WtError, "工作区快照已变化"):
            direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                task=task["id"], message="docs: snapshot", tool="human", session=None,
            ))

        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before_retry)
        self.assertEqual(git(repo, "diff", "--cached", "--binary", "--full-index", "HEAD"), staged_before)
        self.assertEqual((repo / "docs-plan.md").read_text(encoding="utf-8"), "concurrent replacement\n")

    def test_direct_finish_recovers_crash_after_commit_before_ready_registry_save(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("committed before registry save\n", encoding="utf-8")
        original_save = self.reg.save

        def fail_after_commit(row):
            if row.get("id") == task["id"] and row["repos"]["docs"].get("ready_sha"):
                raise RuntimeError("fault injected before ready save")
            return original_save(row)

        with patch.object(self.reg, "save", side_effect=fail_after_commit):
            with self.assertRaisesRegex(RuntimeError, "before ready save"):
                direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                    task=task["id"], message="docs: recover committed intent", tool="human", session=None,
                ))

        committed_sha = git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(committed_sha, task["repos"]["docs"]["base_sha"])
        self.assertEqual(self.reg.load(task["id"])["repos"]["docs"]["pending_commit"]["version"], 2)
        result = direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: recover committed intent", tool="human", session=None,
        ))

        self.assertEqual(result, 0)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), committed_sha)
        row = self.reg.load(task["id"])["repos"]["docs"]
        self.assertEqual(row["ready_sha"], committed_sha)
        self.assertNotIn("pending_commit", row)
        self.assertEqual(self.reg.load(task["id"])["state"], "archived")

    def test_terminal_registry_state_is_persisted_before_lease_release(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("terminal ordering\n", encoding="utf-8")
        lease_type = direct_tasks.direct_checkout.RepoLease
        original_release = lease_type.release
        checked = []

        def assert_archived_then_release(lease, owner):
            self.assertEqual(self.reg.load(owner)["state"], "archived")
            checked.append(owner)
            return original_release(lease, owner)

        with patch.object(lease_type, "release", autospec=True, side_effect=assert_archived_then_release):
            direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                task=task["id"], message="docs: terminal ordering", tool="human", session=None,
            ))
        self.assertEqual(checked, [task["id"]])

    def test_archived_retry_only_clears_its_own_stale_lease(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("terminal cleanup\n", encoding="utf-8")
        lease_type = direct_tasks.direct_checkout.RepoLease
        original_release = lease_type.release
        failed = []

        def crash_at_release(lease, owner):
            if owner == task["id"] and not failed:
                failed.append(owner)
                raise RuntimeError("fault injected before lease release")
            return original_release(lease, owner)

        with patch.object(lease_type, "release", autospec=True, side_effect=crash_at_release):
            with self.assertRaisesRegex(RuntimeError, "before lease release"):
                direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                    task=task["id"], message="docs: terminal cleanup", tool="human", session=None,
                ))

        self.assertEqual(self.reg.load(task["id"])["state"], "archived")
        result = direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: terminal cleanup", tool="human", session=None,
        ))
        self.assertEqual(result, 0)
        self.assertFalse(list((self.cfg.data_root / "locks" / "direct-checkout").glob("*.json")))

        stale = lease_type(repo, owner="T999", runtime_root=self.cfg.data_root).acquire()
        stale.close()
        direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
            task=task["id"], message="docs: terminal cleanup", tool="human", session=None,
        ))
        self.assertEqual(json.loads(stale.record_path.read_text(encoding="utf-8"))["owner"], "T999")

    def test_abort_retry_recovers_crash_after_archive_before_lease_release(self):
        task = self.new_task()
        lease_type = direct_tasks.direct_checkout.RepoLease
        original_release = lease_type.release
        failed = []

        def crash_at_release(lease, owner):
            if owner == task["id"] and not failed:
                failed.append(owner)
                raise RuntimeError("fault injected during abort release")
            return original_release(lease, owner)

        with patch.object(lease_type, "release", autospec=True, side_effect=crash_at_release):
            with self.assertRaisesRegex(RuntimeError, "during abort release"):
                direct_tasks.cmd_abort(self.cfg, self.reg, SimpleNamespace(task=task["id"]))

        self.assertEqual(self.reg.load(task["id"])["state"], "archived")
        self.assertTrue(list((self.cfg.data_root / "locks" / "direct-checkout").glob("*.json")))
        self.assertEqual(direct_tasks.cmd_abort(self.cfg, self.reg, SimpleNamespace(task=task["id"])), 0)
        self.assertFalse(list((self.cfg.data_root / "locks" / "direct-checkout").glob("*.json")))

    def test_direct_master_push_rejects_fetch_or_push_url_mismatch_before_network(self):
        repo = self.cfg.repo("docs").path
        expected = "https://github.com/fangxh2013/aisk-hub.git"
        other = "https://github.com/other/aisk-hub.git"
        git(repo, "remote", "add", "origin", expected)
        repo_cfg = SimpleNamespace(path=repo, automatic={"expected_origin_url": expected})
        cfg = SimpleNamespace(profile_name="aisk-hub")

        for fetch_url, push_url in ((other, expected), (expected, other)):
            git(repo, "remote", "set-url", "origin", fetch_url)
            git(repo, "remote", "set-url", "--push", "origin", push_url)
            with self.subTest(fetch=fetch_url, push=push_url):
                with patch.object(direct_tasks, "_policy", return_value=(repo_cfg, "master", "master")):
                    with patch.object(direct_tasks.git, "run", wraps=direct_tasks.git.run) as run:
                        with self.assertRaisesRegex(WtError, "fetch/push URL"):
                            direct_tasks._push_master(cfg, {}, "main", "deadbeef")
                commands = [tuple(str(x) for x in call.args[0]) for call in run.call_args_list]
                self.assertFalse(any(command and command[0] in ("fetch", "push", "ls-remote")
                                     for command in commands))

    def test_direct_master_push_fails_closed_when_expected_origin_is_missing(self):
        repo_cfg = SimpleNamespace(path=self.cfg.repo("docs").path, automatic={})
        with patch.object(direct_tasks, "_policy", return_value=(repo_cfg, "master", "master")):
            with patch.object(direct_tasks.git, "run", wraps=direct_tasks.git.run) as run:
                with self.assertRaisesRegex(WtError, "expected_origin_url"):
                    direct_tasks._push_master(SimpleNamespace(profile_name="aisk-hub"), {}, "main", "deadbeef")
        self.assertEqual(run.call_count, 0)

    def test_direct_master_push_rechecks_origin_immediately_before_push(self):
        repo = self.cfg.repo("docs").path
        expected = "https://github.com/fangxh2013/aisk-hub.git"
        changed = "https://github.com/changed/aisk-hub.git"
        git(repo, "branch", "master", "HEAD")
        git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        git(repo, "config", "branch.master.remote", "origin")
        git(repo, "config", "branch.master.merge", "refs/heads/master")
        git(repo, "remote", "add", "origin", expected)
        git(repo, "remote", "set-url", "--push", "origin", expected)
        repo_cfg = SimpleNamespace(path=repo, automatic={"expected_origin_url": expected})
        cfg = SimpleNamespace(profile_name="aisk-hub")
        real_run, real_out = direct_tasks.git.run, direct_tasks.git.out
        real_assert = direct_tasks._assert_expected_origin
        validation_count = []
        validation_attempts = []
        network_calls = []

        def offline_run(args, **kwargs):
            command = tuple(str(value) for value in args)
            if command and command[0] == "fetch":
                network_calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            if command and command[0] == "push":
                network_calls.append(command)
                raise AssertionError("push must not run after the origin URL changes")
            return real_run(args, **kwargs)

        def offline_out(args, **kwargs):
            if args and str(args[0]) == "ls-remote":
                return ""  # simulate a remote head that is absent from the preflight result
            return real_out(args, **kwargs)

        def change_after_preflight(repo_path, expected_url):
            validation_attempts.append(1)
            real_assert(repo_path, expected_url)
            validation_count.append(1)
            if len(validation_count) == 4:
                git(repo, "remote", "set-url", "--push", "origin", changed)

        with patch.object(direct_tasks, "_policy", return_value=(repo_cfg, "master", "master")):
            with patch.object(direct_tasks.git, "run", side_effect=offline_run):
                with patch.object(direct_tasks.git, "out", side_effect=offline_out):
                    with patch.object(direct_tasks, "_assert_expected_origin", side_effect=change_after_preflight):
                        with self.assertRaisesRegex(WtError, "fetch/push URL"):
                            direct_tasks._push_master(cfg, {}, "main", git(repo, "rev-parse", "refs/heads/master"))

        self.assertEqual(len(validation_attempts), 5)
        self.assertEqual(len(validation_count), 4)
        self.assertEqual(len(network_calls), 1)
        self.assertEqual(network_calls[0][0:3], ("fetch", "--no-tags", expected))

    def test_dirty_baseline_blocks_task_creation_without_residual_lease(self):
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("existing local work\n", encoding="utf-8")
        with self.assertRaises(WtError):
            self.new_task()
        lease_dir = self.cfg.data_root / "locks" / "direct-checkout"
        self.assertFalse(list(lease_dir.glob("*.json")))
        self.assertEqual(self.reg.all(), [])

    def test_out_of_scope_edit_fails_closed_and_keeps_task_lease(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "outside.txt").write_text("not allowed\n", encoding="utf-8")
        with self.assertRaises(WtError):
            direct_tasks.cmd_finish(self.cfg, self.reg, SimpleNamespace(
                task=task["id"], message="docs: 更新文档", tool="human", session=None,
            ))
        self.assertEqual(git(repo, "branch", "--show-current"), "fxh")
        self.assertTrue(list((self.cfg.data_root / "locks" / "direct-checkout").glob("*.json")))
        self.assertEqual(self.reg.load(task["id"])["state"], "active")

    def test_generic_worktree_archive_refuses_to_delete_a_direct_checkout(self):
        task = self.new_task()
        with self.assertRaisesRegex(WtError, "direct 任务没有 worktree"):
            tasks.cmd_archive(self.cfg, self.reg, SimpleNamespace(
                task=task["id"], force=True, abandon=False, tool="human", session=None,
            ))
        self.assertEqual(git(self.cfg.repo("docs").path, "branch", "--show-current"), "fxh")

    def test_direct_init_skips_anchor_gate_hub_and_preserves_legacy_worktree(self):
        # Make a profile containing only the direct docs checkout: init must not
        # materialize empty worktree infrastructure for it.
        self.prof["worktrees"]["repo_order"] = ["docs"]
        self.prof["worktrees"]["repos"] = {"docs": self.prof["worktrees"]["repos"]["docs"]}
        self.cfg = build_config(self.prof, "mac")
        self.reg = Registry(self.cfg)
        repo = self.cfg.repo("docs").path

        with patch("engine.worktree.doctor.git.version", return_value=(2, 45)):
            result = doctor.cmd_init(self.cfg, self.reg, SimpleNamespace(apply=True))

        self.assertEqual(result, 0)
        self.assertFalse(self.cfg.anchors_dir.exists())
        self.assertFalse(self.cfg.hub.exists())
        self.assertIsNone(git(repo, "config", "--get", "remote.hub.url", check=False) or None)

        legacy = self.cfg.anchor_path("docs", "gate")
        legacy.mkdir(parents=True)
        self.assertTrue(legacy.exists())

        report = doctor.Report()
        with patch("engine.worktree.doctor.git.worktree_list", return_value=[
            {"path": str(repo), "branch": "fxh"}, {"path": str(legacy), "branch": "fxh"},
        ]):
            doctor.check_repo(self.cfg, self.reg, report, "docs", SimpleNamespace(fix=False, ack_refs=False))
        self.assertFalse(any("门禁区缺失" in item or "hub 裸仓" in item for item in report.errs))
        self.assertTrue(any("迁移遗留" in item for item in report.warns))
        self.assertTrue(legacy.exists())

    def test_direct_doctor_checks_task_lease_and_scope_without_worktree_assumptions(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        (repo / "docs-plan.md").write_text("in-scope edit\n", encoding="utf-8")
        report = doctor.Report()

        doctor.check_task(self.cfg, self.reg, report, task)

        self.assertFalse(report.errs)

    def test_host_file_edits_require_matching_direct_task_lease_and_declared_scope(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        allowed = {
            "tool_name": "edit",
            "cwd": str(repo),
            "tool_input": {"file_path": str(repo / "docs-plan.md"), "content": "updated"},
        }
        outside_scope = {
            "tool_name": "edit",
            "cwd": str(repo),
            "tool_input": {"file_path": str(repo / "outside.txt"), "content": "updated"},
        }
        with patch("engine.worktree.guards.config.load_config", return_value=(self.cfg, "test profile")):
            self.assertIsNone(guards.evaluate(allowed, "codex"))
            denied = guards.evaluate(outside_scope, "codex")
        self.assertIn("未声明", denied)
        self.assertEqual(self.reg.load(task["id"])["state"], "active")

    def test_direct_host_shell_writes_are_refused_but_reads_and_direct_finish_are_routed(self):
        task = self.new_task()
        repo = self.cfg.repo("docs").path
        with patch("engine.worktree.guards.config.load_config", return_value=(self.cfg, "test profile")):
            status = guards.evaluate({"tool_name": "bash", "cwd": str(repo),
                                     "tool_input": {"command": "git status --short"}}, "codex")
            denied = guards.evaluate({"tool_name": "bash", "cwd": str(repo),
                                      "tool_input": {"command": "touch docs-plan.md"}}, "codex")
            git_cwd_denied = guards.evaluate({"tool_name": "bash", "cwd": str(self.root),
                                              "tool_input": {"command": f"git -C {repo} add docs-plan.md"}}, "codex")
            finish = guards.evaluate({"tool_name": "bash", "cwd": str(repo),
                                      "tool_input": {"command": f"aisk task direct-finish {task['id']} -m 'docs: finish'"}}, "codex")
        self.assertIsNone(status)
        self.assertIn("direct-finish", denied)
        self.assertIn("direct-finish", git_cwd_denied)
        self.assertIsNone(finish)

    def test_editing_unrelated_file_from_inside_a_direct_repo_cwd_is_not_misattributed(self):
        """2026-09-23 实测：cwd 在 direct 仓库里编辑一个完全无关的文件（例如
        ~/.claude/settings.json）被误判成对该仓库的写入而拒绝——`direct_repo_context`
        当时把 cwd 当成候选之一，且排在声明路径之前，命中即返回，从没检查过目标路径
        是否真的落在该仓库内。声明了 `file_path` 的工具（Edit/Write）必须只认目标路径，
        不认 cwd。这里刻意不创建任何 direct 任务/租约，证明这条路径完全不经过租约判断。"""
        repo = self.cfg.repo("docs").path
        unrelated = self.root / "outside-any-repo.txt"
        unrelated.write_text("pre-existing\n", encoding="utf-8")
        payload = {
            "tool_name": "edit",
            "cwd": str(repo),
            "tool_input": {"file_path": str(unrelated), "content": "updated"},
        }
        with patch("engine.worktree.guards.config.load_config", return_value=(self.cfg, "test profile")):
            self.assertIsNone(guards.evaluate(payload, "claude"))

    def test_direct_host_shell_read_chain_is_evaluated_segment_by_segment(self):
        """2026-09-23 实测：`git status && echo x && git branch -a && git log --oneline -5`
        这类纯只读诊断链被整体拒绝——旧版 `_direct_shell_is_safe_read` 要求"恰好一个
        simple segment"，任何 `&&`/`;` 组合都直接判定为不安全，不管每一段本身是否安全。
        `echo` 当时也不在白名单里，单独一条 `echo hello` 都会被拒。这里验证：每段独立
        判断只读，链式命令才放行；只要有一段不安全（如 `rm`），整条命令仍必须整体拒绝
        （PreToolUse 是执行前的整体拦截，没有"放行一半"这回事）。"""
        self.new_task()
        repo = self.cfg.repo("docs").path
        with patch("engine.worktree.guards.config.load_config", return_value=(self.cfg, "test profile")):
            chain = guards.evaluate({"tool_name": "bash", "cwd": str(repo), "tool_input": {
                "command": "git status --short --branch && echo hello && git branch -a && git log --oneline -3"}},
                "claude")
            solo_echo = guards.evaluate({"tool_name": "bash", "cwd": str(repo),
                                         "tool_input": {"command": "echo hello"}}, "claude")
            mixed_with_write = guards.evaluate({"tool_name": "bash", "cwd": str(repo), "tool_input": {
                "command": "git status --short && rm -rf docs-plan.md"}}, "claude")
        self.assertIsNone(chain)
        self.assertIsNone(solo_echo)
        self.assertIn("direct-finish", mixed_with_write)

    def test_legacy_promote_refuses_all_direct_repositories(self):
        rc = self.cfg.repo("docs")
        with self.assertRaisesRegex(integrate.Reject, "direct-finish"):
            integrate.promote_one(self.cfg, self.reg, "docs", rc, dry=True)

        result = integrate.cmd_promote(self.cfg, self.reg, SimpleNamespace(
            repos="docs", dry_run=True,
        ))
        self.assertEqual(result, 1)
        self.assertEqual(git(rc.path, "branch", "--show-current"), "fxh")


if __name__ == "__main__":
    unittest.main()
