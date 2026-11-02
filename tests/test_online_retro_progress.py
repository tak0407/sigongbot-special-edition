import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import OnlineRetroMeeting, settings
from database.online_retro_attendance import record_attendance
from database.online_retro_leader import choose_meeting_leader
from database.retrospective import (
    get_submitted_user_ids, mark_retrospective_posted, start_retrospective_submission,
)
from database.sqlite import get_connection, initialize_database
from slack.events.online_retro_meeting import (
    handle_start_online_retro_photo,
    handle_start_online_retro_sharing,
    post_meeting_leader,
    post_writing_time_up,
    post_writing_phase,
    notify_all_writing_complete,
    run_online_retro_meeting_scheduler,
)
from slack.events.view_retrospective_submit import handle_view_retrospective_submit


KST = ZoneInfo("Asia/Seoul")
START = datetime.datetime(2026, 11, 8, 20, tzinfo=KST)


class ProgressTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(temporary.name) / "test.db")))
        self.meeting = OnlineRetroMeeting(
            session_name="6기 9회차", starts_at=START,
            notify_at=START - datetime.timedelta(minutes=10),
            channel="C11111111", url="https://meet.example.com/retro",
        )
        self.enterContext(patch.object(settings, "ONLINE_RETRO_MEETINGS", [self.meeting]))
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", {
            "U11111111": "C11111111", "U22222222": "C11111111",
        }))
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "1.0"}), chat_postEphemeral=AsyncMock(),
        )

    async def attend(self, user_id, *, at=None, channel="C11111111"):
        await record_attendance(session_name="6기 9회차", team_channel=channel, user_id=user_id)
        instant = at or START - datetime.timedelta(minutes=1)
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_attendance SET attended_at = ? WHERE user_id = ? AND team_channel = ?",
                (instant.astimezone(datetime.timezone.utc).isoformat(), user_id, channel),
            )

    async def choose(self, *, now=None, starts_at=None):
        return await choose_meeting_leader(
            session_name="6기 9회차", team_channel="C11111111",
            starts_at=starts_at or START, now=now or START,
        )

    def action(self, user_id):
        return {
            "actions": [{"value": json.dumps({
                "session_name": "6기 9회차", "team_channel": "C11111111",
            })}],
            "user": {"id": user_id}, "channel": {"id": "C11111111"},
        }

    async def submit(self, user_id, *, posted=True, is_test=False):
        row = await start_retrospective_submission(
            user_id=user_id, session_name="6기 9회차", slack_channel="C11111111",
            good_points="좋음", improvements="개선", learnings="배움", action_item="실행",
            is_test=is_test,
        )
        if posted:
            await mark_retrospective_posted(row["id"], "1.0")
        return row

    def submission_view(self):
        return {
            "private_metadata": json.dumps({"session_name": "6기 9회차", "channel_id": "C11111111"}),
            "state": {"values": {field: {field + "_input": {"value": "회고 내용"}}
                                 for field in ("good_points", "improvements", "learnings", "action_item")}},
        }


class MeetingLeaderTest(ProgressTestCase):
    async def test_leader_is_not_selected_before_the_meeting_starts(self):
        await self.attend("U11111111")
        self.assertIsNone(await self.choose(now=START - datetime.timedelta(seconds=1)))

    async def test_draw_excludes_late_arrivals_and_other_teams(self):
        await self.attend("U11111111", at=START - datetime.timedelta(minutes=2))
        await self.attend("U22222222", at=START)
        await self.attend("U33333333", at=START + datetime.timedelta(seconds=1))
        await self.attend("U44444444", channel="C22222222")
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[-1]):
            selected = await self.choose(now=START + datetime.timedelta(seconds=30))
        self.assertEqual(selected, "U22222222")

    async def test_empty_meeting_waits_until_someone_arrives(self):
        self.assertIsNone(await self.choose())
        await self.attend("U11111111", at=START + datetime.timedelta(minutes=1))
        self.assertEqual(await self.choose(now=START + datetime.timedelta(minutes=1)), "U11111111")

    async def test_repeated_and_concurrent_draws_keep_the_saved_leader(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[0]):
            first = await self.choose()
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[-1]):
            repeated = await asyncio.gather(*(self.choose() for _ in range(5)))
        self.assertEqual(first, "U11111111")
        self.assertEqual(repeated, ["U11111111"] * 5)

    async def test_rescheduled_meeting_has_its_own_draw(self):
        await self.attend("U11111111")
        self.assertEqual(await self.choose(), "U11111111")
        await self.attend("U22222222", at=START + datetime.timedelta(minutes=10))
        later = START + datetime.timedelta(hours=1)
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[-1]):
            self.assertEqual(await self.choose(now=later, starts_at=later), "U22222222")

    async def test_announcement_is_once_and_keeps_leader_after_slack_failure(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        self.client.chat_postMessage.side_effect = [RuntimeError("Slack unavailable"), {}, {}]
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[0]):
            with self.assertRaisesRegex(RuntimeError, "Slack unavailable"):
                await post_meeting_leader(self.client, self.meeting, now=START)
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[-1]):
            await post_meeting_leader(self.client, self.meeting, now=START)
            await post_meeting_leader(self.client, self.meeting, now=START)
        messages = self.client.chat_postMessage.await_args_list
        self.assertEqual(len(messages), 2)
        for call in messages:
            self.assertIn("<@U11111111>", json.dumps(call.kwargs))
            self.assertNotIn("U22222222", json.dumps(call.kwargs))

    async def test_attendee_other_than_leader_can_advance_both_stages(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        with patch("database.online_retro_leader.random.choice", side_effect=lambda users: users[0]):
            await post_meeting_leader(self.client, self.meeting, now=START)
        self.client.chat_postMessage.reset_mock()
        await handle_start_online_retro_sharing(AsyncMock(), self.action("U22222222"), self.client)
        await handle_start_online_retro_photo(AsyncMock(), self.action("U22222222"), self.client)
        self.assertEqual(self.client.chat_postMessage.await_count, 2)
        self.client.chat_postEphemeral.assert_not_awaited()

    async def test_scheduler_announces_the_leader_when_the_meeting_starts(self):
        await self.attend("U11111111")
        with (
            patch("slack.events.online_retro_meeting.tz_now", return_value=START),
            patch("slack.events.online_retro_meeting.asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_online_retro_meeting_scheduler(self.client)
        bodies = [json.dumps(call.kwargs, ensure_ascii=False) for call in self.client.chat_postMessage.await_args_list]
        self.assertTrue(any("이끌이" in body and "<@U11111111>" in body for body in bodies))


class WritingCompletionTest(ProgressTestCase):
    async def test_sharing_order_does_not_mark_pending_or_test_submissions_complete(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        await self.submit("U11111111", posted=False)
        await self.submit("U22222222", is_test=True)
        await handle_start_online_retro_sharing(AsyncMock(), self.action("U11111111"), self.client)
        text = self.client.chat_postMessage.await_args.kwargs["blocks"][0]["text"]["text"]
        self.assertIn("<@U11111111> ✍️ 작성 확인 필요", text)
        self.assertIn("<@U22222222> ✍️ 작성 확인 필요", text)
        self.assertNotIn("✅ 작성 완료", text)

    async def test_zero_attendees_never_means_everyone_finished(self):
        await self.submit("U11111111")
        self.assertFalse(await notify_all_writing_complete(self.client, self.meeting, now=START))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_pending_and_test_submissions_do_not_finish_the_group(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        await self.submit("U11111111")
        await self.submit("U22222222", is_test=True)
        self.assertFalse(await notify_all_writing_complete(self.client, self.meeting, now=START))
        pending = await self.submit("U22222222", posted=False)
        self.assertFalse(await notify_all_writing_complete(self.client, self.meeting, now=START))
        self.client.chat_postMessage.assert_not_awaited()
        await mark_retrospective_posted(pending["id"], "2.0")
        self.assertTrue(await notify_all_writing_complete(self.client, self.meeting, now=START))

    async def test_completion_waits_until_start_even_if_submissions_already_exist(self):
        await self.attend("U11111111")
        await self.submit("U11111111")
        self.assertFalse(await notify_all_writing_complete(
            self.client, self.meeting, now=START - datetime.timedelta(seconds=1)
        ))
        self.client.chat_postMessage.assert_not_awaited()

    async def test_early_completion_posts_once_with_sharing_button_and_suppresses_timer(self):
        await self.attend("U11111111")
        await self.attend("U22222222")
        await self.submit("U11111111")
        await self.submit("U22222222")
        await asyncio.gather(*(
            notify_all_writing_complete(self.client, self.meeting, now=START + datetime.timedelta(minutes=5))
            for _ in range(2)
        ))
        await post_writing_time_up(self.client, self.meeting)
        self.client.chat_postMessage.assert_awaited_once()
        message = self.client.chat_postMessage.await_args.kwargs
        self.assertIn("모두 회고 작성을 마쳤어요", json.dumps(message, ensure_ascii=False))
        self.assertEqual(message["blocks"][1]["elements"][0]["action_id"], "start_online_retro_sharing")

    async def test_completion_can_follow_timer_when_last_attendee_finishes_later(self):
        await self.attend("U11111111")
        await post_writing_time_up(self.client, self.meeting)
        await self.submit("U11111111")
        self.assertTrue(await notify_all_writing_complete(
            self.client, self.meeting, now=START + datetime.timedelta(minutes=25)
        ))
        self.assertEqual(self.client.chat_postMessage.await_count, 2)

    async def test_no_writing_announcements_after_sharing_has_started(self):
        await self.attend("U11111111")
        await self.submit("U11111111")
        await handle_start_online_retro_sharing(AsyncMock(), self.action("U11111111"), self.client)
        self.client.chat_postMessage.reset_mock()
        await notify_all_writing_complete(self.client, self.meeting, now=START + datetime.timedelta(minutes=25))
        await post_writing_time_up(self.client, self.meeting)
        self.client.chat_postMessage.assert_not_awaited()

    async def test_failed_completion_announcement_is_retried(self):
        await self.attend("U11111111")
        await self.submit("U11111111")
        self.client.chat_postMessage.side_effect = [RuntimeError("Slack unavailable"), {}]
        with self.assertRaisesRegex(RuntimeError, "Slack unavailable"):
            await notify_all_writing_complete(self.client, self.meeting, now=START)
        self.assertTrue(await notify_all_writing_complete(self.client, self.meeting, now=START))
        self.assertEqual(self.client.chat_postMessage.await_count, 2)

    async def test_scheduler_checks_whether_all_attendees_finished(self):
        await self.attend("U11111111")
        await self.submit("U11111111")
        with (
            patch("slack.events.online_retro_meeting.tz_now", return_value=START + datetime.timedelta(minutes=5)),
            patch("slack.events.online_retro_meeting.asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_online_retro_meeting_scheduler(self.client)
        bodies = [json.dumps(call.kwargs, ensure_ascii=False) for call in self.client.chat_postMessage.await_args_list]
        self.assertTrue(any("모두 회고 작성을 마쳤어요" in body for body in bodies))

    async def test_successful_slack_submission_immediately_checks_group_completion(self):
        await self.attend("U11111111")
        view = self.submission_view()
        with patch("slack.events.online_retro_meeting.tz_now", return_value=START + datetime.timedelta(minutes=5)):
            await handle_view_retrospective_submit(
                AsyncMock(), {"user": {"id": "U11111111"}, "view": view}, self.client, view,
            )
        bodies = [json.dumps(call.kwargs, ensure_ascii=False, default=lambda block: block.to_dict())
                  for call in self.client.chat_postMessage.await_args_list]
        self.assertTrue(any("모두 회고 작성을 마쳤어요" in body for body in bodies))

    async def test_delayed_scheduler_does_not_restart_writing_after_everyone_finished(self):
        await self.attend("U11111111")
        await self.submit("U11111111")
        await notify_all_writing_complete(self.client, self.meeting, now=START)
        await post_writing_phase(self.client, self.meeting)
        self.client.chat_postMessage.assert_awaited_once()

    async def test_progress_notification_failure_preserves_successfully_posted_submission(self):
        await self.attend("U11111111")
        view = self.submission_view()
        with patch("slack.events.online_retro_meeting.notify_submission_progress", side_effect=RuntimeError("notice unavailable")):
            await handle_view_retrospective_submit(
                AsyncMock(), {"user": {"id": "U11111111"}, "view": view}, self.client, view,
            )
        self.assertEqual(await get_submitted_user_ids("6기 9회차", posted_only=True), {"U11111111"})
