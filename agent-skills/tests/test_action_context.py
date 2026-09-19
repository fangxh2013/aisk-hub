import json

import unittest
from unittest.mock import patch

from engine.action_context import ActionContext, ActionContextError, audit
from engine.worktree import integrate, registry


class ActionContextTests(unittest.TestCase):
    def test_title_contains_action_tool_and_task(self):
        ctx = ActionContext("codex", "git提交", "T042", "s-1")
        self.assertEqual(ctx.title, "git提交-codex｜T042")


    def test_workbuddy_repository_title_is_explicit_without_changing_legacy_title(self):
        ctx = ActionContext("workbuddy", "git推送", "系统", "s-1", repository="aisk-hub")
        self.assertEqual(ctx.title, "git推送-workbuddy | 系统 | aisk-hub")
        self.assertEqual(ActionContext("codex", "git提交", "T042").title, "git提交-codex｜T042")

    def test_workbuddy_integrate_context_matches_standard_push_dialog(self):
        from types import SimpleNamespace
        args = SimpleNamespace(tool="workbuddy", session="s-1")
        with patch.object(integrate.git, "out", return_value="git@github.com:fangxh2013/aisk-private.git"):
            repo = integrate._repository_label_for_workbuddy(args, "/tmp/worktree")
        ctx = integrate.action_context(args, "git推送", "T003", "摘要", repository=repo)
        self.assertEqual(repo, "aisk-private")
        self.assertEqual(ctx.title, "git推送-workbuddy | T003 | aisk-private")
        self.assertEqual(integrate._push_expect(args), "确认推送")
        self.assertEqual(integrate._push_expect(SimpleNamespace(tool="codex", session="s-1")), "推送")

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ActionContextError):
            ActionContext("unknown", "git提交", "T001")


    def test_audit_does_not_need_private_payload(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            old = os.environ.get("AISKHUB_RUNTIME_ROOT")
            os.environ["AISKHUB_RUNTIME_ROOT"] = directory
            try:
                ctx = ActionContext("claude", "git推送", "T009", "session-9", summary="只记录摘要")
                audit(ctx, "denied")
                record = json.loads((__import__("pathlib").Path(directory) / "logs" / "dialog_audit.jsonl").read_text().splitlines()[0])
                self.assertEqual(record["title"], "git推送-claude｜T009")
                self.assertNotIn("password", record)
            finally:
                if old is None:
                    os.environ.pop("AISKHUB_RUNTIME_ROOT", None)
                else:
                    os.environ["AISKHUB_RUNTIME_ROOT"] = old

    @patch("engine.worktree.registry.Path.exists", return_value=True)
    @patch("engine.worktree.registry.subprocess.run")
    def test_workbuddy_native_dialog_uses_standard_push_label_and_repository_title(self, run, _exists):
        run.return_value.returncode = 0
        run.return_value.stdout = "button returned:确认推送"
        run.return_value.stderr = ""
        ctx = ActionContext("workbuddy", "git推送", "系统", "session-42", repository="aisk-hub")
        self.assertTrue(registry.confirm_human(
            "即将推送公开远端 aisk-hub 的 master。", "确认推送", context=ctx))
        script = run.call_args.args[0][2]
        self.assertIn("git推送-workbuddy | 系统 | aisk-hub", script)
        self.assertIn("确认推送", script)

    @patch("engine.worktree.registry.Path.exists", return_value=True)
    @patch("engine.worktree.registry.subprocess.run")
    def test_native_dialog_receives_tool_title(self, run, _exists):
        run.return_value.returncode = 0
        run.return_value.stdout = "button returned:推送"
        run.return_value.stderr = ""
        ctx = ActionContext("codex", "git推送", "T042", "session-42")
        self.assertTrue(registry.confirm_human("推送候选", "推送", context=ctx))
        script = run.call_args.args[0][2]
        self.assertIn("git推送-codex｜T042", script)
