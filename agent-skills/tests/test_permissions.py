# -*- coding: utf-8 -*-
"""端上权限白名单的不变量：只读动词免确认，写动词永远不进 allow。

2026-09-21（T009）把 `git merge dev` 从宿主级 hard-deny 移出，改由内核守卫弹
`git合并-<工具>` 确认（覆盖见 test_worktree.py 的
`test_merge_protected_branch_requires_tool_named_confirmation`）。移出 deny 之后，
「写动词不许进 allow」这半条不变量必须单独钉住：守卫弹窗和白名单本来是各自独立的
一道，下次谁顺手把合并动词写进 allow，两道会一起失效。
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from engine import permissions

# 不可逆或必须由人拍板的动词：任何端的 allow 里都不许出现。
WRITE_VERBS = ("db exec", "secret set", "secret rm", "git push origin main", "git merge dev")
# 这些必须继续留在 hard-deny —— 它们没有「弹窗确认」这条退路。
HARD_DENY_VERBS = ("aisk db exec", "aisk secret set", "aisk secret rm", "git push origin main")


class WriteVerbsNeverInAllow(unittest.TestCase):
    def _generate(self, apply_fn, relative):
        """在临时 HOME 下真的生成一遍配置，读回落盘结果——不是读常量表。"""
        with TemporaryDirectory(prefix="aisk-perm-") as home:
            with patch.dict(os.environ, {"HOME": home}):
                apply_fn()
                path = Path(home) / relative
                self.assertTrue(path.is_file(), f"没有生成 {relative}")
                return json.loads(path.read_text(encoding="utf-8"))["permissions"]

    def _assert_no_write_verb(self, allow, tool):
        for verb in WRITE_VERBS:
            self.assertFalse(any(verb in entry for entry in allow),
                             f"{tool}: 写动词 {verb} 混进了 allow")

    def test_claude_allow_only_has_readonly_verbs(self):
        perms = self._generate(permissions.apply_claude, ".claude/settings.json")
        self._assert_no_write_verb(perms["allow"], "claude")
        for verb in permissions.READONLY_VERBS:
            self.assertIn(f"Bash({verb})", perms["allow"])

    def test_workbuddy_allow_only_has_readonly_verbs(self):
        perms = self._generate(permissions.apply_workbuddy, ".codebuddy/settings.json")
        self._assert_no_write_verb(perms["allow"], "workbuddy")
        for verb in permissions.READONLY_VERBS:
            self.assertIn(f"Bash({verb})", perms["allow"])

    def test_cursor_allow_only_has_readonly_verbs(self):
        perms = self._generate(permissions.apply_cursor, ".cursor/cli-config.json")
        self._assert_no_write_verb(perms["allow"], "cursor")

    def test_hard_deny_still_covers_verbs_without_a_confirm_path(self):
        for verb in HARD_DENY_VERBS:
            self.assertTrue(any(verb in entry for entry in permissions.DENY_VERBS),
                            f"{verb} 不在 hard-deny 里")

    def test_merge_dev_relies_on_the_guard_not_on_a_host_level_deny(self):
        """T009 的策略要两边都如实：不在 hard-deny，也不在任何白名单里。"""
        self.assertFalse(any("git merge" in entry for entry in permissions.DENY_VERBS),
                         "合并动词若又写回 hard-deny，守卫弹窗就永远走不到")
        self.assertFalse(any("git merge" in verb for verb in permissions.READONLY_VERBS))


if __name__ == "__main__":
    unittest.main()
