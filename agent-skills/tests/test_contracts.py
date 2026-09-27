import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine import contracts, mcp_contract
from engine.skill_router import SkillRouter

SKILLS = Path(__file__).resolve().parents[1] / "skills"


class ContractTests(unittest.TestCase):
    def test_skill_inventory_and_alias_contract(self):
        result = contracts.validate_skill_migration(".")
        self.assertEqual(result["target_count"], 9)
        self.assertGreaterEqual(result["alias_count"], 20)
        self.assertEqual(contracts.resolve_skill_alias(".", "dev-code"), "backend-engineering")
        self.assertEqual(contracts.resolve_skill_alias(".", "k3s-deployment"), "ops-workbench")

    def test_coordination_contracts_are_complete(self):
        result = contracts.validate_coordination_specs(".")
        self.assertGreaterEqual(result["event_count"], 13)
        self.assertGreaterEqual(result["transition_count"], 14)

    def test_mcp_offline_semantics_never_fabricate_live_data(self):
        unavailable = json.loads(mcp_contract.offline_database_unavailable())
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["data"], None)
        with self.assertRaises(contracts.ContractError):
            contracts.validate_mcp_response({
                "ok": True,
                "source": "database-broker",
                "data_availability": "offline_unavailable",
                "as_of": "2026-09-20T00:00:00Z",
                "is_live": False,
                "redacted": True,
                "data": {"rows": 1},
            })


class SkillEntryFormatTests(unittest.TestCase):
    def test_public_skills_follow_the_documented_short_entry_format(self):
        self.assertEqual(SkillRouter().check(SKILLS), [])

    def test_skill_check_names_each_broken_rule(self):
        router = SkillRouter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in router.canonical_names():
                shutil.copytree(SKILLS / name, root / name)

            def rewrite(name, old, new):
                path = root / name / "SKILL.md"
                text = path.read_text(encoding="utf-8")
                self.assertIn(old, text)
                path.write_text(text.replace(old, new, 1), encoding="utf-8")

            rewrite("security", "勿用：", "另见：")
            rewrite("web-engineering", "## 按需参考", "## 参考")
            rewrite("db-workbench", "references/database-workflow.md)", "references/missing.md)")
            (root / "ops-workbench" / "references" / "orphan.md").write_text("# 没人索引\n", encoding="utf-8")
            errors = router.check(root)
        self.assertEqual(sorted(errors), sorted([
            "security: description 缺少「勿用：」",
            "web-engineering: 正文缺少「## 按需参考」一节",
            "db-workbench: 按需参考链接的 references/missing.md 不存在",
            "db-workbench: references/database-workflow.md 没有在「## 按需参考」里列出",
            "ops-workbench: references/orphan.md 没有在「## 按需参考」里列出",
        ]))


if __name__ == "__main__":
    unittest.main()
