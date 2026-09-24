import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine import privacy  # noqa: E402


class GitHistoryReferenceTests(unittest.TestCase):
    def test_task_branch_does_not_scan_unpublished_local_master_history(self):
        with tempfile.TemporaryDirectory(prefix="privacy-history-") as raw:
            repo = Path(raw)
            run = lambda *args: subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
            )
            run("init", "-q", "-b", "task")
            (repo / "README.md").write_text("clean\n", encoding="utf-8")
            run("add", "README.md")
            run("-c", "user.name=Tester", "-c", "user.email=test@example.test", "commit", "-qm", "clean")

            run("switch", "--orphan", "master")
            (repo / "leak.txt").write_text("LEAK\n", encoding="utf-8")
            run("add", "leak.txt")
            run("-c", "user.name=Tester", "-c", "user.email=test@example.test", "commit", "-qm", "private")
            run("switch", "task")

            rules = (("test-marker", re.compile("LEAK"), "LEAK"),)
            with patch.object(privacy, "compile_rules", return_value=rules):
                self.assertEqual([], privacy.scan_git_history(repo))
                run("switch", "master")
                findings = privacy.scan_git_history(repo)
                self.assertTrue(findings)
                self.assertEqual("test-marker", findings[0]["rule"])


class SinceRangeTests(unittest.TestCase):
    """`public verify --since`：任务发布门禁只为待发布提交负责，不背已发布的历史。"""

    RULES = (("test-marker", re.compile("LEAK"), "LEAK"),)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="privacy-since-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "master")
        self.commit("notes.txt", "a\nb\n", "base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def commit(self, name, text, message):
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        self.git("-c", "user.name=Tester", "-c", "user.email=test@example.test", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD").strip()

    def scan(self, since="published"):
        with patch.object(privacy, "compile_rules", return_value=self.RULES):
            return privacy.scan_since(self.repo, since)

    def test_published_history_leak_is_not_blamed_on_a_clean_new_commit(self):
        self.commit("old.txt", "LEAK\n", "leak already published")
        self.git("branch", "published")
        self.commit("notes.txt", "a\nb\nclean\n", "clean follow-up")
        self.assertEqual([], self.scan())
        with patch.object(privacy, "compile_rules", return_value=self.RULES):
            ok, _ = privacy.verify(self.repo, since="published")
            self.assertTrue(ok)
            self.assertTrue(privacy.scan_git_history(self.repo), "完整历史扫描仍要报出旧泄露")

    def test_leak_added_then_removed_inside_range_is_still_caught_with_line(self):
        self.git("branch", "published")
        leaked = self.commit("notes.txt", "a\nb\nLEAK\n", "adds leak")
        self.commit("notes.txt", "a\nb\n", "removes it again")
        findings = self.scan()
        self.assertEqual([{"rule": "test-marker", "path": f"{leaked[:12]}:notes.txt", "line": 3}], findings)

    def test_unknown_since_ref_fails_closed(self):
        findings = self.scan("no-such-ref")
        self.assertEqual("unpublished-range-unknown", findings[0]["rule"])


if __name__ == "__main__":
    unittest.main()
