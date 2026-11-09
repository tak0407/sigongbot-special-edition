import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import settings
from database import online_retro_poll as polls
from database.online_retro_confirmation import get_confirmation, mark_cancelled, mark_confirmed, reserve_confirmation
from database.sqlite import get_connection, initialize_database
from google_workspace import calendar_meet
from slack.events.online_retro_confirmation import NO_ATTENDEES_WARNING, confirm_meeting_time, post_confirmation_announcement
from slack.events.online_retro_poll import (
    ALL_OPTIONS, UNAVAILABLE, _update_poll_message,
    handle_open_time_poll, handle_time_poll_submit,
)
from test_online_retro_confirmation import CREDENTIALS, FakeGoogle


KST = ZoneInfo("Asia/Seoul")
NOW = datetime.datetime(2026, 11, 5, 18, 0, tzinfo=KST)
GREEN = "C11111111"
BLUE = "C22222222"
MEMBERS = {"U11111111": GREEN, "U22222222": GREEN, "U33333333": BLUE}


class AutoConfirmTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temp.name) / "test.db")))
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", dict(MEMBERS)))
        self.enterContext(patch.object(settings, "ANNOUNCEMENT_CHANNEL", "C0ANNOUNCE"))
        self.enterContext(patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", "OPEN"))
        self.enterContext(patch.object(settings, "ONLINE_RETRO_TEAM_ATTENDEES", {}))
        self.enterContext(patch.object(settings, "GOOGLE_CALENDAR_TIMEZONE", "Asia/Seoul"))
        self.enterContext(patch.object(settings, "google_credentials", lambda: CREDENTIALS))
        self.enterContext(patch("slack.events.online_retro_lock._poll_locks", {}))
        self.enterContext(patch("slack.events.online_retro_poll.MESSAGE_LOCK", asyncio.Lock()))
        self.enterContext(patch("slack.events.online_retro_confirmation.tz_now", return_value=NOW))
        self.google = FakeGoogle()
        self.enterContext(patch.object(calendar_meet, "_request", self.google.request))
        self.enterContext(patch.object(calendar_meet, "get_access_token", AsyncMock(return_value="test")))
        self.client = SimpleNamespace(
            chat_update=AsyncMock(), chat_postMessage=AsyncMock(return_value={"ts": "999.111"}),
            views_open=AsyncMock(), users_info=AsyncMock(return_value={"user": {"profile": {}}}),
        )
        initialize_database()
        self.poll = await self.make_poll()

    async def make_poll(self, channel=GREEN, *, is_test=False):
        poll = await polls.create_poll(
            meeting_date="2026-11-08", session_name="6기 9회차",
            team_channel=channel, team_name="그린팀" if channel == GREEN else "블루팀",
            slots=ALL_OPTIONS, is_test=is_test,
        )
        await polls.mark_poll_posted(poll["id"], "123.456", "C0ANNOUNCE")
        return await polls.get_poll(poll["id"])

    async def vote(self, user, slots=None, *, now=NOW, poll=None):
        return await polls.save_vote(
            poll_id=(poll or self.poll)["id"], user_id=user,
            slots=slots or ["20:00~21:00"], now=now,
        )


class PollStorageTest(AutoConfirmTestCase):
    async def test_final_response_starts_window_and_edits_keep_original_start(self):
        await self.vote("U11111111")
        self.assertIsNone((await polls.get_poll(self.poll["id"])).get("all_voted_at"))
        last = NOW + datetime.timedelta(minutes=5)
        await self.vote("U22222222", now=last)
        first = (await polls.get_poll(self.poll["id"]))["all_voted_at"]
        self.assertEqual(datetime.datetime.fromisoformat(first), last)
        await self.vote("U22222222", ["21:00~22:00"], now=last + datetime.timedelta(minutes=30))
        self.assertEqual((await polls.get_poll(self.poll["id"]))["all_voted_at"], first)
        self.assertEqual(await polls.get_vote(self.poll["id"], "U22222222"), ["21:00~22:00"])

    async def test_unavailable_is_a_completed_response(self):
        await self.vote("U11111111")
        await self.vote("U22222222", [UNAVAILABLE])
        self.assertEqual(datetime.datetime.fromisoformat((await polls.get_poll(self.poll["id"]))["all_voted_at"]), NOW)

    async def test_other_people_do_not_replace_missing_team_member(self):
        await self.vote("U11111111")
        await self.vote("U99999999")
        self.assertIsNone((await polls.get_poll(self.poll["id"])).get("all_voted_at"))

    async def test_membership_change_restarts_window_only_after_new_member_responds(self):
        await self.vote("U11111111")
        await self.vote("U22222222")
        settings.SUBMISSION_DESTINATIONS["U44444444"] = GREEN
        after = NOW + datetime.timedelta(minutes=20)
        state = await polls.sync_poll_completion(self.poll["id"], now=after)
        self.assertIsNone(state["all_voted_at"])
        await self.vote("U44444444", now=after)
        self.assertEqual(datetime.datetime.fromisoformat((await polls.get_poll(self.poll["id"]))["all_voted_at"]), after)

    async def test_empty_and_test_teams_do_not_start_automatic_confirmation(self):
        for channel, is_test, user in (("C0EMPTY", False, "U99999999"), (BLUE, True, "U33333333")):
            poll = await self.make_poll(channel, is_test=is_test)
            await self.vote(user, poll=poll)
            self.assertIsNone((await polls.get_poll(poll["id"])).get("all_voted_at"))

    async def test_confirmed_poll_rejects_late_response_without_changing_votes(self):
        await self.vote("U11111111")
        reserved = await reserve_confirmation(
            poll=self.poll, slot="20:00~21:00",
            starts_at="2026-11-08T20:00:00+09:00", ends_at="2026-11-08T21:00:00+09:00",
            calendar_event_id="event1", conference_request_id="request1",
        )
        await mark_confirmed(
            confirmation_id=reserved["id"], calendar_event_id="event1",
            meet_url="https://meet.google.com/abc-defg-hij", meet_access_type=None,
        )
        saved = await self.vote("U11111111", ["21:00~22:00"])
        self.assertFalse(saved)
        self.assertEqual(await polls.get_vote(self.poll["id"], "U11111111"), ["20:00~21:00"])


class PollMessageTest(AutoConfirmTestCase):
    async def test_complete_team_shows_deadline_without_exposing_voter_identities(self):
        await self.make_poll(BLUE)
        await self.vote("U11111111")
        await self.vote("U22222222")
        await _update_poll_message(self.client, self.poll)
        message = self.client.chat_update.await_args.kwargs
        self.assertEqual((message["channel"], message["ts"]), ("C0ANNOUNCE", "123.456"))
        text = json.dumps(message["blocks"], ensure_ascii=False)
        self.assertIn("모든 조원이 투표를 마쳤어요", text)
        self.assertIn("1시간 뒤", text)
        self.assertIn(str(int(datetime.datetime(2026, 11, 5, 19, tzinfo=KST).timestamp())), text)
        self.assertIn("블루팀 0/1", text)
        for user in MEMBERS:
            self.assertNotIn(user, text)

    async def test_confirmation_updates_original_announcement_and_keeps_other_team_voting(self):
        await self.make_poll(BLUE)
        await self.vote("U11111111")
        await self.vote("U22222222", [UNAVAILABLE])
        confirmed, _ = await confirm_meeting_time(self.client, poll=self.poll, slot="20:00~21:00")
        self.client.chat_postMessage.assert_not_awaited()
        message = self.client.chat_update.await_args.kwargs
        self.assertEqual((message["channel"], message["ts"]), ("C0ANNOUNCE", "123.456"))
        text = json.dumps(message["blocks"], ensure_ascii=False)
        self.assertIn("11월 8일 일요일 20:00~21:00", text)
        self.assertIn("<@U11111111>", text)
        self.assertNotIn("U22222222", text)
        self.assertIn("블루팀 0/1", text)
        self.assertIn("open_online_retro_time_poll", text)
        from database.online_retro_confirmation import get_confirmation
        self.assertEqual((await get_confirmation(self.poll["id"]))["announced_slack_ts"], "123.456")

    async def test_all_confirmed_message_becomes_meeting_guide_without_vote_buttons(self):
        await self.vote("U11111111")
        await confirm_meeting_time(self.client, poll=self.poll, slot="20:00~21:00")
        blocks = self.client.chat_update.await_args.kwargs["blocks"]
        text = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("온라인 회고 모임 안내", text)
        self.assertNotIn("언제 모일까요", text)
        self.assertNotIn("open_online_retro_time_poll", text)

    async def test_open_button_for_confirmed_team_shows_closed_notice(self):
        await confirm_meeting_time(self.client, poll=self.poll, slot="20:00~21:00")
        await handle_open_time_poll(
            AsyncMock(), {"actions": [{"value": str(self.poll["id"])}],
                          "user": {"id": "U11111111"}, "trigger_id": "trigger"},
            self.client,
        )
        view = self.client.views_open.await_args.kwargs["view"]
        self.assertNotIn("callback_id", view)
        self.assertIn("확정", json.dumps(view, ensure_ascii=False))

    async def test_stale_modal_submission_does_not_claim_vote_was_saved(self):
        await self.vote("U11111111")
        await confirm_meeting_time(self.client, poll=self.poll, slot="20:00~21:00")
        ack = AsyncMock()
        await handle_time_poll_submit(
            ack, {"user": {"id": "U11111111"}}, self.client,
            {"private_metadata": str(self.poll["id"]),
             "state": {"values": {"available_slots": {"available_slots_input": {
                 "selected_options": [{"value": "21:00~22:00"}]}}}}},
        )
        result = json.dumps(ack.await_args.kwargs, ensure_ascii=False)
        self.assertIn("확정", result)
        self.assertNotIn("투표에 저장했어요", result)
        self.assertEqual(await polls.get_vote(self.poll["id"], "U11111111"), ["20:00~21:00"])

    async def test_same_timestamp_in_different_channel_is_not_part_of_announcement(self):
        blue = await self.make_poll(BLUE)
        await polls.mark_poll_posted(blue["id"], "123.456", "C0OTHER")
        await _update_poll_message(self.client, self.poll)
        self.assertNotIn("블루팀", json.dumps(self.client.chat_update.await_args.kwargs, ensure_ascii=False))

    async def test_poll_button_does_not_route_to_another_channels_poll(self):
        blue = await self.make_poll(BLUE)
        await polls.mark_poll_posted(blue["id"], "123.456", "C0OTHER")
        await handle_open_time_poll(
            AsyncMock(), {"actions": [{"value": str(self.poll["id"])}],
                          "user": {"id": "U33333333"}, "trigger_id": "trigger"},
            self.client,
        )
        self.assertNotIn("callback_id", self.client.views_open.await_args.kwargs["view"])

    async def test_message_clean_does_not_erase_a_new_response_during_slack_request(self):
        await self.vote("U11111111")
        async def another_response(**kwargs):
            self.client.chat_update.side_effect = None
            await self.vote("U22222222")
        self.client.chat_update.side_effect = another_response
        await _update_poll_message(self.client, self.poll)
        self.assertGreater((await polls.get_poll(self.poll["id"]))["message_dirty"], 0)


class AutoConfirmSchedulerTest(AutoConfirmTestCase):
    async def tick(self, now):
        from slack.events.online_retro_auto_confirm import process_auto_confirmations
        return await process_auto_confirmations(self.client, now=now)

    async def complete(self, *, first=None, last=None, now=NOW):
        await self.vote("U11111111", first, now=now)
        await self.vote("U22222222", last, now=now)

    async def test_confirms_at_one_hour_boundary_and_updates_only_original_message(self):
        await self.complete()
        await self.tick(NOW + datetime.timedelta(minutes=59, seconds=59))
        self.assertEqual(self.google.events, {})
        await self.tick(NOW + datetime.timedelta(hours=1))
        confirmation = await get_confirmation(self.poll["id"])
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(confirmation["slot"], "20:00~21:00")
        self.assertEqual(confirmation["announced_slack_ts"], "123.456")
        self.client.chat_postMessage.assert_not_awaited()
        self.assertEqual(self.client.chat_update.await_args.kwargs["channel"], "C0ANNOUNCE")

    async def test_teams_finish_independently_and_share_one_message(self):
        blue = await self.make_poll(BLUE)
        await self.complete()
        await self.vote("U33333333", poll=blue, now=NOW + datetime.timedelta(minutes=30))
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")
        self.assertIsNone(await get_confirmation(blue["id"]))
        message = json.dumps(self.client.chat_update.await_args.kwargs, ensure_ascii=False)
        self.assertIn("블루팀", message)
        self.assertIn("1시간 뒤", message)
        await self.tick(NOW + datetime.timedelta(minutes=90))
        self.assertEqual((await get_confirmation(blue["id"]))["status"], "confirmed")
        self.assertEqual(len(self.google.events), 2)

    async def test_latest_votes_choose_most_popular_time_without_extending_deadline(self):
        await self.complete(first=["20:00~21:00"], last=["21:00~22:00"])
        await self.vote("U11111111", ["21:00~22:00"], now=NOW + datetime.timedelta(minutes=30))
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["slot"], "21:00~22:00")

    async def test_equal_counts_choose_earliest_time(self):
        await self.complete(first=["21:00~22:00"], last=["20:00~21:00"])
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["slot"], "20:00~21:00")

    async def test_repeated_and_concurrent_scheduler_runs_do_not_duplicate_google_event(self):
        await self.complete()
        due = NOW + datetime.timedelta(hours=1)
        await asyncio.gather(self.tick(due), self.tick(due))
        await self.tick(due + datetime.timedelta(minutes=1))
        self.assertEqual(self.google.counts("POST"), 1)
        self.assertEqual(len(self.google.events), 1)

    async def test_persisted_deadline_survives_new_database_initialization(self):
        await self.complete()
        initialize_database()
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")

    async def test_existing_completed_poll_gets_an_hour_from_first_detection(self):
        await self.complete()
        with get_connection() as connection:
            connection.execute("UPDATE online_retro_time_polls SET all_voted_at = NULL")
        observed = NOW + datetime.timedelta(hours=2)
        await self.tick(observed)
        self.assertIsNone(await get_confirmation(self.poll["id"]))
        self.assertEqual(datetime.datetime.fromisoformat((await polls.get_poll(self.poll["id"]))["all_voted_at"]), observed)
        await self.tick(observed + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")

    async def test_google_failure_keeps_pending_notice_and_retries_after_five_minutes(self):
        await self.complete()
        self.google.queued_failures = [(404, "notFound"), (403, "forbidden")]
        due = NOW + datetime.timedelta(hours=1)
        await self.tick(due)
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "failed")
        text = json.dumps(self.client.chat_update.await_args.kwargs, ensure_ascii=False)
        self.assertIn("자동 확정을 완료하지 못했어요", text)
        self.assertNotIn("확정됐어요", text)
        calls = len(self.google.calls)
        await self.tick(due + datetime.timedelta(minutes=1))
        self.assertEqual(len(self.google.calls), calls)
        await self.tick(due + datetime.timedelta(minutes=5))
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")
        self.assertEqual(len(self.google.events), 1)

    async def test_slack_failure_retries_announcement_without_calling_google_again(self):
        await self.complete()
        self.client.chat_update.side_effect = RuntimeError("offline")
        due = NOW + datetime.timedelta(hours=1)
        await self.tick(due)
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")
        self.assertIsNone((await get_confirmation(self.poll["id"]))["announced_slack_ts"])
        calls = len(self.google.calls)
        self.client.chat_update.side_effect = None
        await self.tick(due + datetime.timedelta(minutes=1))
        self.assertEqual(len(self.google.calls), calls)
        self.assertEqual((await get_confirmation(self.poll["id"]))["announced_slack_ts"], "123.456")

    async def test_failed_refresh_of_existing_confirmation_keeps_message_pending(self):
        await self.complete()
        confirmation, _ = await confirm_meeting_time(self.client, poll=self.poll, slot="20:00~21:00")
        self.assertEqual((await polls.get_poll(self.poll["id"]))["message_dirty"], 0)
        self.client.chat_update.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await post_confirmation_announcement(self.client, confirmation, channel="C0ANNOUNCE")
        self.assertGreater((await polls.get_poll(self.poll["id"]))["message_dirty"], 0)
        self.client.chat_update.side_effect = None
        calls = len(self.google.calls)
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await polls.get_poll(self.poll["id"]))["message_dirty"], 0)
        self.assertEqual(len(self.google.calls), calls)

    async def test_failed_team_does_not_prevent_other_team_confirmation(self):
        blue = await self.make_poll(BLUE)
        await self.complete()
        await self.vote("U33333333", poll=blue)
        self.google.queued_failures = [(404, "notFound"), (403, "forbidden")]
        await self.tick(NOW + datetime.timedelta(hours=1))
        states = [(await get_confirmation(p["id"]))["status"] for p in (self.poll, blue)]
        self.assertEqual(sorted(states), ["confirmed", "failed"])

    async def test_all_unavailable_does_not_create_empty_meeting(self):
        await self.complete(first=[UNAVAILABLE], last=[UNAVAILABLE])
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual(self.google.events, {})
        self.assertIsNone(await get_confirmation(self.poll["id"]))
        self.assertTrue((await polls.get_poll(self.poll["id"]))["auto_confirm_error"])

    async def test_past_winning_time_is_not_silently_replaced_with_another_time(self):
        now = datetime.datetime(2026, 11, 8, 15, 0, tzinfo=KST)
        await self.complete(first=["13:00~14:00", "22:00~23:00"], last=["13:00~14:00"], now=now)
        await self.tick(now + datetime.timedelta(hours=1))
        self.assertEqual(self.google.events, {})
        self.assertTrue((await polls.get_poll(self.poll["id"]))["auto_confirm_error"])

    async def test_cancelled_and_manually_confirmed_meetings_are_left_alone(self):
        await self.complete()
        manual, _ = await confirm_meeting_time(self.client, poll=self.poll, slot="21:00~22:00")
        count = len(self.google.calls)
        await self.tick(NOW + datetime.timedelta(hours=1))
        self.assertEqual((await get_confirmation(self.poll["id"]))["slot"], "21:00~22:00")
        await mark_cancelled(manual["id"])
        await self.tick(NOW + datetime.timedelta(hours=2))
        self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "cancelled")
        self.assertEqual(len(self.google.calls), count)

    async def test_test_and_unposted_polls_are_not_automatically_confirmed(self):
        test = await self.make_poll(BLUE, is_test=True)
        await self.vote("U33333333", poll=test)
        unposted = await polls.create_poll(
            meeting_date="2026-11-15", session_name="6기 10회차", team_channel=BLUE,
            team_name="블루팀", slots=ALL_OPTIONS, is_test=False,
        )
        await self.vote("U33333333", poll=unposted)
        await self.tick(NOW + datetime.timedelta(hours=2))
        self.assertEqual(self.google.events, {})

    async def test_google_request_for_one_team_does_not_block_other_team_voting(self):
        blue = await self.make_poll(BLUE)
        await self.complete()
        entered, release = asyncio.Event(), asyncio.Event()
        request = self.google.request
        async def slow_request(*args, **kwargs):
            entered.set()
            await release.wait()
            return await request(*args, **kwargs)
        with patch.object(calendar_meet, "_request", slow_request):
            task = asyncio.create_task(self.tick(NOW + datetime.timedelta(hours=1)))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                ack = AsyncMock()
                await asyncio.wait_for(handle_time_poll_submit(
                    ack, {"user": {"id": "U33333333"}}, self.client,
                    {"private_metadata": str(blue["id"]),
                     "state": {"values": {"available_slots": {"available_slots_input": {
                         "selected_options": [{"value": "21:00~22:00"}]}}}}},
                ), timeout=1)
                self.assertEqual(await polls.get_vote(blue["id"], "U33333333"), ["21:00~22:00"])
            finally:
                release.set()
                await task

    async def test_missing_invitees_warning_is_saved_and_sent_to_operator(self):
        await self.complete()
        with patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""), patch(
            "slack.events.online_retro_auto_confirm.send_alert",
            AsyncMock(return_value=True), create=True,
        ) as alert:
            due = NOW + datetime.timedelta(hours=1)
            await self.tick(due)
            confirmation = await get_confirmation(self.poll["id"])
            self.assertEqual(confirmation["status"], "confirmed")
            self.assertEqual(confirmation.get("warning_text"), NO_ATTENDEES_WARNING)
            self.assertIn(NO_ATTENDEES_WARNING, alert.await_args.args[0])
            await self.tick(due + datetime.timedelta(minutes=1))
            self.assertEqual(alert.await_count, 1)
            self.assertEqual(self.google.counts("POST"), 1)

    async def test_access_policy_warning_is_saved_and_visible_in_admin_details(self):
        from dashboard.online_retro import _confirmation_detail
        await self.complete()
        self.google.meet_patch_error = (403, "forbidden")
        with patch("slack.events.online_retro_auto_confirm.send_alert", AsyncMock(return_value=True), create=True) as alert:
            await self.tick(NOW + datetime.timedelta(hours=1))
            confirmation = await get_confirmation(self.poll["id"])
            self.assertEqual(confirmation["status"], "confirmed")
            self.assertIn("입장 정책", confirmation.get("warning_text") or "")
            self.assertIn("입장 정책을 자동으로 바꾸지 못했어요", _confirmation_detail(confirmation))
            self.assertIn("입장 정책", alert.await_args.args[0])

    async def test_failed_operator_warning_retries_without_recreating_meeting(self):
        await self.complete()
        with patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""), patch(
            "slack.events.online_retro_auto_confirm.send_alert",
            AsyncMock(side_effect=[False, True]), create=True,
        ) as alert:
            due = NOW + datetime.timedelta(hours=1)
            await self.tick(due)
            calls = len(self.google.calls)
            await self.tick(due + datetime.timedelta(minutes=1))
            await self.tick(due + datetime.timedelta(minutes=2))
            self.assertEqual(alert.await_count, 2)
            self.assertEqual(len(self.google.calls), calls)
            self.assertEqual((await get_confirmation(self.poll["id"]))["status"], "confirmed")
