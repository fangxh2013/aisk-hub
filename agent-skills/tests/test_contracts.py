import json
import unittest

from engine import contracts, mcp_contract


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


if __name__ == "__main__":
    unittest.main()
