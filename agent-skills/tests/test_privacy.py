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

    def test_since_gate_ignores_the_history_baseline(self):
        """待发布的提交还来得及改：登记进基线也照样拦，否则「引入、删掉并登记」就能随历史推出去。"""
        self.git("branch", "published")
        leaked = self.commit("notes.txt", "a\nb\nLEAK\n", "adds leak")
        write_baseline(self.repo, [f"{leaked}:notes.txt:test-marker:3"])
        self.commit("notes.txt", "a\nb\n", "removes it and registers it")
        self.assertEqual([{"rule": "test-marker", "path": f"{leaked[:12]}:notes.txt", "line": 3}], self.scan())


def write_baseline(repo, entries, version=1):
    lines = [f"version: {version}", "accepted_findings:"] + [f'- "{entry}"' for entry in entries]
    path = Path(repo) / privacy.BASELINE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class HistoryBaselineTests(unittest.TestCase):
    """spec/privacy-history-baseline.yaml：只放行已经从文件树删掉的已发布历史，其余照常拦。"""

    RULES = (("test-marker", re.compile("LEAK"), "LEAK"),)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="privacy-baseline-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "master")
        self.commit({"notes.txt": "clean\n"}, "base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def commit(self, files, message):
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("-c", "user.name=Tester", "-c", "user.email=test@example.test", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD").strip()

    def publish(self):
        """把当前 HEAD 记为已发布（origin/master），基线只承认已发布的提交。"""
        self.git("update-ref", privacy.PUBLISHED_REF, "HEAD")

    def history(self):
        with patch.object(privacy, "compile_rules", return_value=self.RULES):
            return privacy.history_scan(self.repo)

    def test_exact_fingerprint_of_removed_history_is_accepted_but_reported(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:1"])
        self.commit({"old.txt": "clean\n"}, "remove leak and register it")
        self.publish()
        result = self.history()
        self.assertEqual([], result["blocking"])
        self.assertEqual([{"rule": "test-marker", "path": f"{leaked[:12]}:old.txt", "line": 1}], result["accepted"])
        self.assertEqual([], result["stale_baseline"])
        with patch.object(privacy, "compile_rules", return_value=self.RULES):
            report = privacy.verify_report(self.repo)
        self.assertTrue(report["ok"])
        self.assertEqual(1, len(report["accepted_history"]), "放行的命中必须如实报告，不能静默")

    def test_fingerprint_must_match_exactly(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:2"])
        self.commit({"old.txt": "clean\n"}, "remove leak, wrong line registered")
        self.publish()
        result = self.history()
        self.assertEqual(1, len(result["blocking"]))
        self.assertEqual([f"{leaked}:old.txt:test-marker:2"], result["stale_baseline"])

    def test_tip_commit_is_never_accepted(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak at the tip")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:1"])  # 未提交的基线也骗不过顶端
        self.publish()
        blocking = self.history()["blocking"]
        self.assertEqual(1, len(blocking))
        self.assertIn("顶端", blocking[0]["detail"])

    def test_leak_still_in_the_tree_stays_blocking(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:1"])
        tip = self.commit({"notes.txt": "more\n"}, "leak still present downstream")
        self.publish()
        result = self.history()
        self.assertEqual([f"{tip[:12]}:old.txt"], [item["path"] for item in result["blocking"]])
        self.assertEqual(1, len(result["accepted"]))

    def test_accepted_findings_do_not_consume_the_blocking_cap(self):
        older = self.commit({"x.txt": "LEAK\n"}, "older leak, never registered")
        many = self.commit({"x.txt": "clean\n", "y.txt": "LEAK\n" * 25}, "25 leaks")
        write_baseline(self.repo, [f"{many}:y.txt:test-marker:{n}" for n in range(1, 26)])
        self.commit({"y.txt": "clean\n"}, "remove and register the 25")
        self.publish()
        result = self.history()
        self.assertEqual(25, len(result["accepted"]))
        self.assertEqual([f"{older[:12]}:x.txt"], [item["path"] for item in result["blocking"]])

    def test_unpublished_commits_cannot_be_registered(self):
        # 一个 PR 在 A 引入泄漏、B 删掉并登记 A：A 还没进 origin/master，必须照样拦下
        self.publish()
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak inside the pull request")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:1"])
        self.commit({"old.txt": "clean\n"}, "remove and register it in the same pull request")
        result = self.history()
        self.assertEqual([f"{leaked[:12]}:old.txt"], [item["path"] for item in result["blocking"]])
        self.assertIn("还没有进入", result["blocking"][0]["detail"])
        self.assertEqual([], result["accepted"])
        self.assertEqual([], result["stale_baseline"], "被拒绝的条目不是失效条目")

    def test_without_the_published_ref_nothing_is_accepted(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak")
        write_baseline(self.repo, [f"{leaked}:old.txt:test-marker:1"])
        self.commit({"old.txt": "clean\n"}, "remove and register")
        blocking = self.history()["blocking"]
        self.assertEqual(1, len(blocking))
        self.assertIn("git fetch origin", blocking[0]["detail"])

    def test_malformed_baseline_fails_closed(self):
        leaked = self.commit({"old.txt": "LEAK\n"}, "leak")
        self.commit({"old.txt": "clean\n"}, "remove")
        for entries, version in (
            ([f"{leaked[:12]}:old.txt:test-marker:1"], 1),       # 缩写提交号
            ([f"{leaked}:old.txt:no-such-rule:1"], 1),           # 不存在的规则
            ([f"{leaked}:../old.txt:test-marker:1"], 1),         # 路径越界
            ([f"{leaked}:old.txt:test-marker:0"], 1),            # 行号非法
            ([f"{leaked}:old.txt:test-marker:1"] * 2, 1),        # 重复条目
            ([f"{leaked}:old.txt:test-marker:1"], 2),            # 未知版本
        ):
            with self.subTest(entries=entries, version=version):
                write_baseline(self.repo, entries, version=version)
                blocking = self.history()["blocking"]
                self.assertEqual("history-baseline-invalid", blocking[0]["rule"])

    def test_committed_baseline_is_valid_and_points_at_real_commits(self):
        root = KERNEL.parent
        rule_ids = [rule_id for rule_id, _pattern, _prefilter in privacy.compile_rules(root)]
        baseline = privacy.load_history_baseline(root, rule_ids)
        if not (root / ".git").exists():
            self.skipTest("不是 git 检出，无法核对提交")
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        has_published = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                                        privacy.PUBLISHED_REF], capture_output=True).returncode == 0
        for commit, path, _rule, _line in baseline:
            self.assertNotEqual(head, commit)
            if has_published:
                ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", commit,
                                           privacy.PUBLISHED_REF], capture_output=True)
                self.assertEqual(0, ancestor.returncode, f"基线指向的 {commit[:12]} 不在已发布的 origin/master 里")
            present = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}:{path}"],
                                     capture_output=True)
            self.assertEqual(0, present.returncode, f"基线指向的 {commit[:12]}:{path} 不在本仓库历史里")


class PublicExportTests(unittest.TestCase):
    def test_export_copies_only_tracked_files(self):
        with tempfile.TemporaryDirectory(prefix="privacy-export-") as raw:
            source, dest = Path(raw) / "src", Path(raw) / "out"
            (source / "docs").mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "docs" / "public.md").write_text("public\n", encoding="utf-8")
            (source / "README.md").write_text("readme\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=source, check=True)
            (source / "docs" / "local-only.md").write_text("never scanned\n", encoding="utf-8")
            copied = privacy.copy_public(source, dest)
            self.assertIn("docs/", copied)
            self.assertTrue((dest / "docs" / "public.md").is_file())
            self.assertTrue((dest / "README.md").is_file())
            self.assertFalse((dest / "docs" / "local-only.md").exists(), "未跟踪文件未经扫描，不能进入公开候选")


if __name__ == "__main__":
    unittest.main()
