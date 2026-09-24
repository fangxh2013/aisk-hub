"""Isolated quota tests using temporary repositories and task registries."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import tasks  # noqa: E402
from engine.worktree.config import WtError, build_config  # noqa: E402
from engine.worktree.registry import Registry, file_lock  # noqa: E402


def git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(os.fsdecode(result.stderr))
    return os.fsdecode(result.stdout).strip()


def init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "fxh")
    git(repo, "config", "user.name", "Quota Test")
    git(repo, "config", "user.email", "quota-test@example.test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "feat: quota fixture")
    git(repo, "branch", "dev")
    git(repo, "branch", "main")


def make_profile(root, *, quota_active=8, active_worktrees=0, materialized=0,
                 be_limits=None, web_limits=None, docs_limits=None):
    repo_paths = {alias: root / "repos" / alias for alias in ("be", "web", "docs")}
    declarations = {}
    for alias, mode, limits in (
        ("be", "task-worktree", be_limits or {}),
        ("web", "task-worktree", web_limits or {}),
        ("docs", "direct", docs_limits or {}),
    ):
        declarations[alias] = {
            "profile_repo": alias,
            "workspace_mode": mode,
            "limits": dict(limits),
            "trunk": "dev",
            "push_branch": "fxh-dev",
            "promote": "ff-trunk",
            "anchors": [],
            "gate": {"kind": "none"},
        }
    return {
        "project": "quota-test",
        "_path": root / "quota-test.yaml",
        "repos": {alias: str(path) for alias, path in repo_paths.items()},
        "worktrees": {
            "data_root": str(root / "runtime"),
            "integration_branch": "fxh",
            "protected": ["main", "dev", "fxh"],
            "repo_order": ["be", "web", "docs"],
            "quotas": {
                "active": quota_active,
                "builds": 2,
                "active_worktrees": active_worktrees,
                "materialized": materialized,
            },
            "repos": declarations,
        },
    }


class WorktreeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-quota-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Quota Test",
            "GIT_AUTHOR_EMAIL": "quota-test@example.test",
            "GIT_COMMITTER_NAME": "Quota Test",
            "GIT_COMMITTER_EMAIL": "quota-test@example.test",
        })
        env.start()
        self.addCleanup(env.stop)
        self.profile = make_profile(self.root)
        for repo in self.profile["repos"].values():
            init_repo(Path(repo))
        self.cfg = build_config(self.profile, "mac")
        self.reg = Registry(self.cfg)

    def add_worktree(self, alias, task_id):
        task_dir = self.cfg.tasks_dir / f"{task_id}-quota-test"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / alias
        branch = f"ai/{task_id}-{alias}"
        git(self.cfg.repo(alias).path, "worktree", "add", "-q", "-b", branch, str(path), "fxh")
        return path

    def save_task(self, task_id, *, state="active", repos=None, creating=None, creating_repos=None, task_dir=None):
        directory = task_dir or (self.cfg.tasks_dir / f"{task_id}-quota-test")
        return self.reg.save({
            "id": task_id,
            "title": f"配额任务 {task_id}",
            "os": "mac",
            "state": state,
            "dir": str(directory),
            "repos": dict(repos or {}),
            **({"creating": creating} if creating is not None else {}),
            **({"creating_repos": list(creating_repos)} if creating_repos is not None else {}),
        })

    def check_quota(self, *, exclude=None, repos=None, materialize=None):
        with self.reg.lock():
            return tasks.check_quota(self.cfg, self.reg, exclude=exclude, repos=repos, materialize=materialize)

    def test_legacy_global_active_task_cap_counts_task_records(self):
        self.cfg.quota_active = 1
        self.save_task("T001", state="active")

        with self.assertRaisesRegex(WtError, "进行中的任务已达上限"):
            self.check_quota(repos=["be"], materialize=["be"])

    def test_global_active_worktree_cap_counts_new_requested_slots(self):
        self.cfg.quota_active_worktrees = 1
        worktree = self.add_worktree("be", "T001")
        self.save_task("T001", state="active", repos={"be": {"path": str(worktree)}})

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)
        self.assertEqual(snapshot["active_total"], 1)
        with self.assertRaisesRegex(WtError, "活跃任务 worktree"):
            self.check_quota(repos=["web"], materialize=[])

    def test_per_repo_active_worktree_cap(self):
        self.cfg.repo("be").limits["active_worktrees"] = 1
        worktree = self.add_worktree("be", "T001")
        self.save_task("T001", state="active", repos={"be": {"path": str(worktree)}})

        with self.assertRaisesRegex(WtError, "仓库 be 活跃 worktree"):
            self.check_quota(repos=["be"], materialize=[])

    def test_full_slot_message_names_holders_and_the_actions_that_free_one(self):
        """2026-09-24：槽位满时另一个 AI 建议「停掉一个会话」——那释放不了槽位（按任务状态计）。
        报错必须点名占位任务，并说清只有暂停/落地/归档才释放。"""
        self.cfg.repo("be").limits["active_worktrees"] = 1
        worktree = self.add_worktree("be", "T001")
        self.save_task("T001", state="queued", repos={"be": {"path": str(worktree)}})

        with self.assertRaises(WtError) as caught:
            self.check_quota(repos=["be"], materialize=[])
        message = str(caught.exception)
        self.assertIn("T001(queued)", message)
        self.assertIn("aisk task pause", message)
        self.assertIn("只停掉 AI 会话不会释放", message)

    def queued_task_with_real_worktree(self):
        worktree = self.add_worktree("be", "T001")
        return self.save_task("T001", state="queued", repos={
            "be": {"path": str(worktree), "branch": "ai/T001-be", "ready_sha": "0" * 40}})

    def test_stale_queued_without_a_land_process_recovers_to_rejected(self):
        """2026-09-24 T028：land 在等确认弹窗时进程被杀，queued 永久残留，commit/ready/pause 全被拒。"""
        task = self.queued_task_with_real_worktree()

        recovered = tasks.recover_stale_queued(self.cfg, self.reg, task)

        self.assertEqual("rejected", recovered["state"])
        stored = self.reg.load("T001")
        self.assertEqual("rejected", stored["state"])
        self.assertEqual(("queued", "rejected"), (stored["history"][-1]["from"], stored["history"][-1]["to"]))
        self.assertIn("中断", stored["last_reject"]["reason"])
        # 恢复后 commit / ready / pause 共用的状态门槛必须放行。
        tasks.require_local(self.cfg, stored, ("active", "rejected", "parked", "ready"))

    def test_queued_task_is_left_alone_while_a_land_holds_the_lock(self):
        task = self.queued_task_with_real_worktree()

        with file_lock(tasks.land_lock_path(self.cfg, "be")):
            untouched = tasks.recover_stale_queued(self.cfg, self.reg, task)

        self.assertEqual("queued", untouched["state"])
        self.assertEqual("queued", self.reg.load("T001")["state"])

    def test_recovery_ignores_tasks_that_are_not_queued(self):
        worktree = self.add_worktree("be", "T001")
        task = self.save_task("T001", state="active", repos={"be": {"path": str(worktree)}})

        self.assertEqual("active", tasks.recover_stale_queued(self.cfg, self.reg, task)["state"])
        self.assertEqual([], self.reg.load("T001").get("history") or [])

    def test_paused_task_still_occupies_global_and_per_repo_physical_slots(self):
        self.cfg.quota_materialized = 1
        self.cfg.repo("be").limits["materialized_worktrees"] = 1
        worktree = self.add_worktree("be", "T001")
        self.save_task("T001", state="parked", repos={"be": {"path": str(worktree)}})

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)
        self.assertEqual(snapshot["physical_by_repo"]["be"], 1)
        self.assertEqual(snapshot["active_by_repo"]["be"], 0)
        self.assertEqual(snapshot["physical_total"], 1)
        self.assertEqual(snapshot["active_total"], 0)
        with self.assertRaisesRegex(WtError, "物理槽位"):
            self.check_quota(repos=[], materialize=["be"])

    def test_per_repo_materialized_cap(self):
        self.cfg.repo("be").limits["materialized_worktrees"] = 1
        worktree = self.add_worktree("be", "T001")
        self.save_task("T001", state="landed", repos={"be": {"path": str(worktree)}})

        with self.assertRaisesRegex(WtError, "仓库 be 物理 worktree"):
            self.check_quota(repos=[], materialize=["be"])

    def test_orphan_task_directory_worktree_is_counted_without_task_record(self):
        worktree = self.add_worktree("be", "T900")

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)

        self.assertTrue(worktree.exists())
        self.assertEqual(snapshot["physical_by_repo"]["be"], 1)
        self.assertEqual(snapshot["active_by_repo"]["be"], 0)
        self.assertEqual(snapshot["physical_total"], 1)

    def test_creation_reservation_prevents_second_creator_taking_last_slot(self):
        self.cfg.quota_materialized = 1
        with self.reg.lock():
            tasks.check_quota(self.cfg, self.reg, repos=["be"], materialize=["be"])
            reserved_dir = self.cfg.tasks_dir / "T001-reservation"
            self.save_task(
                "T001", state="active", creating="now", creating_repos=["be"],
                task_dir=reserved_dir,
            )

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)
        self.assertEqual(snapshot["physical_total"], 1)
        self.assertEqual(snapshot["active_total"], 1)
        with self.assertRaisesRegex(WtError, "物理槽位"):
            self.check_quota(repos=["be"], materialize=["be"])

    def test_direct_mode_aliases_do_not_consume_worktree_quotas(self):
        self.cfg.quota_active_worktrees = 1
        self.cfg.quota_materialized = 1
        self.cfg.repo("docs").limits.update({"active_worktrees": 0, "materialized_worktrees": 0})
        direct_path = self.cfg.tasks_dir / "T001-direct" / "docs"
        direct_path.mkdir(parents=True)
        self.save_task(
            "T001", state="active", repos={"docs": {"path": str(direct_path)}},
            creating="now", creating_repos=["docs"], task_dir=direct_path.parent,
        )

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)

        self.assertEqual(snapshot["physical_by_repo"]["docs"], 0)
        self.assertEqual(snapshot["active_by_repo"]["docs"], 0)
        self.assertIsNone(self.check_quota(repos=["docs"], materialize=["docs"]))

    def test_quota_snapshot_skips_existing_non_git_profile_directory(self):
        broken = self.root / "repos" / "not-a-git-checkout"
        broken.mkdir()
        self.cfg.repo("docs").path = broken

        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)

        self.assertEqual(snapshot["physical_by_repo"]["docs"], 0)
        self.assertEqual(snapshot["active_by_repo"]["docs"], 0)

    def test_claim_parked_checkout_at_physical_limit_does_not_double_reserve(self):
        self.cfg.quota_active_worktrees = 1
        self.cfg.quota_materialized = 1
        self.cfg.repo("be").limits.update({"active_worktrees": 1, "materialized_worktrees": 1})
        task_dir = self.cfg.tasks_dir / "T001-parked"
        repo_path = task_dir / "be"
        repo_path.mkdir(parents=True)
        self.save_task("T001", state="parked", repos={"be": {"path": str(repo_path)}}, task_dir=task_dir)

        with patch.object(tasks, "require_local"):
            result = tasks.cmd_claim(self.cfg, self.reg, SimpleNamespace(
                task="T001", tool="human", session=None, takeover=False, reason="",
            ))

        self.assertEqual(result, 0)
        self.assertEqual(self.reg.load("T001")["state"], "active")
        snapshot = tasks.worktree_quota_snapshot(self.cfg, self.reg)
        self.assertEqual(snapshot["physical_by_repo"]["be"], 1)
        self.assertEqual(snapshot["active_by_repo"]["be"], 1)
        queued = self.reg.load("T001")
        self.reg.set_state(queued, "queued", note="recovering local land")
        self.assertEqual(tasks.worktree_quota_snapshot(self.cfg, self.reg)["active_by_repo"]["be"], 1)

    def test_disk_budget_blocks_new_materialization_at_or_above_budget(self):
        self.cfg.retention["disk_budget_gb"] = 2
        self.cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
        for used in (2 * (1 << 30), 3 * (1 << 30)):
            with self.subTest(used=used), patch.object(tasks, "dir_size", return_value=used) as size:
                with self.assertRaisesRegex(WtError, "aisk task gc 预览"):
                    self.check_quota(repos=["be"], materialize=["be"])
                size.assert_called_once_with(self.cfg.tasks_dir)

    def test_disk_budget_does_not_block_without_new_materialization(self):
        self.cfg.retention["disk_budget_gb"] = 0
        self.cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(tasks, "dir_size", return_value=10 * (1 << 30)) as size:
            self.assertIsNone(self.check_quota(repos=["be"], materialize=[]))
        size.assert_not_called()

    def test_xinhua_backend_and_frontend_must_be_separate_tasks(self):
        cfg = SimpleNamespace(
            profile_name="xinhua",
            ai_re=re.compile(r"(?!)"),
            repo=lambda alias: SimpleNamespace(workspace_mode="task-worktree"),
        )
        with self.assertRaisesRegex(WtError, "必须拆成独立任务"):
            tasks.create_task(cfg, object(), slug="split-repos", title="前后端拆分",
                              repos=["be", "web"], goal="独立交付", accept="各自验收")


if __name__ == "__main__":
    unittest.main()
