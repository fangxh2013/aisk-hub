"""Isolated tests for direct-checkout baselines and repository write leases."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.direct_checkout import (  # noqa: E402
    BaselineViolation,
    DirtyBaselineError,
    DirectCheckoutError,
    RepoLease,
    RepoLeaseBusy,
    RepoLeaseNotHeld,
    acquire_repo_lease,
    capture_baseline,
    recover_repo_lease,
    validate_task_changes,
)


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
    git(repo, "config", "user.name", "Aisk Test")
    git(repo, "config", "user.email", "aisk-test@example.test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", "README.md", "docs/guide.md", "src/app.py")
    git(repo, "commit", "-q", "-m", "feat: 初始化测试仓库")


class DirectCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aisk-direct-checkout-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.runtime = self.root / "runtime"
        init_repo(self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def lease(self, owner="T001", repo=None):
        return acquire_repo_lease(repo or self.repo, owner=owner, runtime_root=self.runtime)

    def baseline(self, lease=None, allowed=("docs",)):
        lease = lease or self.lease()
        return capture_baseline(self.repo, allowed, lease=lease)

    def test_clean_named_branch_baseline_and_allowed_changes(self):
        with self.lease() as lease:
            baseline = self.baseline(lease)
            self.assertEqual(baseline.branch, "fxh")
            self.assertEqual(baseline.head, git(self.repo, "rev-parse", "HEAD"))
            (self.repo / "docs" / "guide.md").write_text("updated\n", encoding="utf-8")
            (self.repo / "docs" / "new.md").write_text("new\n", encoding="utf-8")

            report = validate_task_changes(self.repo, baseline, lease=lease)

            self.assertTrue(report.ok)
            self.assertEqual(report.changed_paths, ("docs/guide.md", "docs/new.md"))
            self.assertEqual(report.out_of_scope, ())
            lease.release("T001")

    def test_dirty_start_is_rejected_without_changing_existing_content(self):
        (self.repo / "README.md").write_text("operator edit\n", encoding="utf-8")
        before = (self.repo / "README.md").read_bytes()

        with self.lease() as lease:
            with self.assertRaises(DirtyBaselineError) as caught:
                self.baseline(lease)
            lease.release("T001")

        self.assertIn("README.md", caught.exception.paths)
        self.assertEqual((self.repo / "README.md").read_bytes(), before)

    def test_out_of_scope_tracked_and_untracked_changes_are_reported_not_reverted(self):
        with self.lease() as lease:
            baseline = self.baseline(lease, allowed=("docs",))
            (self.repo / "docs" / "guide.md").write_text("task change\n", encoding="utf-8")
            git(self.repo, "add", "docs/guide.md")
            (self.repo / "src" / "app.py").write_text("concurrent change\n", encoding="utf-8")
            (self.repo / "outside.txt").write_text("untracked outside\n", encoding="utf-8")
            before = {
                "guide": (self.repo / "docs" / "guide.md").read_bytes(),
                "source": (self.repo / "src" / "app.py").read_bytes(),
                "outside": (self.repo / "outside.txt").read_bytes(),
            }

            report = validate_task_changes(self.repo, baseline, lease=lease)

            self.assertFalse(report.ok)
            self.assertEqual(report.out_of_scope, ("outside.txt", "src/app.py"))
            self.assertEqual((self.repo / "docs" / "guide.md").read_bytes(), before["guide"])
            self.assertEqual((self.repo / "src" / "app.py").read_bytes(), before["source"])
            self.assertEqual((self.repo / "outside.txt").read_bytes(), before["outside"])
            with self.assertRaises(BaselineViolation):
                report.raise_if_invalid()
            lease.release("T001")

    def test_changed_branch_is_reported_as_baseline_drift(self):
        with self.lease() as lease:
            baseline = self.baseline(lease)
            git(self.repo, "switch", "-q", "-c", "other")

            report = validate_task_changes(self.repo, baseline, lease=lease)

            self.assertFalse(report.ok)
            self.assertTrue(any("分支" in item for item in report.baseline_mismatches))
            self.assertEqual(report.current_branch, "other")
            lease.release("T001")

    def test_advanced_head_is_reported_as_baseline_drift(self):
        with self.lease() as lease:
            baseline = self.baseline(lease)
            (self.repo / "docs" / "guide.md").write_text("committed\n", encoding="utf-8")
            git(self.repo, "add", "docs/guide.md")
            git(self.repo, "commit", "-q", "-m", "docs: advance head")

            report = validate_task_changes(self.repo, baseline, lease=lease)

            self.assertFalse(report.ok)
            self.assertTrue(any("HEAD" in item for item in report.baseline_mismatches))
            lease.release("T001")

    def test_persistent_lease_resumes_for_owner_and_requires_explicit_release(self):
        first = self.lease("T001")
        created_at = first.metadata["created_at"]
        with first:
            second = RepoLease(self.repo, owner="T002", runtime_root=self.runtime)
            with self.assertRaises(RepoLeaseBusy):
                second.acquire()
        with self.assertRaises(RepoLeaseBusy):
            self.lease("T002")

        resumed = self.lease("T001")
        with resumed:
            self.assertTrue(resumed.held)
            self.assertEqual(resumed.metadata["owner"], "T001")
            self.assertEqual(resumed.metadata["created_at"], created_at)
            before_count = resumed.metadata["heartbeat_count"]
            with self.assertRaises(RepoLeaseNotHeld):
                resumed.heartbeat("T002")
            self.assertEqual(resumed.heartbeat("T001")["heartbeat_count"], before_count + 1)
            with self.assertRaises(RepoLeaseNotHeld):
                resumed.release("T002")
            resumed.release("T001")

        with self.lease("T003") as reacquired:
            self.assertTrue(reacquired.held)
            reacquired.release("T003")

    def test_old_heartbeat_fails_closed_until_explicit_owner_matched_recovery(self):
        with self.lease("T001") as lease:
            record = json.loads(lease.record_path.read_text(encoding="utf-8"))
            record["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
            lease.record_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(RepoLeaseBusy):
            self.lease("T002")
        with self.assertRaises(RepoLeaseNotHeld):
            recover_repo_lease(self.repo, expected_owner="T002", reason="误填 owner", runtime_root=self.runtime)
        self.assertTrue(lease.record_path.exists(), "错误 owner 的恢复请求不得删除记录")

        recovered = recover_repo_lease(
            self.repo, expected_owner="T001", reason="确认旧任务已终止", runtime_root=self.runtime,
        )
        self.assertEqual(recovered["owner"], "T001")
        self.assertEqual(recovered["heartbeat_at"], "2000-01-01T00:00:00+00:00")
        with self.lease("T002") as new_owner:
            new_owner.release("T002")

    def test_persistent_lease_survives_process_exit_and_resumes_in_another_process(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(KERNEL) + os.pathsep + env.get("PYTHONPATH", "")
        acquire_and_close = (
            "import sys; from engine.direct_checkout import acquire_repo_lease; "
            "repo, runtime, owner = sys.argv[1:]; "
            "lease = acquire_repo_lease(repo, owner=owner, runtime_root=runtime); "
            "print(lease.metadata['created_at']); lease.close()"
        )
        acquire_and_release = (
            "import sys; from engine.direct_checkout import acquire_repo_lease; "
            "repo, runtime, owner = sys.argv[1:]; "
            "lease = acquire_repo_lease(repo, owner=owner, runtime_root=runtime); "
            "print(lease.metadata['created_at']); lease.release(owner); lease.close()"
        )

        first = subprocess.run(
            [sys.executable, "-c", acquire_and_close, str(self.repo), str(self.runtime), "T001"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        other_owner = subprocess.run(
            [sys.executable, "-c", acquire_and_close, str(self.repo), str(self.runtime), "T002"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(other_owner.returncode, 0)
        self.assertIn("不会按超时自动接管", other_owner.stderr)

        resumed = subprocess.run(
            [sys.executable, "-c", acquire_and_release, str(self.repo), str(self.runtime), "T001"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(first.stdout.strip(), resumed.stdout.strip(), "跨进程续接保留原 created_at")

    def test_other_repository_has_an_independent_lease(self):
        other_repo = self.root / "other"
        init_repo(other_repo)
        with self.lease("T001") as first:
            with self.lease("T002", repo=other_repo) as other:
                self.assertTrue(other.held)
                other.release("T002")
            first.release("T001")

    def test_baseline_round_trip_and_owner_binding(self):
        with self.lease("T001") as lease:
            baseline = self.baseline(lease, allowed=("docs/", "src/app.py"))
            restored = type(baseline).from_dict(baseline.to_dict())
            self.assertEqual(restored, baseline)
            with self.assertRaises(RepoLeaseNotHeld):
                lease.release("T002")
            lease.release("T001")

        with self.lease("T002") as other_owner:
            with self.assertRaises(RepoLeaseNotHeld):
                validate_task_changes(self.repo, baseline, lease=other_owner)
            other_owner.release("T002")

    def test_baseline_requires_held_lease(self):
        lease = RepoLease(self.repo, owner="T001", runtime_root=self.runtime)
        with self.assertRaises(RepoLeaseNotHeld):
            capture_baseline(self.repo, ("docs",), lease=lease)

    def test_declared_paths_reject_escape_and_wildcards(self):
        with self.lease() as lease:
            for bad_path in ("../outside", "/tmp/file", "C:/outside", "src/*.py", ".git/config"):
                with self.subTest(path=bad_path):
                    with self.assertRaises(DirectCheckoutError):
                        capture_baseline(self.repo, (bad_path,), lease=lease)
            lease.release("T001")


if __name__ == "__main__":
    unittest.main()
