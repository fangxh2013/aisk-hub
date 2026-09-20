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


if __name__ == "__main__":
    unittest.main()
