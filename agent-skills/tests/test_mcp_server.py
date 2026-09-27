"""MCP 服务端协议与工具回归；全部在临时目录里进行，不碰真实档案、登记簿或数据库。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine import mcp_server, profile  # noqa: E402
from engine.coordination_store import CoordinationStore  # noqa: E402
from engine.worktree.config import build_config  # noqa: E402
from engine.worktree.registry import Registry, now_iso  # noqa: E402


def call(name, arguments, msg_id=1):
    return mcp_server.process_request({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})


def payload(response):
    """tools/call 的结果是一段 JSON 文本（mcp_contract 的结构化响应）。"""
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    def test_tools_list_exposes_six_flat_tools(self):
        response = mcp_server.process_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(names, ["aisk_task_inspect", "aisk_fact_service", "aisk_fact_entry",
                                 "aisk_env_summary", "aisk_repo", "aisk_db_query"])
        for tool in response["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_arguments_are_rejected_before_any_handler_runs(self):
        response = call("aisk_task_inspect", {"no_such_arg": True})
        self.assertEqual(response["error"]["code"], -32602)

    def test_notifications_get_no_response(self):
        self.assertIsNone(mcp_server.process_request({"jsonrpc": "2.0", "method": "tools/list"}))

    def test_db_query_refuses_writes_before_touching_profiles_or_credentials(self):
        with patch.object(mcp_server.secrets, "get", side_effect=AssertionError("不该取凭据")):
            is_error, body = payload(call("aisk_db_query", {"sql": "DELETE FROM orders"}))
        self.assertTrue(is_error)
        self.assertFalse(body["ok"])
        self.assertIn("只读安全策略拒绝", body["error_message"])


class TaskInspectTests(unittest.TestCase):
    """aisk_task_inspect 读的是 aisk task 真正维护的登记簿，而不是一个没人写的 state.db。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aisk-mcp-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        profiles = root / "profiles"
        profiles.mkdir()
        (profiles / "demo.yaml").write_text("\n".join([
            "project: demo",
            "repos:",
            f"  backend: {root / 'backend'}",
            "worktrees:",
            f"  data_root: {root / 'runtime'}",
            "  integration_branch: integration",
            "  protected:",
            "    - integration",
            "  repos:",
            "    backend:",
            "      profile_repo: backend",
            "      trunk: main",
            "      push_branch: integration",
            "      promote: push-only",
        ]) + "\n", encoding="utf-8")
        (profiles / "plain.yaml").write_text(f"project: plain\nrepos:\n  main: {root / 'plain'}\n", encoding="utf-8")
        for patcher in (patch.object(profile, "PROFILE_DIR", profiles), patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # 在 patch.dict 的快照里改环境，测试结束自动还原
        os.environ.pop("AISK_STATE_DB", None)
        os.environ.pop("AISK_PROFILE", None)
        self.cfg = build_config(profile.load(profiles / "demo.yaml"))
        task_dir = self.cfg.tasks_dir / "T001-demo-task"
        Registry(self.cfg).save({
            "id": "T001", "slug": "demo-task", "name": "T001-demo-task", "title": "演示任务",
            "os": self.cfg.os, "dir": str(task_dir), "state": "active", "next_step": "补测试",
            "owner": {"tool": "codex", "sessions": ["session-123456789"], "heartbeat_at": now_iso()},
            "repos": {"backend": {"branch": "ai/T001-demo-task", "path": str(task_dir / "backend"),
                                  "ready_sha": None, "landed_sha": None}},
        })

    def test_lists_tasks_from_the_registry(self):
        is_error, body = payload(call("aisk_task_inspect", {"profile": "demo"}))
        self.assertFalse(is_error, body)
        self.assertEqual(body["source"], "aisk-task-registry")
        self.assertTrue(body["is_live"])
        self.assertIn("[T001] 演示任务 | active | codex:session-…", body["data"])
        self.assertIn("持有中", body["data"])

    def test_shows_one_task_by_id_or_full_name(self):
        for ref in ("T001", "T001-demo-task"):
            with self.subTest(ref=ref):
                is_error, body = payload(call("aisk_task_inspect", {"profile": "demo", "task_id": ref}))
                self.assertFalse(is_error, body)
                self.assertIn("task_id: T001", body["data"])
                self.assertIn("state: active", body["data"])
                self.assertIn("repo.backend: branch=ai/T001-demo-task", body["data"])

    def test_unknown_task_and_profile_without_worktrees_are_tool_errors(self):
        for arguments in ({"profile": "demo", "task_id": "T999"}, {"profile": "plain"}):
            with self.subTest(arguments=arguments):
                is_error, body = payload(call("aisk_task_inspect", arguments))
                self.assertTrue(is_error)
                self.assertEqual(body["error_code"], "TOOL_REJECTED")

    def test_explicit_state_db_still_reads_the_contract_store(self):
        db_path = Path(self._tmp.name) / "state.db"
        store = CoordinationStore(db_path)
        store.create("T900", {"tool": "codex", "session_id": "s-1"}, {"title": "contract"})
        store.close()
        with patch.dict(os.environ, {"AISK_STATE_DB": str(db_path)}):
            is_error, body = payload(call("aisk_task_inspect", {"task_id": "T900"}))
        self.assertFalse(is_error, body)
        self.assertEqual(body["source"], "sqlite-coordination-store")
        self.assertIn("status: Unassigned", body["data"])


if __name__ == "__main__":
    unittest.main()
