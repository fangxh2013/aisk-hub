import json
import ctypes
import os
import tempfile
import unittest
from unittest.mock import patch

from engine.action_context import ActionContext, ActionContextError, audit
from engine.worktree import integrate, registry

_AUDIT_SANDBOX = None
_AUDIT_SAVED = None


def setUpModule():
    """把审计出口指到临时目录，别往真实确认日志里写测试数据。

    本文件的弹窗用例只 patch 掉 osascript 那一层，confirm_human 本身是真跑的，
    而它无论接受还是拒绝都会 audit()。不隔离的话，每跑一次全量套件就往
    ~/.aisk-runtime/logs/dialog_audit.jsonl 追加两条「已批准推送」，和真人点过的
    记录长得一模一样。那份日志是事后追查「谁批准过推主干」的唯一凭据，掺不得。
    """
    global _AUDIT_SANDBOX, _AUDIT_SAVED
    _AUDIT_SAVED = os.environ.get("AISKHUB_RUNTIME_ROOT")
    _AUDIT_SANDBOX = tempfile.TemporaryDirectory(prefix="aisk-audit-")
    os.environ["AISKHUB_RUNTIME_ROOT"] = _AUDIT_SANDBOX.name


def tearDownModule():
    if _AUDIT_SAVED is None:
        os.environ.pop("AISKHUB_RUNTIME_ROOT", None)
    else:
        os.environ["AISKHUB_RUNTIME_ROOT"] = _AUDIT_SAVED
    _AUDIT_SANDBOX.cleanup()


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

    def test_windows_task_dialog_names_the_action_and_defaults_to_cancel(self):
        seen = {}

        class TaskDialog:
            def __call__(self, config_ptr, selected_ptr, _radio, _verified):
                config = ctypes.cast(config_ptr, ctypes.POINTER(registry._TaskDialogConfig)).contents
                seen["title"] = config.pszWindowTitle
                seen["main"] = config.pszMainInstruction
                seen["content"] = config.pszContent
                seen["footer"] = config.pszFooter
                seen["default"] = config.nDefaultButton
                seen["buttons"] = [config.pButtons[i].pszButtonText for i in range(config.cButtons)]
                ctypes.cast(selected_ptr, ctypes.POINTER(ctypes.c_int)).contents.value = registry._WIN_CONFIRM_BUTTON
                return 0

        class Comctl32:
            TaskDialogIndirect = TaskDialog()

        ctx = ActionContext("codex", "git推送", "T042", "session-42", risk_level="HIGH")
        with patch.object(registry.ctypes, "WinDLL", return_value=Comctl32(), create=True):
            accepted, detail = registry._confirm_windows_task_dialog("be: fxh → origin/fxh，3 个提交", "推送", ctx)

        self.assertTrue(accepted)
        self.assertEqual(detail, "windows-task-dialog")
        self.assertEqual(seen["title"], "git推送-codex｜T042")
        self.assertEqual(seen["main"], "确认推送")
        self.assertEqual(seen["buttons"], ["确认推送", "取消"])
        self.assertEqual(seen["default"], registry._WIN_CANCEL_BUTTON)
        self.assertIn("3 个提交", seen["content"])
        self.assertIn("风险级别：HIGH", seen["footer"])

    def test_windows_without_interactive_desktop_denies_without_terminal_fallback(self):
        ctx = ActionContext("codex", "git合并", "T042", "session-42")
        with patch.object(registry, "IS_WIN", True), \
                patch.object(registry, "_windows_desktop_available", return_value=False), \
                patch.object(registry, "_confirm_windows_task_dialog") as dialog, \
                patch.object(registry, "audit") as audit_log, \
                patch.object(registry.sys.stdin, "isatty", return_value=True):
            self.assertFalse(registry.confirm_human("候选合并", "落地", context=ctx))

        dialog.assert_not_called()
        audit_log.assert_called_once_with(ctx, "denied", detail="no-interactive-desktop")
