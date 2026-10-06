import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import OnlineRetroMeeting, parse_online_retro_meetings, settings
from database.online_retro_poll import message_polls
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
    UNAVAILABLE,
    PollPost,
    build_poll_blocks,
    handle_mark_unavailable,
    handle_open_time_poll,
    handle_time_poll_submit,
    post_time_poll,
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
        # 그린팀(C11111111) 2명, 블루팀(C22222222) 1명.
        self.enterContext(
            patch.object(
                settings,
                "SUBMISSION_DESTINATIONS",
                {"U11111111": "C11111111", "U33333333": "C11111111", "U22222222": "C22222222"},
            )
        )
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
            chat_update=AsyncMock(),
            chat_postEphemeral=AsyncMock(),
            views_open=AsyncMock(),
        )

    async def _post(self, teams=(("C11111111", "그린팀"), ("C22222222", "블루팀"))):
        return await post_time_poll(
            self.client,
            teams=list(teams),
            meeting_date=datetime.date(2026, 10, 11),
            session_name="6기 5회차",
            message_channel="C0ANNOUNCE",
        )

    async def _polls(self) -> dict[str, dict]:
        return {
            poll["team_name"]: poll
            for poll in await message_polls(meeting_date="2026-10-11", is_test=False)
        }

    def _action(self, anchor: dict, user_id: str = "U11111111") -> dict:
        return {
            "actions": [{"value": str(anchor["id"])}],
            "trigger_id": "trigger-1",
            "user": {"id": user_id},
            "channel": {"id": "C0ANNOUNCE"},
        }

    async def _vote(self, anchor: dict, slots: list[str], user_id: str = "U11111111") -> dict:
        await handle_open_time_poll(AsyncMock(), self._action(anchor, user_id), self.client)
        modal = self.client.views_open.await_args.kwargs["view"]
        modal["state"] = {
            "values": {
                "available_slots": {
                    "available_slots_input": {
                        "selected_options": [{"value": slot} for slot in slots]
                    }
                }
            }
        }
        self.submit_ack = AsyncMock()
        await handle_time_poll_submit(self.submit_ack, {"user": {"id": user_id}}, self.client, modal)
        return modal

    def _progress(self, method: AsyncMock) -> str:
        return method.await_args.kwargs["blocks"][1]["text"]["text"]

    def _notice(self) -> str:
        """마지막으로 띄운 안내 창의 본문. 안내는 채널 메시지가 아니라 창으로 보인다."""
        view = self.client.views_open.await_args.kwargs["view"]
        self.assertNotIn("callback_id", view)
        return view["blocks"][0]["text"]["text"]

    async def test_one_announcement_carries_every_team_and_posts_once(self):
        self.assertEqual(await self._post(), PollPost(added=["그린팀", "블루팀"], new_message=True))
        self.assertEqual(await self._post(), PollPost(added=[], new_message=False))
        self.client.chat_postMessage.assert_awaited_once()
        self.assertEqual(self.client.chat_postMessage.await_args.kwargs["channel"], "C0ANNOUNCE")
        polls = await self._polls()
        self.assertEqual({poll["slack_ts"] for poll in polls.values()}, {"123.456"})
        self.assertEqual({poll["message_channel"] for poll in polls.values()}, {"C0ANNOUNCE"})
        self.assertIn("그린팀 0/2 · 블루팀 0/1", self._progress(self.client.chat_postMessage))

    async def test_vote_goes_to_the_voters_own_team(self):
        await self._post()
        polls = await self._polls()
        # 버튼에는 공지의 첫 팀(그린팀) 투표 ID가 들어 있다. 블루팀원이 눌러도 블루팀 투표로 연다.
        modal = await self._vote(polls["그린팀"], ["19:00~20:00", "20:00~21:00"], "U22222222")
        self.assertEqual(modal["private_metadata"], str(polls["블루팀"]["id"]))
        self.assertIn("블루팀 · 10월 11일(일)", modal["blocks"][0]["label"]["text"])
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT poll_id, user_id FROM online_retro_time_votes"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(polls["블루팀"]["id"], "U22222222")])
        update = self.client.chat_update.await_args.kwargs
        self.assertEqual((update["channel"], update["ts"]), ("C0ANNOUNCE", "123.456"))
        self.assertIn("그린팀 0/2 · 블루팀 1/1", self._progress(self.client.chat_update))
        # 같은 창이 저장 결과로 바뀌고, 채널에는 나에게만 보이는 메시지를 남기지 않는다.
        saved = self.submit_ack.await_args.kwargs
        self.assertEqual(saved["response_action"], "update")
        self.assertIn("블루팀 투표에 저장했어요", saved["view"]["blocks"][0]["text"]["text"])
        self.assertIn("19:00~20:00, 20:00~21:00", saved["view"]["blocks"][0]["text"]["text"])
        self.client.chat_postEphemeral.assert_not_awaited()

    async def test_test_announcement_routes_by_team_and_lets_anyone_try(self):
        await post_time_poll(
            self.client,
            teams=[("C11111111", "그린팀"), ("C22222222", "블루팀")],
            meeting_date=datetime.date(2026, 10, 11),
            session_name="6기 5회차",
            message_channel="C0TEST",
            is_test=True,
        )
        polls = {
            poll["team_name"]: poll
            for poll in await message_polls(meeting_date="2026-10-11", is_test=True)
        }
        self.assertIn("[테스트]", self.client.chat_postMessage.await_args.kwargs["blocks"][0]["text"]["text"])
        modal = await self._vote(polls["그린팀"], ["20:00~21:00"], "U22222222")
        self.assertEqual(modal["private_metadata"], str(polls["블루팀"]["id"]))
        # 팀 배정이 없는 사람도 테스트 공지는 첫 팀 투표로 눌러 볼 수 있다.
        modal = await self._vote(polls["그린팀"], ["21:00~22:00"], "U99999999")
        self.assertEqual(modal["private_metadata"], str(polls["그린팀"]["id"]))
        self.assertIn("그린팀 1/2 · 블루팀 1/1", self._progress(self.client.chat_update))

    async def test_member_without_a_team_cannot_vote(self):
        await self._post()
        anchor = (await self._polls())["그린팀"]
        await handle_open_time_poll(AsyncMock(), self._action(anchor, "U99999999"), self.client)
        self.assertIn("팀 배정이 없어", self._notice())
        await handle_mark_unavailable(AsyncMock(), self._action(anchor, "U99999999"), self.client)
        self.assertIn("팀 배정이 없어", self._notice())
        self.client.chat_update.assert_not_awaited()
        self.client.chat_postEphemeral.assert_not_awaited()

    async def test_team_missing_from_the_announcement_is_added_to_it(self):
        await self._post(teams=[("C11111111", "그린팀")])
        anchor = (await self._polls())["그린팀"]
        # 아직 공지에 붙지 않은 팀은 투표할 수 없다.
        await handle_open_time_poll(AsyncMock(), self._action(anchor, "U22222222"), self.client)
        self.assertIn("팀 배정이 없어", self._notice())

        self.assertEqual(await self._post(), PollPost(added=["블루팀"], new_message=False))
        self.client.chat_postMessage.assert_awaited_once()
        self.assertEqual((await self._polls())["블루팀"]["slack_ts"], "123.456")
        self.assertIn("블루팀 0/1", self._progress(self.client.chat_update))

    async def test_modal_shows_checkboxes_with_the_previous_choice_checked(self):
        await self._post()
        anchor = (await self._polls())["그린팀"]
        await handle_open_time_poll(AsyncMock(), self._action(anchor), self.client)
        element = self.client.views_open.await_args.kwargs["view"]["blocks"][0]["element"]
        self.assertEqual(element["type"], "checkboxes")
        self.assertNotIn(UNAVAILABLE, [option["value"] for option in element["options"]])
        self.assertNotIn("initial_options", element)

        await self._vote(anchor, ["19:00~20:00", "21:00~22:00"])
        await handle_open_time_poll(AsyncMock(), self._action(anchor), self.client)
        element = self.client.views_open.await_args.kwargs["view"]["blocks"][0]["element"]
        self.assertEqual(
            [option["value"] for option in element["initial_options"]],
            ["19:00~20:00", "21:00~22:00"],
        )

    async def test_empty_choice_points_to_the_unavailable_button(self):
        await self._post()
        anchor = (await self._polls())["그린팀"]
        ack = AsyncMock()
        await handle_open_time_poll(AsyncMock(), self._action(anchor), self.client)
        modal = self.client.views_open.await_args.kwargs["view"]
        modal["state"] = {
            "values": {"available_slots": {"available_slots_input": {"selected_options": []}}}
        }
        await handle_time_poll_submit(ack, {"user": {"id": "U11111111"}}, self.client, modal)
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.assertIn("이번엔 어려워요", ack.await_args.kwargs["errors"]["available_slots"])
        with get_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM online_retro_time_votes").fetchone()[0],
                0,
            )

    async def test_unavailable_button_answers_in_one_click(self):
        await self._post()
        anchor = (await self._polls())["그린팀"]
        await self._vote(anchor, ["19:00~20:00"])
        await handle_mark_unavailable(AsyncMock(), self._action(anchor), self.client)

        with get_connection() as connection:
            stored = connection.execute(
                "SELECT slots_json FROM online_retro_time_votes WHERE user_id = 'U11111111'"
            ).fetchone()[0]
        self.assertEqual(json.loads(stored), [UNAVAILABLE])
        self.assertIn("그린팀 1/2", self._progress(self.client.chat_update))
        self.assertIn("그린팀 투표에 이번엔 어렵다고", self._notice())
        self.client.chat_postEphemeral.assert_not_awaited()

    def test_message_shows_team_progress_not_slot_counts(self):
        polls = [
            {"id": 1, "team_name": "그린팀", "team_channel": "C11111111",
             "meeting_date": "2026-10-11", "is_test": 0, "voters": 2},
            {"id": 2, "team_name": "블루팀", "team_channel": "C22222222",
             "meeting_date": "2026-10-11", "is_test": 0, "voters": 0},
        ]
        blocks = build_poll_blocks(polls)
        self.assertIn("10월 11일(일)", blocks[0]["text"]["text"])
        self.assertIn("그린팀 2/2 · 블루팀 0/1", blocks[1]["text"]["text"])
        self.assertNotIn("18:00~19:00", json.dumps(blocks, ensure_ascii=False))
        self.assertEqual(blocks[2]["elements"][0]["value"], "1")

        test = build_poll_blocks([{**polls[0], "is_test": 1, "voters": 1}])
        self.assertIn("[테스트]", test[0]["text"]["text"])
        self.assertIn("그린팀 1/2", test[1]["text"]["text"])
        # 테스트 채널처럼 배정된 팀원이 없는 투표는 응답 수만 보인다.
        legacy = build_poll_blocks([{**polls[0], "team_channel": "C0TEST", "voters": 3}])
        self.assertIn("그린팀 3명", legacy[1]["text"]["text"])

        edited = build_poll_blocks([{**polls[0], "intro_template": "{날짜} {모임}"}])
        # 정해 둔 치환자만 바꾸고 다른 중괄호는 그대로 둔다.
        self.assertEqual(edited[0]["text"]["text"], "10월 11일(일) {모임}")

if __name__ == "__main__":
    unittest.main()
