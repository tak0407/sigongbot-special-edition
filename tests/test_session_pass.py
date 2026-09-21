"""회차 회고 패스: 사용 제한, 확인 모달, 공개 안내, 리마인더 제외."""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import settings
from constants import MAX_PASS_COUNT
from database import passes
from database.sqlite import get_connection, initialize_database
from database import sessions as sessions_db
from slack.events import session_pass as pass_event

KST = ZoneInfo("Asia/Seoul")
COHORT_SESSIONS = ["9기 1회차", "9기 2회차", "9기 3회차"]


class PassTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(
            patch.object(
                settings, "SUBMISSION_DESTINATIONS", {"U11111111": "C11111111"}
            )
        )
        initialize_database()
        self.addCleanup(sessions_db.invalidate)

        # 기수 경계를 확인하려고 앞 기수 회차를 하나 끼워 둔다.
        with get_connection() as connection:
            connection.execute("DELETE FROM sessions")
            base = datetime.datetime(2099, 1, 1, 5, 0, tzinfo=KST)
            connection.execute(
                "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
                ("8기 12회차", base.isoformat()),
            )
            for index, name in enumerate(COHORT_SESSIONS, start=1):
                connection.execute(
                    "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
                    (name, (base + datetime.timedelta(days=7 * index)).isoformat()),
                )
        sessions_db.invalidate()

    def use(self, session_name: str, user_id: str = "U11111111") -> int:
        return passes.record_pass(
            user_id=user_id, session_name=session_name, team_channel="C11111111"
        )


class PassEligibilityTest(PassTestCase):
    async def test_allows_the_first_pass_and_reports_remaining(self):
        result = passes.check_eligibility("U11111111", "9기 1회차")
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, MAX_PASS_COUNT)

    async def test_blocks_a_consecutive_pass(self):
        self.use("9기 1회차")
        result = passes.check_eligibility("U11111111", "9기 2회차")
        self.assertFalse(result.allowed)
        self.assertIn("직전 회차", result.reason)
        # 연속 사용만 막을 뿐 횟수는 남아 있다.
        self.assertEqual(result.remaining, MAX_PASS_COUNT - 1)

    async def test_allows_a_pass_again_after_skipping_one_session(self):
        self.use("9기 1회차")
        result = passes.check_eligibility("U11111111", "9기 3회차")
        self.assertTrue(result.allowed)

    async def test_blocks_when_the_cohort_quota_is_used_up(self):
        self.use("9기 1회차")
        self.use("9기 3회차")
        with get_connection() as connection:
            connection.execute(
                "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
                ("9기 4회차", datetime.datetime(2099, 3, 1, 5, 0, tzinfo=KST).isoformat()),
            )
        sessions_db.invalidate()
        result = passes.check_eligibility("U11111111", "9기 4회차")
        self.assertFalse(result.allowed)
        self.assertEqual(result.remaining, 0)
        self.assertIn(str(MAX_PASS_COUNT), result.reason)

    async def test_quota_is_counted_per_cohort(self):
        """지난 기수에서 쓴 패스가 이번 기수 횟수를 깎으면 안 된다."""
        self.use("8기 12회차")
        result = passes.check_eligibility("U11111111", "9기 1회차")
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, MAX_PASS_COUNT)

    async def test_previous_session_stops_at_the_cohort_boundary(self):
        """기수의 첫 회차는 직전 회차가 없다고 봐야 한다."""
        self.assertIsNone(passes.previous_session("9기 1회차"))
        self.assertEqual(passes.previous_session("9기 2회차"), "9기 1회차")

    async def test_the_same_session_cannot_be_passed_twice(self):
        self.use("9기 1회차")
        with self.assertRaises(passes.PassAlreadyUsed):
            self.use("9기 1회차")


class PassModalTest(PassTestCase):
    def eligibility(self, session_name: str):
        return passes.check_eligibility("U11111111", session_name)

    async def test_confirm_button_appears_only_when_allowed(self):
        view = pass_event.build_pass_view(
            session_name="9기 1회차",
            channel_id="C11111111",
            eligibility=self.eligibility("9기 1회차"),
        )
        self.assertIn("submit", view)

    async def test_blocked_modal_shows_the_reason_without_a_confirm_button(self):
        self.use("9기 1회차")
        view = pass_event.build_pass_view(
            session_name="9기 2회차",
            channel_id="C11111111",
            eligibility=self.eligibility("9기 2회차"),
        )
        # 확인 버튼이 없으면 모달을 닫는 것 외에 아무 일도 일어나지 않는다.
        self.assertNotIn("submit", view)
        body = view["blocks"][0]["text"]["text"]
        self.assertIn("직전 회차", body)


class PassConfirmTest(PassTestCase):
    def body(self, session_name: str = "9기 1회차") -> dict:
        import json

        return {
            "user": {"id": "U11111111"},
            "view": {
                "private_metadata": json.dumps(
                    {"channel_id": "C11111111", "session_name": session_name}
                )
            },
        }

    async def confirm(self, client, session_name: str = "9기 1회차"):
        body = self.body(session_name)
        await pass_event.handle_pass_confirm(
            AsyncMock(), body, client, body["view"]
        )

    async def test_records_the_pass_and_announces_it_to_the_team(self):
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.5"}
        with patch.object(pass_event, "submission_preflight", AsyncMock(return_value=None)):
            await self.confirm(client)

        with get_connection() as connection:
            row = connection.execute(
                "SELECT user_id, session_name, cohort, slack_ts FROM session_passes"
            ).fetchone()
        self.assertEqual(row["session_name"], "9기 1회차")
        self.assertEqual(row["cohort"], "9기")
        # 공개 안내 메시지의 ts까지 기록해 둔다.
        self.assertEqual(row["slack_ts"], "1.5")

        posted = client.chat_postMessage.await_args.kwargs
        self.assertEqual(posted["channel"], "C11111111")
        self.assertIn("패스", posted["text"])
        self.assertIn("U11111111", posted["text"])

    async def test_does_not_record_when_no_longer_eligible(self):
        """모달을 열어 둔 사이 상황이 바뀌면 기록하지 않는다."""
        client = AsyncMock()
        with patch.object(
            pass_event, "submission_preflight", AsyncMock(return_value="마감됐어요.")
        ):
            await self.confirm(client)
        with get_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_passes").fetchone()[0],
                0,
            )
        client.chat_postMessage.assert_not_awaited()

    async def test_public_announcement_failure_keeps_the_pass(self):
        """안내가 실패해도 이미 기록된 패스를 되돌리지 않는다."""
        client = AsyncMock()
        client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
        with patch.object(pass_event, "submission_preflight", AsyncMock(return_value=None)):
            await self.confirm(client)
        with get_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_passes").fetchone()[0],
                1,
            )


class PassReminderTest(PassTestCase):
    async def test_passed_members_are_not_reminded(self):
        from slack.events import unsubmitted_reminder

        self.use("9기 1회차")
        client = AsyncMock()
        with (
            patch.object(
                unsubmitted_reminder,
                "get_submitted_user_ids",
                AsyncMock(return_value=set()),
            ),
            patch.object(
                unsubmitted_reminder, "announcement_sent", AsyncMock(return_value=False)
            ),
        ):
            counts = await unsubmitted_reminder.send_unsubmitted_reminders(
                client,
                session_name="9기 1회차",
                due_at=datetime.datetime(2099, 1, 8, 5, 0, tzinfo=KST),
            )
        self.assertEqual(counts["targets"], 0)
        client.chat_postMessage.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
