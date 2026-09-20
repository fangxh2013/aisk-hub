import unittest

from engine import contracts, token_efficiency


class TokenEfficiencyTests(unittest.TestCase):
    def test_public_budget_audit_passes(self):
        result = token_efficiency.audit(".")
        contracts.validate_token_audit(result)
        self.assertTrue(result["passed"], result)
        self.assertLessEqual(result["estimated_task_context_tokens"], 4500)
        self.assertLessEqual(result["duplicate_line_ratio"], 0.20)

    def test_estimator_is_deterministic_and_conservative(self):
        self.assertEqual(token_efficiency.estimate_tokens("abcd", 4), 1)
        self.assertEqual(token_efficiency.estimate_tokens("abcde", 4), 2)
        with self.assertRaises(token_efficiency.TokenEfficiencyError):
            token_efficiency.estimate_tokens("text", 0)


if __name__ == "__main__":
    unittest.main()
