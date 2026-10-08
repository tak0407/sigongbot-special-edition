import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from slack_sdk.errors import SlackApiError

from config import settings
from database.online_retro_confirmation import mark_cancelled, reserve_confirmation
from database.online_retro_poll import create_poll, mark_poll_posted, save_vote
from database.scheduled_announcements import announcement_sent
from database.sqlite import initialize_database
from slack.events import online_retro_poll_reminder
from slack.events.online_retro_poll import ALL_OPTIONS, poll_actions
from slack.events.online_retro_poll_reminder import (
    reminded_users,
    reminder_at,
    reminder_key,
    send_poll_reminders,
)

KST = ZoneInfo("Asia/Seoul")
MEETING = "2026-10-11"
POSTED = datetime.datetime(2026, 10, 6, 22, 41, tzinfo=KST)
# 모임 이틀 전 20:00
REMIND = datetime.datetime(2026, 10, 9, 20, 0, tzinfo=KST)
GREEN = "C11111111"
BLUE = "C22222222"
TEAMS = {
    "U11111111": GREEN,
    "U22222222": GREEN,
    "U33333333": GREEN,
    "U44444444": BLUE,
    "U55555555": BLUE,
}


class FakeSlackResponse:
    """SlackApiError가 담는 응답 중 코드가 실제로 읽는 부분만 흉내 낸다."""

    def __init__(self, code: str):
        self.data = {"ok": False, "error": code}
        self.headers = {}

    def __getitem__(self, key):
        return self.data.get(key)


def slack_error(code: str) -> SlackApiError:
    return SlackApiError(code, FakeSlackResponse(code))


class PollReminderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", dict(TEAMS)))
        # 발송 간격은 rate limit 대비용이라 테스트에서는 기다리지 않는다.
        self.enterContext(patch.object(online_retro_poll_reminder, "SEND_INTERVAL_SECONDS", 0))
        self.alert = self.enterContext(
            patch.object(online_retro_poll_reminder, "send_alert", AsyncMock(return_value=True))
        )
        initialize_database()
        self.green = await self.post(GREEN, "그린팀")
        self.blue = await self.post(BLUE, "블루팀")
        await save_vote(poll_id=self.green["id"], user_id="U11111111", slots=["20:00~21:00"])
        self.client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ok": True}))

    async def post(
        self,
        team_channel: str,
        team_name: str,
        *,
        meeting_date: str = MEETING,
        is_test: bool = False,
        posted_at: datetime.datetime = POSTED,
    ) -> dict:
        poll = await create_poll(
            meeting_date=meeting_date,
            session_name="6기 5회차",
            team_channel=team_channel,
            team_name=team_name,
            slots=ALL_OPTIONS,
            is_test=is_test,
        )
        await mark_poll_posted(poll["id"], f"{posted_at.timestamp():.6f}", "C0ANNOUNCE")
        return poll

    def recipients(self) -> list[str]:
        return sorted(
            call.kwargs["channel"] for call in self.client.chat_postMessage.await_args_list
        )

    def test_reminder_goes_out_two_days_before_at_eight_pm(self):
        self.assertEqual(reminder_at(MEETING), REMIND)

    async def test_nothing_is_sent_before_the_reminder_time(self):
        result = await send_poll_reminders(
            self.client, now=REMIND - datetime.timedelta(minutes=1)
        )
        self.client.chat_postMessage.assert_not_awaited()
        self.assertEqual(result, {"targets": 0, "delivered": 0, "failed": 0})

    async def test_dms_members_who_have_not_answered_with_their_own_team_buttons(self):
        result = await send_poll_reminders(self.client, now=REMIND)

        self.assertEqual(
            self.recipients(), ["U22222222", "U33333333", "U44444444", "U55555555"]
        )
        self.assertEqual(result, {"targets": 4, "delivered": 4, "failed": 0})
        sent = {
            call.kwargs["channel"]: call.kwargs
            for call in self.client.chat_postMessage.await_args_list
        }
        self.assertEqual(sent["U22222222"]["blocks"][-1], poll_actions(self.green["id"]))
        self.assertEqual(sent["U44444444"]["blocks"][-1], poll_actions(self.blue["id"]))
        intro = sent["U44444444"]["blocks"][0]["text"]["text"]
        self.assertIn("10월 11일(일) 온라인 회고 시간 투표를 기다리고 있어요", intro)
        self.assertIn("블루팀 모임 시간은", intro)
        self.assertTrue(await announcement_sent(reminder_key(self.green["id"], "U22222222")))
        self.assertEqual(
            await reminded_users(),
            {
                self.green["id"]: {"U22222222", "U33333333"},
                self.blue["id"]: {"U44444444", "U55555555"},
            },
        )

    async def test_each_member_gets_one_dm_per_poll(self):
        await send_poll_reminders(self.client, now=REMIND)
        result = await send_poll_reminders(
            self.client, now=REMIND + datetime.timedelta(minutes=1)
        )
        self.assertEqual(self.client.chat_postMessage.await_count, 4)
        self.assertEqual(result["targets"], 0)

    async def test_late_start_still_sends_that_night_but_not_after_midnight(self):
        await send_poll_reminders(self.client, now=REMIND + datetime.timedelta(hours=4))
        self.client.chat_postMessage.assert_not_awaited()
        await send_poll_reminders(
            self.client, now=REMIND + datetime.timedelta(hours=3, minutes=59)
        )
        self.assertEqual(self.client.chat_postMessage.await_count, 4)

    async def test_skips_teams_with_a_decided_time_until_it_is_cancelled(self):
        confirmation = await reserve_confirmation(
            poll=self.blue,
            slot="20:00~21:00",
            starts_at="2026-10-11T20:00:00+09:00",
            ends_at="2026-10-11T21:00:00+09:00",
            calendar_event_id="event",
            conference_request_id="request",
        )
        await send_poll_reminders(self.client, now=REMIND)
        self.assertEqual(self.recipients(), ["U22222222", "U33333333"])

        await mark_cancelled(confirmation["id"])
        await send_poll_reminders(self.client, now=REMIND + datetime.timedelta(minutes=1))
        self.assertEqual(
            self.recipients(), ["U22222222", "U33333333", "U44444444", "U55555555"]
        )

    async def test_test_polls_never_dm_team_members(self):
        await self.post(GREEN, "그린팀", meeting_date="2026-10-18", is_test=True)
        await send_poll_reminders(self.client, now=reminder_at("2026-10-18"))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_poll_posted_after_the_reminder_time_is_not_chased(self):
        remind = reminder_at("2026-10-18")
        await self.post(
            GREEN,
            "그린팀",
            meeting_date="2026-10-18",
            posted_at=remind + datetime.timedelta(minutes=30),
        )
        await send_poll_reminders(self.client, now=remind + datetime.timedelta(minutes=31))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_permanent_errors_are_not_retried_but_temporary_ones_are(self):
        def deliver(*, channel, **_):
            if channel == "U22222222":
                raise slack_error("user_not_found")
            if channel == "U33333333":
                raise slack_error("internal_error")
            return {"ok": True}

        self.client.chat_postMessage.side_effect = deliver
        result = await send_poll_reminders(self.client, now=REMIND)
        self.assertEqual(result, {"targets": 4, "delivered": 2, "failed": 2})
        self.alert.assert_awaited_once()

        self.client.chat_postMessage.reset_mock(side_effect=True)
        await send_poll_reminders(self.client, now=REMIND + datetime.timedelta(minutes=1))
        self.assertEqual(self.recipients(), ["U33333333"])

    async def test_token_problems_stop_the_round(self):
        self.client.chat_postMessage.side_effect = slack_error("missing_scope")
        result = await send_poll_reminders(self.client, now=REMIND)
        self.assertEqual(self.client.chat_postMessage.await_count, 1)
        self.assertEqual(result, {"targets": 1, "delivered": 0, "failed": 1})
        self.alert.assert_awaited_once()
        self.assertEqual(await reminded_users(), {})


if __name__ == "__main__":
    unittest.main()
