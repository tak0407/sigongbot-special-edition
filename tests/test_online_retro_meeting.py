import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import OnlineRetroMeeting, parse_online_retro_meetings, settings
from database.sqlite import get_connection, initialize_database
from slack.events.online_retro_meeting import (
    build_online_retro_blocks,
    build_time_up_blocks,
    build_writing_blocks,
    handle_online_retro_attendance,
    handle_online_retro_photo_submit,
    handle_open_online_retro_photo_upload,
    handle_start_online_retro_sharing,
    post_online_retro_announcement,
    post_writing_phase,
    post_writing_time_up,
)
from slack.events.online_retro_poll import (
    handle_open_time_poll,
    handle_time_poll_submit,
    post_team_time_poll,
)


KST = ZoneInfo("Asia/Seoul")


def meeting() -> OnlineRetroMeeting:
    return OnlineRetroMeeting(
        session_name="6기 1회차",
        starts_at=datetime.datetime(2026, 9, 14, 22, 0, tzinfo=KST),
        notify_at=datetime.datetime(2026, 9, 14, 21, 50, tzinfo=KST),
        channel="C11111111",
        url="https://meet.example.com/retro",
    )


class OnlineRetroConfigTest(unittest.TestCase):
    def test_config_parses_timezone_aware_schedule(self):
        parsed = parse_online_retro_meetings(
            json.dumps(
                [
                    {
                        "session_name": "6기 1회차",
                        "starts_at": "2026-09-14T22:00:00+09:00",
                        "notify_at": "2026-09-14T21:50:00+09:00",
                        "channel": "C11111111",
                        "url": "https://meet.example.com/retro",
                    }
                ]
            )
        )
        self.assertEqual(parsed, [meeting()])

    def test_config_rejects_missing_timezone_and_non_https_url(self):
        base = {
            "session_name": "6기 1회차",
            "starts_at": "2026-09-14T22:00:00",
            "notify_at": "2026-09-14T21:50:00+09:00",
            "channel": "C11111111",
            "url": "http://meet.example.com/retro",
        }
        with self.assertRaisesRegex(ValueError, "https"):
            parse_online_retro_meetings(json.dumps([base]))
        base["url"] = "https://meet.example.com/retro"
        with self.assertRaisesRegex(ValueError, "시간대"):
            parse_online_retro_meetings(json.dumps([base]))


class OnlineRetroMeetingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings,
                "DATABASE_PATH",
                str(Path(self.temporary.name) / "test.db"),
            )
        )
        self.enterContext(patch.object(settings, "ONLINE_RETRO_MEETINGS", [meeting()]))
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(),
            chat_postEphemeral=AsyncMock(),
            views_open=AsyncMock(),
        )

    async def test_announcement_posts_once_and_contains_both_buttons(self):
        await post_online_retro_announcement(self.client, meeting())
        await post_online_retro_announcement(self.client, meeting())
        self.client.chat_postMessage.assert_awaited_once()
        message = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C11111111")
        actions = message["blocks"][1]["elements"]
        self.assertEqual(
            [action["action_id"] for action in actions],
            ["attend_online_retro_meeting", "open_online_retro_meeting"],
        )

    async def test_attendance_keeps_first_click_only(self):
        body = {
            "actions": [
                {
                    "value": json.dumps(
                        {
                            "session_name": meeting().session_name,
                            "team_channel": meeting().channel,
                        },
                        ensure_ascii=False,
                    )
                }
            ],
            "user": {"id": "U11111111"},
            "channel": {"id": "C11111111"},
        }
        first_ack = AsyncMock()
        second_ack = AsyncMock()
        await handle_online_retro_attendance(first_ack, body, self.client)
        await handle_online_retro_attendance(second_ack, body, self.client)

        first_ack.assert_awaited_once()
        second_ack.assert_awaited_once()
        with get_connection() as connection:
            records = connection.execute(
                "SELECT session_name, team_channel, user_id FROM online_retro_attendance"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in records],
            [("6기 1회차", "C11111111", "U11111111")],
        )
        notices = [call.kwargs["text"] for call in self.client.chat_postEphemeral.await_args_list]
        self.assertIn("기록했어요", notices[0])
        self.assertIn("이미 기록", notices[1])

    def test_blocks_do_not_expose_url_in_message_text(self):
        blocks = build_online_retro_blocks(meeting())
        self.assertNotIn(meeting().url, blocks[0]["text"]["text"])
        self.assertEqual(blocks[1]["elements"][1]["url"], meeting().url)

    async def test_writing_and_time_up_phases_each_post_once(self):
        await post_writing_phase(self.client, meeting())
        await post_writing_phase(self.client, meeting())
        await post_writing_time_up(self.client, meeting())
        await post_writing_time_up(self.client, meeting())
        self.assertEqual(self.client.chat_postMessage.await_count, 2)
        writing_actions = build_writing_blocks(meeting())[1]["elements"]
        self.assertEqual(writing_actions[0]["action_id"], "start_retrospective_from_announcement")
        self.assertEqual(
            build_time_up_blocks(meeting())[1]["elements"][0]["action_id"],
            "start_online_retro_sharing",
        )

    async def test_only_checked_in_participant_starts_sharing(self):
        await handle_online_retro_attendance(
            AsyncMock(),
            {
                "actions": [{"value": json.dumps({
                    "session_name": "6기 1회차",
                    "team_channel": "C11111111",
                })}],
                "user": {"id": "U11111111"},
                "channel": {"id": "C11111111"},
            },
            self.client,
        )
        action_body = {
            "actions": [{"value": json.dumps({
                "session_name": "6기 1회차",
                "team_channel": "C11111111",
            })}],
            "user": {"id": "U22222222"},
            "channel": {"id": "C11111111"},
        }
        await handle_start_online_retro_sharing(AsyncMock(), action_body, self.client)
        self.client.chat_postMessage.assert_not_awaited()
        self.assertIn(
            "출석 체크",
            self.client.chat_postEphemeral.await_args.kwargs["text"],
        )

        action_body["user"]["id"] = "U11111111"
        await handle_start_online_retro_sharing(AsyncMock(), action_body, self.client)
        message = self.client.chat_postMessage.await_args.kwargs
        self.assertIn("<@U11111111>", message["blocks"][0]["text"]["text"])
        self.assertEqual(
            message["blocks"][1]["elements"][0]["action_id"],
            "start_online_retro_photo",
        )

    async def test_photo_upload_modal_posts_selected_slack_file(self):
        value = json.dumps({
            "session_name": "6기 1회차",
            "team_channel": "C11111111",
        })
        await handle_open_online_retro_photo_upload(
            AsyncMock(),
            {
                "actions": [{"value": value}],
                "trigger_id": "trigger-1",
                "user": {"id": "U11111111"},
                "channel": {"id": "C11111111"},
            },
            self.client,
        )
        modal = self.client.views_open.await_args.kwargs["view"]
        self.assertEqual(modal["callback_id"], "online_retro_photo_submit")
        self.assertEqual(modal["blocks"][0]["element"]["type"], "file_input")

        modal["state"] = {
            "values": {
                "meeting_photo": {
                    "meeting_photo_input": {"files": [{"id": "F11111111"}]}
                }
            }
        }
        await handle_online_retro_photo_submit(
            AsyncMock(),
            {"user": {"id": "U11111111"}},
            self.client,
            modal,
        )
        image = self.client.chat_postMessage.await_args.kwargs["blocks"][1]
        self.assertEqual(image["slack_file"]["id"], "F11111111")


class OnlineRetroPollTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings,
                "DATABASE_PATH",
                str(Path(self.temporary.name) / "poll.db"),
            )
        )
        self.enterContext(
            patch.object(
                settings,
                "SUBMISSION_DESTINATIONS",
                {"U11111111": "C11111111"},
            )
        )
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
            chat_update=AsyncMock(),
            chat_postEphemeral=AsyncMock(),
            views_open=AsyncMock(),
        )

    async def test_functional_poll_posts_once_and_updates_vote_counts(self):
        poll = await post_team_time_poll(
            self.client,
            channel="C11111111",
            team_name="그린팀",
            meeting_date=datetime.date(2026, 10, 11),
            session_name="6기 5회차",
            is_test=False,
        )
        await post_team_time_poll(
            self.client,
            channel="C11111111",
            team_name="그린팀",
            meeting_date=datetime.date(2026, 10, 11),
            session_name="6기 5회차",
            is_test=False,
        )
        self.client.chat_postMessage.assert_awaited_once()

        await handle_open_time_poll(
            AsyncMock(),
            {
                "actions": [{"value": str(poll["id"])}],
                "trigger_id": "trigger-1",
                "user": {"id": "U11111111"},
                "channel": {"id": "C11111111"},
            },
            self.client,
        )
        modal = self.client.views_open.await_args.kwargs["view"]
        modal["state"] = {
            "values": {
                "available_slots": {
                    "available_slots_input": {
                        "selected_options": [
                            {"value": "19:00~20:00"},
                            {"value": "20:00~21:00"},
                        ]
                    }
                }
            }
        }
        await handle_time_poll_submit(
            AsyncMock(), {"user": {"id": "U11111111"}}, self.client, modal
        )
        updated = self.client.chat_update.await_args.kwargs["blocks"]
        counts = updated[1]["text"]["text"]
        self.assertIn("19:00~20:00 · *1명*", counts)
        self.assertIn("20:00~21:00 · *1명*", counts)
        with get_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM online_retro_time_votes").fetchone()[0],
                1,
            )

    async def test_production_poll_rejects_other_team_member(self):
        poll = await post_team_time_poll(
            self.client,
            channel="C11111111",
            team_name="그린팀",
            meeting_date=datetime.date(2026, 10, 11),
            session_name="6기 5회차",
        )
        await handle_open_time_poll(
            AsyncMock(),
            {
                "actions": [{"value": str(poll["id"])}],
                "trigger_id": "trigger-1",
                "user": {"id": "U22222222"},
                "channel": {"id": "C11111111"},
            },
            self.client,
        )
        self.client.views_open.assert_not_awaited()
        self.assertIn("자신이 배정된 팀", self.client.chat_postEphemeral.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
