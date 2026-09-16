import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from database.sqlite import initialize_database
from database.retrospective import start_retrospective_submission, mark_retrospective_posted
from slack.events import submission_entry as entry
from slack.events.command_retrospective import handle_command_retrospective
from slack.events.test_announcement import handle_start_from_announcement, handle_method_select


class SubmissionEntryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for key, value in {
            "DATABASE_PATH": str(Path(temporary.name) / "test.db"),
            "SESSION_NAME_OVERRIDE": "",
            "SUBMISSION_DESTINATIONS": {"U1": "CTEAM"},
            "SUBMISSION_CHANNEL_CHOOSER_IDS": [],
        }.items():
            self.enterContext(patch.object(settings, key, value))
        initialize_database()
        self.now = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc)
        self.enterContext(patch.object(entry, "tz_now", return_value=self.now))
        self.current = self.enterContext(patch.object(entry, "get_current_session_info", return_value=(0, "6기 1회차", datetime.timedelta(days=1), True)))
        self.client = SimpleNamespace(views_open=AsyncMock(), chat_postEphemeral=AsyncMock())

    async def start(self, source, session="6기 1회차", extra=None):
        self.client.views_open.reset_mock()
        self.client.chat_postEphemeral.reset_mock()
        ack = AsyncMock()
        if source == "command":
            await handle_command_retrospective(ack, {"user_id": "U1", "channel_id": "CSOURCE", "trigger_id": "trigger"}, self.client)
        else:
            await handle_start_from_announcement(ack, {
                "user": {"id": "U1"}, "channel": {"id": "CSOURCE"}, "trigger_id": "trigger",
                "actions": [{"value": json.dumps({"session_name": session, "channel_id": "CSOURCE", **(extra or {})})}],
            }, self.client)
        ack.assert_awaited_once_with()

    async def record(self, session="6기 1회차", is_test=False):
        row = await start_retrospective_submission(
            user_id="U1", session_name=session, slack_channel="CTEAM",
            good_points="좋음", improvements="개선", learnings="배움", action_item="실천", is_test=is_test,
        )
        await mark_retrospective_posted(row["id"], "1.2")
        return row

    async def test_both_entries_open_chooser_and_start_both_methods(self):
        for source in ("command", "button"):
            for method, callback in (("direct", "retrospective_submit"), ("guided", "guided_retrospective_submit")):
                with self.subTest(source=source, method=method):
                    await self.start(source)
                    view = self.client.views_open.await_args.kwargs["view"]
                    self.assertEqual(view["callback_id"], "select_retrospective_method")
                    self.assertFalse(json.loads(view["private_metadata"])["test_mode"])
                    view["state"] = {"values": {"retrospective_method": {"method_input": {"selected_option": {"value": method}}}}}
                    ack = AsyncMock()
                    with patch("slack.events.test_announcement.get_latest_temp_retrospective", return_value={"good_points": "복원된 내용"}):
                        await handle_method_select(ack, {"user": {"id": "U1"}, "view": view}, self.client)
                    next_view = ack.await_args.kwargs["view"]
                    self.assertEqual(next_view["callback_id"], callback)
                    if method == "direct":
                        block = next(b for b in next_view["blocks"] if b.get("block_id") == "good_points")
                        self.assertEqual(block["element"]["initial_value"], "복원된 내용")

    async def test_submitted_user_is_blocked_at_both_entries(self):
        await self.record()
        for source in ("command", "button"):
            await self.start(source)
            self.client.views_open.assert_not_awaited()
            self.assertIn("이미", self.client.chat_postEphemeral.await_args.kwargs["text"])

    async def test_closed_button_does_not_switch_to_current_session(self):
        self.current.return_value = (1, "다음 회차", datetime.timedelta(days=1), True)
        await self.start("button")
        self.client.views_open.assert_not_awaited()
        self.assertIn("제출 기간", self.client.chat_postEphemeral.await_args.kwargs["text"])

    async def test_deadline_and_inactive_period_are_blocked(self):
        for source in ("command", "button"):
            with patch.object(entry, "tz_now", return_value=self.now + datetime.timedelta(days=10)):
                await self.start(source)
                self.client.views_open.assert_not_awaited()
            self.current.return_value = (0, "6기 1회차", datetime.timedelta(0), False)
            await self.start(source)
            self.client.views_open.assert_not_awaited()

    async def test_unknown_session_and_missing_team_are_blocked(self):
        for session in ("없는 회차", "", None, ["invalid"]):
            await self.start("button", session=session, extra={"test_mode": True})
            self.client.views_open.assert_not_awaited()
            self.assertIn("회차를 찾지", self.client.chat_postEphemeral.await_args.kwargs["text"])
        with patch.object(settings, "SUBMISSION_DESTINATIONS", {}):
            for source in ("command", "button"):
                await self.start(source)
                self.client.views_open.assert_not_awaited()
                self.assertIn("팀 배정", self.client.chat_postEphemeral.await_args.kwargs["text"])

    async def test_test_session_allows_repeated_entry_and_database_submission(self):
        for session in ("테스트 회차", "별도 테스트"):
            with patch.object(settings, "SESSION_NAME_OVERRIDE", session):
                self.current.return_value = (-1, session, datetime.timedelta(0), True)
                first = await self.record(session, is_test=True)
                for source in ("command", "button"):
                    await self.start(source, session=session)
                    self.assertEqual(self.client.views_open.await_args.kwargs["view"]["callback_id"], "select_retrospective_method")
                second = await self.record(session, is_test=True)
                self.assertNotEqual(first["id"], second["id"])
