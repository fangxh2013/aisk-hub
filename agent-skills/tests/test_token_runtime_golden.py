"""Token Runtime 黄金任务与审计边界的离线回归。

docs/TOKEN-RUNTIME-BENCHMARK.md 与 reports/token_runtime_baseline.json 描述的每条行为都在这里
有对应断言；基线报告里的摘要也在这里复算，夹具或策略一改，报告就必须跟着重新生成。
不访问网络、数据库或任何客户端。
"""

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine import cli as aisk_cli
from engine import miniyaml
from engine.token import budget, digest, ledger, planner, policy, telemetry

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "agent-skills/tests/token_runtime_golden_tasks.json"
REPORT = "reports/token_runtime_baseline.json"
TOOLS = ("codex", "claude", "antigravity", "workbuddy")
# 基线报告的 policy 摘要：按下面的顺序把文件字节首尾相接后取 sha256，
# 等价于 `cat <这些文件> | sha256sum`。
POLICY_FILES = ("spec/token-efficiency.yaml",) + tuple(
    f"agent-skills/adapters/{tool}/token-policy.yaml" for tool in TOOLS)
SHA = hashlib.sha256(b"rules").hexdigest()
LOADED_AT = "2026-09-27T00:00:00Z"


def sha256_of(*relative_paths):
    h = hashlib.sha256()
    for rel in relative_paths:
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()


def golden_cases():
    return json.loads((ROOT / FIXTURE).read_text(encoding="utf-8"))["cases"]


def load_report():
    return json.loads((ROOT / REPORT).read_text(encoding="utf-8"))


class GoldenTaskTests(unittest.TestCase):
    def test_six_golden_tasks_route_to_the_expected_mode(self):
        cases = golden_cases()
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(case=case["id"]):
                result = planner.Planner().plan(
                    {"title": case["task"]},
                    complexity=case["complexity"],
                    required_sources=case.get("required_sources", ()),
                    available_sources=case.get("available_sources"),
                    conflicts=case.get("conflicts", ()),
                )
                expect = case["expect"]
                self.assertEqual(result["mode"], expect["mode"])
                self.assertEqual([t["rule"] for t in result["triggers"]], expect["rules"])
                self.assertEqual(result["fallback_reason"] is not None, expect["fail_to_full"])
                self.assertEqual(result["budget"]["requested"], budget.DEFAULT_BUDGETS[expect["mode"]])
                self.assertTrue(result["budget"]["hard_constraints_preserved"])

    def test_every_high_risk_category_forces_deep_even_when_marked_simple(self):
        samples = {
            "database": "改 mysql 表结构",
            "permission": "调整 RBAC 权限",
            "privacy": "处理用户隐私数据",
            "production": "排查线上故障",
            "git_push": "推送到远端",
            "git_merge": "合并到主干",
        }
        for rule, text in samples.items():
            with self.subTest(rule=rule):
                result = planner.plan({"title": text}, complexity="simple")
                self.assertEqual(result["mode"], "deep")
                self.assertIn(rule, [t["rule"] for t in result["triggers"]])

    def test_readme_examples_run_through_the_cli(self):
        for argv in (["contract", "token-runtime", "--task", "git push master", "--complexity", "simple"],
                     ["contract", "token-runtime", "--task", "数据库迁移", "--complexity", "complex"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(aisk_cli.main(argv), 0)
                self.assertEqual(json.loads(out.getvalue())["mode"], "deep")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(aisk_cli.main(["contract", "token-runtime", "--task", "整理一下"]), 0)
        self.assertEqual(json.loads(out.getvalue())["mode"], "emergency", "没给复杂度按不确定处理，回退 emergency")


class BudgetProtectionTests(unittest.TestCase):
    def test_small_budget_trims_only_optional_context(self):
        result = planner.Planner().plan(
            {"title": "修正 README 里的错别字"}, complexity="simple", budget=10,
            context=[{"id": "optional-notes", "tokens": 500},
                     {"id": "security-rules", "tokens": 300, "protected": True}],
            hard_constraints=["privacy: 不记录凭据", "verification: 落地前必须跑门禁"],
        )
        plan_budget = result["budget"]
        self.assertEqual(result["mode"], "lite")
        self.assertEqual(set(result["protected_sources"]), {"security-rules", "constraint-0", "constraint-1"})
        included = {item["source"] for item in plan_budget["included_sources"]}
        self.assertLessEqual(set(result["protected_sources"]), included)
        self.assertEqual([item["source"] for item in plan_budget["dropped_sources"]], ["optional-notes"])
        self.assertTrue(plan_budget["hard_constraints_preserved"])
        self.assertEqual(plan_budget["effective"], plan_budget["protected_tokens"], "预算不够时按保护规则的实际大小放宽")

    def test_fail_to_full_widens_the_budget_and_keeps_protected_rules(self):
        result = planner.Planner().plan(
            {"title": "整理一下这个模块"}, complexity="uncertain", budget=10,
            context=[{"id": "optional-notes", "tokens": 500}], hard_constraints=["security: 保留授权门禁"])
        self.assertEqual(result["mode"], "emergency")
        self.assertEqual(result["budget"]["requested"], budget.DEFAULT_BUDGETS["emergency"])
        self.assertEqual(result["budget"]["dropped_sources"], [])
        self.assertTrue(result["budget"]["hard_constraints_preserved"])

    def test_allocate_protects_named_sources_and_rejects_negative_budgets(self):
        result = budget.allocate([{"id": "verification", "tokens": 50}, {"id": "extra", "tokens": 50}], 10,
                                 protected_sources=["verification"])
        self.assertEqual([item["source"] for item in result["included"]], ["verification"])
        self.assertEqual([item["source"] for item in result["dropped"]], ["extra"])
        with self.assertRaises(budget.BudgetError):
            budget.allocate([], -1)
        for budgets in ({"turbo": 1}, {"lite": -1}):
            with self.assertRaises(ValueError):
                planner.Planner(budgets)


class AuditBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = ledger.AuditLedger(self.tmp / "audit.jsonl")

    def record(self, **overrides):
        fields = dict(session_id="sess-1", token_count=42, mode="deep", trigger="git_push",
                      fallback="none", result="planned", source_digest=SHA,
                      metadata={"tool": "codex", "task_id": "T006", "path": "spec/token-efficiency.yaml"})
        fields.update(overrides)
        return self.ledger.make_record(**fields)

    def test_ledger_keeps_metadata_only(self):
        stored = self.ledger.append(self.record())
        self.assertEqual(set(stored), {"schema_version", "at", "session_id", "metadata", "token_count",
                                       "mode", "trigger", "fallback", "result", "source_digest"})
        self.assertNotIn("git push origin master", (self.tmp / "audit.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(self.ledger.read(), [stored])

    def test_ledger_rejects_bodies_credentials_and_local_paths(self):
        for metadata in ({"prompt": "x"}, {"content": "x"}, {"env": "DEV"}, {"password": "x"},
                         {"note": "未登记的字段"}, {"path": str(self.tmp / "rules.yaml")},
                         {"path": "../outside.yaml"}, {"source_digest": "abc"}, {"version": 1.5}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ledger.LedgerError):
                    self.record(metadata=metadata)
        with self.assertRaises(ledger.LedgerError):
            ledger.AuditLedger.append_validate({**self.record(), "body": "任务正文"})
        with self.assertRaises(ledger.LedgerError):
            self.ledger.append(self.record(source_digest="not-a-sha256"))
        self.assertFalse((self.tmp / "audit.jsonl").exists(), "被拒绝的记录一行都不能落盘")

    def test_digest_records_never_keep_content_or_absolute_paths(self):
        rules = self.tmp / "rules.yaml"
        rules.write_text("version: 1\n", encoding="utf-8")
        content, record = digest.load_rule_source(rules, "PUBLIC", loaded_at=LOADED_AT)
        self.assertEqual(content, "version: 1\n")
        self.assertEqual(set(record), {"path", "sha256", "digest", "loaded_at", "classification"})
        self.assertEqual(record["path"], "rules.yaml", "绝对路径只留文件名，不泄露本机目录结构")
        self.assertEqual(record["sha256"], hashlib.sha256(b"version: 1\n").hexdigest())
        relative = digest.source_record("spec/x.yaml", SHA, "INTERNAL_TEMPLATE", loaded_at=LOADED_AT)
        self.assertEqual(relative["path"], "spec/x.yaml")
        self.assertEqual(digest.version_digest({"rules": record}), digest.version_digest({"rules": dict(record)}))
        self.assertNotEqual(digest.version_digest({"rules": record}), digest.version_digest({"rules": relative}))
        for bad in (lambda: digest.source_record("a", SHA, "TOP_SECRET"),
                    lambda: digest.source_record("a", SHA.upper(), "PUBLIC"),
                    lambda: digest.source_record("a", SHA, "PUBLIC", loaded_at="yesterday"),
                    lambda: digest.version_digest({"rules": {"path": "a"}})):
            with self.assertRaises(digest.DigestError):
                bad()

    def test_telemetry_aggregates_counts_without_content(self):
        tracker = telemetry.Telemetry(self.ledger)
        tracker.record(session_id="s1", token_count=10, mode="lite", trigger="none", fallback="none",
                       result="planned", source_digest=SHA)
        tracker.record(session_id="s1", token_count=5, mode="emergency", trigger="complexity_uncertain",
                       fallback="emergency", result="planned", source_digest=SHA)
        stats = tracker.stats("s1")
        self.assertEqual((stats["event_count"], stats["token_count"]), (2, 15))
        self.assertEqual(stats["by_mode"], {"emergency": 1, "lite": 1})
        self.assertEqual(stats["source_digests"], [SHA])
        with self.assertRaises(telemetry.TelemetryError):
            tracker.record(session_id="s1", token_count=1, mode="lite", trigger="x", fallback="x",
                           result="x", source_digest=SHA, metadata={"text": "任务正文"})


class ToolPolicyTests(unittest.TestCase):
    def test_four_tool_policies_declare_the_shared_contract(self):
        spec = miniyaml.load_file(ROOT / "spec/token-efficiency.yaml")
        cap = int(spec["budgets"]["max_task_context_tokens"])
        for tool in TOOLS:
            with self.subTest(tool=tool):
                data = miniyaml.load_file(ROOT / f"agent-skills/adapters/{tool}/token-policy.yaml")
                self.assertEqual((data["version"], data["tool"]), (1, tool))
                self.assertEqual(data["contract_source"], "public-token-runtime")
                self.assertIs(data["do_not_duplicate_core_policy"], True)
                self.assertEqual(data["verification"], {"status": "offline_contract", "runtime_status": "dry-run",
                                                        "real_client_validation": "not_integrated"})
                self.assertEqual(data["modes"], {"supported": list(policy.MODES), "default": "standard"})
                self.assertLessEqual(data["budget"]["default_tokens"], data["budget"]["max_tokens"])
                self.assertLessEqual(data["budget"]["max_tokens"], cap)
                self.assertEqual(data["fail_to_full"], {"enabled": True, "on_budget_error": "emergency",
                                                        "preserve_safety_privacy_verification": True,
                                                        "never_bypass_authorization": True})
                dialog = data["dialog"]
                self.assertEqual(dialog["high_risk_title_template"], "{action}-{tool}｜{task_id}")
                self.assertEqual(dialog["example"], f"git推送-{tool}｜T006")
                self.assertEqual(dialog["missing_identity"], "fail_closed")
                self.assertIn("explicit_session_argument", data["session"]["sources"])

    def test_standard_mode_stays_within_the_legacy_static_contract(self):
        spec = miniyaml.load_file(ROOT / "spec/token-efficiency.yaml")
        self.assertEqual(set(budget.DEFAULT_BUDGETS), set(policy.MODES))
        self.assertLessEqual(budget.DEFAULT_BUDGETS["standard"], int(spec["budgets"]["max_task_context_tokens"]))
        ordered = [budget.DEFAULT_BUDGETS[mode] for mode in policy.MODES]
        self.assertEqual(ordered, sorted(ordered), "lite < standard < deep < emergency")


class BaselineReportTests(unittest.TestCase):
    def test_baseline_report_matches_the_fixture_and_policies(self):
        report = load_report()
        self.assertEqual(report["golden_tasks"], {"fixture": FIXTURE, "cases": len(golden_cases())})
        expected = {"fixture": sha256_of(FIXTURE), "policy": sha256_of(*POLICY_FILES)}
        self.assertEqual({key: report["digests"][key] for key in expected}, expected,
                         "夹具或策略改了：把这里算出的新摘要写回 reports/token_runtime_baseline.json")
        self.assertEqual(report["tool_policies"],
                         {tool: f"agent-skills/adapters/{tool}/token-policy.yaml" for tool in TOOLS})
        self.assertTrue((ROOT / report["token_policy"]["legacy_static_contract"]).is_file())

    def test_baseline_report_never_claims_a_real_runtime_result(self):
        report = load_report()
        self.assertEqual(report["validation_scope"], "offline_contract")
        self.assertIsNone(report["real_runtime_score"])
        self.assertIs(report["real_runtime_certified"], False)
        self.assertEqual(report["runtime_status"], "offline-only")
        self.assertEqual(report["real_runtime_validation"], {tool: "not_run" for tool in TOOLS})
        token_policy = report["token_policy"]
        self.assertEqual(token_policy["uncertain_evidence_states"], ["uncertain", "conflict", "missing"])
        self.assertEqual((token_policy["default_mode"], token_policy["high_risk_mode"],
                          token_policy["evidence_fallback_mode"]), ("standard", "deep", "emergency"))
        self.assertEqual(token_policy["protected_rules"], ["security", "privacy", "verification"])


if __name__ == "__main__":
    unittest.main()
