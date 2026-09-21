import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from slack_sdk.errors import SlackApiError

from config import settings
from database.retrospective import create_retrospective, start_retrospective_submission
from database.scheduled_announcements import announcement_sent
from database.sessions import invalidate as invalidate_schedule
from database.sqlite import initialize_database
from slack.events import unsubmitted_reminder
from slack.events.unsubmitted_reminder import (
    REMINDER_LEAD,
    reminder_key,
    run_unsubmitted_reminder_once,
    send_unsubmitted_reminders,
)

SESSION_NAME = "6기 3회차"
# constants.py의 6기 3회차 마감과 같은 값이다.
SESSION_DUE = datetime.datetime(
    2026, 9, 29, 5, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
)
TEAMS = {"U11111111": "C11111111", "U22222222": "C11111111", "U33333333": "C22222222"}


class FakeSlackResponse:
    """SlackApiError가 담는 응답 중 코드가 실제로 읽는 부분만 흉내 낸다."""

    def __init__(self, code: str, headers: dict | None = None):
        self.data = {"ok": False, "error": code}
        self.headers = headers or {}

    def __getitem__(self, key):
        return self.data.get(key)


def slack_error(code: str, headers: dict | None = None) -> SlackApiError:
    return SlackApiError(code, FakeSlackResponse(code, headers))


class UnsubmittedReminderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", dict(TEAMS)))
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", ""))
        self.enterContext(patch.object(settings, "TEST_SUBMISSION_CHANNEL", ""))
        self.enterContext(patch.object(settings, "ANNOUNCEMENT_CHANNEL", "C99999999"))
        # 회차 일정 캐시는 프로세스 전역이라 임시 DB로 바꾸기 전후로 비워 준다.
        invalidate_schedule()
        self.addCleanup(invalidate_schedule)
        # 발송 간격은 rate limit 대비용이라 테스트에서는 기다리지 않는다.
        self.enterContext(patch.object(unsubmitted_reminder, "SEND_INTERVAL_SECONDS", 0))
        self.alert = self.enterContext(
            patch.object(unsubmitted_reminder, "send_alert", AsyncMock(return_value=True))
        )
        unsubmitted_reminder.forget_completed_sessions()
        self.addCleanup(unsubmitted_reminder.forget_completed_sessions)
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ok": True, "channel": "D11111111"}),
            chat_postEphemeral=AsyncMock(return_value={"ok": True}),
        )

    async def submit(self, user_id: str, *, is_test: bool = False) -> None:
        if is_test:
            await start_retrospective_submission(
                user_id=user_id,
                session_name=SESSION_NAME,
                slack_channel=TEAMS[user_id],
                good_points="good",
                improvements="improve",
                learnings="learn",
                action_item="act",
                is_test=True,
            )
            return
        await create_retrospective(
            user_id=user_id,
            session_name=SESSION_NAME,
            slack_channel=TEAMS[user_id],
            slack_ts="1",
            good_points="good",
            improvements="improve",
            learnings="learn",
            action_item="act",
        )

    async def send(self) -> dict[str, int]:
        return await send_unsubmitted_reminders(
            self.client, session_name=SESSION_NAME, due_at=SESSION_DUE
        )

    def sent_channels(self) -> list[str]:
        return [
            call.kwargs["channel"]
            for call in self.client.chat_postMessage.await_args_list
        ]

    async def test_sends_only_to_unsubmitted_members(self):
        await self.submit("U22222222")
        result = await self.send()
        self.assertEqual(result["targets"], 2)
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(sorted(self.sent_channels()), ["U11111111", "U33333333"])

    async def test_never_reveals_unsubmitted_members_in_a_channel(self):
        await self.send()
        # 발송 대상 channel은 전부 사용자 ID여야 한다. 팀 채널이나 공지 채널로
        # 나가는 순간 누가 미제출인지 공개된다.
        for channel in self.sent_channels():
            self.assertTrue(channel.startswith("U"), channel)
        self.client.chat_postEphemeral.assert_not_awaited()
        for call in self.client.chat_postMessage.await_args_list:
            blocks = str(call.kwargs["blocks"])
            for user_id in TEAMS:
                if user_id != call.kwargs["channel"]:
                    self.assertNotIn(user_id, blocks)

    async def test_reminder_carries_own_submission_button(self):
        await self.send()
        blocks = self.client.chat_postMessage.await_args_list[0].kwargs["blocks"]
        button = blocks[1]["elements"][0]
        self.assertEqual(button["action_id"], "start_retrospective_from_announcement")
        self.assertIn(SESSION_NAME, button["value"])

    async def test_does_not_send_twice_in_same_session(self):
        await self.send()
        self.assertTrue(await announcement_sent(reminder_key(SESSION_NAME, "U11111111")))
        self.client.chat_postMessage.reset_mock()
        result = await self.send()
        self.client.chat_postMessage.assert_not_awaited()
        self.assertEqual(result["delivered"], 0)

    async def test_test_submission_does_not_count_as_submitted(self):
        await self.submit("U11111111", is_test=True)
        result = await self.send()
        self.assertEqual(result["targets"], 3)
        self.assertIn("U11111111", self.sent_channels())

    async def test_retries_once_after_rate_limit(self):
        self.client.chat_postMessage.side_effect = [
            slack_error("ratelimited", {"Retry-After": "0"}),
            {"ok": True, "channel": "D11111111"},
            {"ok": True, "channel": "D22222222"},
            {"ok": True, "channel": "D33333333"},
        ]
        result = await self.send()
        self.assertEqual(result["delivered"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.client.chat_postMessage.await_count, 4)
        self.alert.assert_not_awaited()

    async def test_falls_back_to_private_ephemeral_and_alerts(self):
        self.client.chat_postMessage.side_effect = slack_error("channel_not_found")
        result = await self.send()
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["fallback"], 3)
        self.assertEqual(self.client.chat_postEphemeral.await_count, 3)
        for call in self.client.chat_postEphemeral.await_args_list:
            # 본인 팀 채널에, 본인에게만 보이게 보낸다.
            self.assertEqual(TEAMS[call.kwargs["user"]], call.kwargs["channel"])
        self.alert.assert_awaited_once()
        message = self.alert.await_args.args[0]
        self.assertIn("channel_not_found 3명", message)
        for user_id in TEAMS:
            self.assertNotIn(user_id, message)

    async def test_alerts_when_delivery_fails_entirely(self):
        """일시적 오류는 표시하지 않아 다음 tick에 다시 시도한다."""
        self.client.chat_postMessage.side_effect = slack_error("internal_error")
        self.client.chat_postEphemeral.side_effect = slack_error("user_not_in_channel")
        result = await self.send()
        self.assertEqual(result["failed"], 3)
        self.assertFalse(await announcement_sent(reminder_key(SESSION_NAME, "U11111111")))
        self.alert.assert_awaited_once()
        message = self.alert.await_args.args[0]
        self.assertIn("internal_error 3명", message)
        for user_id in TEAMS:
            self.assertNotIn(user_id, message)

    async def test_permanent_failure_is_not_retried_next_tick(self):
        """다시 보내도 같은 결과인 오류는 표시해 둬야 8시간 동안 반복하지 않는다."""
        self.client.chat_postMessage.side_effect = slack_error("cannot_dm_bot")
        self.client.chat_postEphemeral.side_effect = slack_error("user_not_in_channel")
        result = await self.send()
        self.assertEqual(result["failed"], 3)
        for user_id in TEAMS:
            self.assertTrue(await announcement_sent(reminder_key(SESSION_NAME, user_id)))

        self.client.chat_postMessage.reset_mock()
        self.client.chat_postEphemeral.reset_mock()
        again = await self.send()
        self.client.chat_postMessage.assert_not_awaited()
        self.client.chat_postEphemeral.assert_not_awaited()
        self.assertEqual(again["failed"], 0)

    async def test_scope_error_stops_the_run_instead_of_hitting_everyone(self):
        """토큰이나 스코프 문제는 나머지에게도 같은 결과이므로 즉시 접는다."""
        self.client.chat_postMessage.side_effect = slack_error("missing_scope")
        result = await self.send()
        self.assertEqual(self.client.chat_postMessage.await_count, 1)
        self.client.chat_postEphemeral.assert_not_awaited()
        self.assertEqual(result["failed"], 1)
        # 다음 회차에는 다시 시도해야 하므로 발송한 것으로 표시하지 않는다.
        for user_id in TEAMS:
            self.assertFalse(await announcement_sent(reminder_key(SESSION_NAME, user_id)))
        self.alert.assert_awaited_once()
        message = self.alert.await_args.args[0]
        self.assertIn("missing_scope", message)
        self.assertIn("중단", message)

    async def test_one_failure_does_not_stop_the_rest(self):
        self.client.chat_postMessage.side_effect = [
            slack_error("cannot_dm_bot"),
            {"ok": True, "channel": "D22222222"},
            {"ok": True, "channel": "D33333333"},
        ]
        self.client.chat_postEphemeral.side_effect = slack_error("user_not_in_channel")
        result = await self.send()
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(result["failed"], 1)


class UnsubmittedReminderWindowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", dict(TEAMS)))
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", ""))
        self.enterContext(patch.object(unsubmitted_reminder, "SEND_INTERVAL_SECONDS", 0))
        self.enterContext(
            patch.object(unsubmitted_reminder, "send_alert", AsyncMock(return_value=True))
        )
        invalidate_schedule()
        self.addCleanup(invalidate_schedule)
        # 회차 완료 표시는 프로세스 전역이라 테스트마다 비운다.
        unsubmitted_reminder.forget_completed_sessions()
        self.addCleanup(unsubmitted_reminder.forget_completed_sessions)
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ok": True, "channel": "D11111111"}),
            chat_postEphemeral=AsyncMock(return_value={"ok": True}),
        )

    async def test_sends_inside_the_window_for_the_current_session(self):
        now = SESSION_DUE - datetime.timedelta(hours=1)
        self.assertTrue(await run_unsubmitted_reminder_once(self.client, now=now))
        self.assertEqual(self.client.chat_postMessage.await_count, 3)
        blocks = self.client.chat_postMessage.await_args_list[0].kwargs["blocks"]
        self.assertIn(SESSION_NAME, blocks[0]["text"]["text"])

    async def test_stops_checking_once_the_session_is_done(self):
        """실패 없이 한 바퀴를 돈 회차는 남은 발송 창을 건너뛴다."""
        now = SESSION_DUE - datetime.timedelta(hours=1)
        self.assertTrue(await run_unsubmitted_reminder_once(self.client, now=now))
        self.client.chat_postMessage.reset_mock()

        later = SESSION_DUE - datetime.timedelta(minutes=30)
        self.assertFalse(await run_unsubmitted_reminder_once(self.client, now=later))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_keeps_checking_while_a_failure_can_still_be_retried(self):
        """일시적 실패가 남아 있으면 완료로 보지 않고 다음 tick에 다시 시도한다."""
        self.client.chat_postMessage.side_effect = slack_error("internal_error")
        self.client.chat_postEphemeral.side_effect = slack_error("user_not_in_channel")
        now = SESSION_DUE - datetime.timedelta(hours=1)
        self.assertTrue(await run_unsubmitted_reminder_once(self.client, now=now))

        self.client.chat_postMessage.reset_mock()
        self.client.chat_postMessage.side_effect = None
        self.client.chat_postMessage.return_value = {"ok": True, "channel": "D11111111"}
        later = SESSION_DUE - datetime.timedelta(minutes=30)
        self.assertTrue(await run_unsubmitted_reminder_once(self.client, now=later))
        self.assertEqual(self.client.chat_postMessage.await_count, 3)

    async def test_stays_quiet_before_the_window_opens(self):
        now = SESSION_DUE - REMINDER_LEAD - datetime.timedelta(minutes=1)
        self.assertFalse(await run_unsubmitted_reminder_once(self.client, now=now))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_stays_quiet_after_the_deadline(self):
        now = SESSION_DUE
        self.assertFalse(await run_unsubmitted_reminder_once(self.client, now=now))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_session_name_override_never_triggers_a_send(self):
        with patch.object(settings, "SESSION_NAME_OVERRIDE", "테스트 회차"):
            now = SESSION_DUE - datetime.timedelta(hours=1)
            self.assertFalse(await run_unsubmitted_reminder_once(self.client, now=now))
        self.client.chat_postMessage.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
