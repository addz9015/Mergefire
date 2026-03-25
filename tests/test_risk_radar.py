from __future__ import annotations

from datetime import datetime
import unittest

from agents.risk_radar import RiskRadarAgent, _get_test_delta, _is_test_file


class TestRiskRadarScoring(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = RiskRadarAgent(
            github_client=object(),
            slack_client=object(),
            discord_client=object(),
            high_threshold=67,
            medium_threshold=34,
        )

    def test_high_risk_assessment_includes_friday_flag(self) -> None:
        changed_files = [
            {"filename": "auth/service.py", "status": "modified"},
            {"filename": "payments/gateway.py", "status": "modified"},
            {"filename": "migrations/001_add_column.sql", "status": "added"},
            {"filename": "app/api.py", "status": "modified"},
            {"filename": "tests/test_api.py", "status": "removed"},
            {"filename": "tests/test_auth.py", "status": "removed"},
        ]

        now = datetime(2026, 3, 27, 16, 0, 0)  # Friday 4pm
        assessment = self.agent._assess_risk(changed_files, now=now)

        self.assertEqual(assessment.band, "high")
        self.assertGreaterEqual(assessment.score, 67)
        self.assertTrue(assessment.friday_after_3pm)
        self.assertEqual(assessment.test_delta, "tests_deleted")
        self.assertGreaterEqual(len(assessment.sensitive_files), 3)

    def test_low_risk_assessment_for_small_tested_change(self) -> None:
        changed_files = [
            {"filename": "src/helpers.py", "status": "modified"},
            {"filename": "tests/test_helpers.py", "status": "added"},
        ]

        now = datetime(2026, 3, 24, 10, 0, 0)  # Tuesday morning
        assessment = self.agent._assess_risk(changed_files, now=now)

        self.assertEqual(assessment.band, "low")
        self.assertLess(assessment.score, 34)
        self.assertFalse(assessment.friday_after_3pm)
        self.assertEqual(assessment.test_delta, "tests_added")


class TestRiskRadarHelpers(unittest.TestCase):
    def test_is_test_file_patterns(self) -> None:
        self.assertTrue(_is_test_file("tests/test_service.py"))
        self.assertTrue(_is_test_file("src/module/user_test.py"))
        self.assertTrue(_is_test_file("web/app.spec.ts"))
        self.assertFalse(_is_test_file("src/service.py"))

    def test_get_test_delta_states(self) -> None:
        tests_added = _get_test_delta([
            {"filename": "tests/test_a.py", "status": "added"},
        ])
        tests_deleted = _get_test_delta([
            {"filename": "tests/test_a.py", "status": "removed"},
            {"filename": "tests/test_b.py", "status": "removed"},
        ])
        tests_unchanged = _get_test_delta([
            {"filename": "src/app.py", "status": "modified"},
        ])

        self.assertEqual(tests_added, "tests_added")
        self.assertEqual(tests_deleted, "tests_deleted")
        self.assertEqual(tests_unchanged, "tests_unchanged")


if __name__ == "__main__":
    unittest.main()
