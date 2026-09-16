"""별도 제출은 보존하되 멤버 제출 지표와 분리한다."""
import unittest
from unittest.mock import patch

import test_dashboard as fixtures
from config import settings
from dashboard import overview, members
from database.sessions import list_sessions
from database.sqlite import get_connection


class SeparateSubmissionTest(fixtures.AdminWebTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", ["U33333333"]))
        with get_connection() as connection:
            connection.execute(
                """INSERT INTO retrospectives
                   (user_id, session_name, slack_channel, good_points, slack_ts, improvements, learnings, action_item)
                   VALUES ('U33333333', '6기 1회차', 'C11111111', '별도 제출 본문', '3.0', '', '', '')"""
            )

    async def test_overview_counts_regular_and_separate_submissions(self):
        data = overview._collect()
        self.assertEqual((data["submitted"], data["expected"], data["missing"]), (1, 2, 1))
        self.assertEqual(data["separate"], ["U33333333"])
        self.assertEqual(data["unassigned"], 0)
        self.assertEqual(data["teams"][0]["submitted"], 1)
        trend = {row["session_name"]: row for row in data["trend"]}
        self.assertEqual(trend["6기 1회차"]["submitters"], 1)
        self.assertEqual(trend["6기 1회차"]["separate_submitters"], 1)
        self.assertEqual(trend["5기 12회차"]["submitters"], 0)
        self.assertEqual(trend["5기 12회차"]["separate_submitters"], 1)

    async def test_schedule_uses_same_counts_and_records_remain_visible(self):
        sessions = {row["name"]: row for row in list_sessions()}
        self.assertEqual(sessions["6기 1회차"]["submissions"], 1)
        self.assertEqual(sessions["6기 1회차"]["separate_submissions"], 1)
        for path in ("/", "/schedule", "/retrospectives", "/retrospectives/3", "/members"):
            with self.subTest(path=path):
                response = await self._get(path)
                self.assertEqual(response.status, 200)
                body = await response.text()
                self.assertIn("별도 제출", body)
                self.assertIn("집계 제외", body)
        body = await (await self._get("/retrospectives/3")).text()
        self.assertIn("별도 제출 본문", body)
        with get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM retrospectives").fetchone()[0], 3)

    async def test_assigned_channel_chooser_is_counted_normally(self):
        with patch.object(settings, "SUBMISSION_DESTINATIONS", {
            **settings.SUBMISSION_DESTINATIONS, "U33333333": "C11111111",
        }):
            data = overview._collect()
            self.assertEqual((data["submitted"], data["expected"], data["missing"]), (2, 3, 1))
            self.assertEqual(data["separate"], [])
            session = next(row for row in list_sessions() if row["name"] == "6기 1회차")
            self.assertEqual(session["submissions"], 2)
            self.assertEqual(session["separate_submissions"], 0)

    async def test_unassigned_regular_user_is_not_silently_excluded(self):
        with patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", []):
            data = overview._collect()
            self.assertEqual(data["submitted"], 2)
            self.assertEqual(data["unassigned"], 1)
            self.assertEqual(data["separate"], [])

    async def test_test_only_submission_is_not_counted(self):
        with get_connection() as connection:
            connection.execute("UPDATE retrospectives SET is_test_submission=1 WHERE user_id='U33333333'")
        data = overview._collect()
        self.assertEqual(data["submitted"], 1)
        self.assertEqual(data["separate"], [])
        self.assertEqual(len(data["trend"]), 1)
        session = next(row for row in list_sessions() if row["name"] == "6기 1회차")
        self.assertEqual(session["separate_submissions"], 0)

    async def test_members_summary_excludes_separate_account_but_keeps_history(self):
        data = members._collect()
        self.assertEqual(data["counted"], 2)
        self.assertEqual(len(data["members"]), 3)
        separate = next(row for row in data["members"] if row["user_id"] == "U33333333")
        self.assertTrue(separate["current_done"])
        regular = [row for row in data["members"] if row["user_id"] != "U33333333"]
        self.assertEqual(data["average"], round(sum(row["rate"] for row in regular) / 2 * 100))
        self.assertEqual(data["at_risk"], sum(row["streak"] >= members.AT_RISK_MISSES for row in regular))
        with patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", []):
            self.assertEqual(members._collect()["counted"], 3)

    async def test_only_separate_submitters_have_zero_summary(self):
        with patch.object(settings, "SUBMISSION_DESTINATIONS", {}), patch.object(
            settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", ["U11111111", "U33333333"],
        ):
            data = members._collect()
            self.assertEqual((data["counted"], data["average"], data["at_risk"], data["never"]), (0, 0, 0, 0))
            overview_data = overview._collect()
            self.assertEqual(overview_data["submitted"], 0)
            self.assertEqual(len(overview_data["separate"]), 2)


if __name__ == "__main__":
    unittest.main()
