import json
import importlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

from config import parse_submission_teams, settings
from database.sqlite import get_connection, initialize_database
from slack.events import test_announcement as announcement
from slack.events import view_retrospective_submit as submission
from slack.events.command_retrospective import build_retrospective_view


class SubmissionRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logger.disable("database.retrospective")
        logger.disable("slack.events.view_retrospective_submit")
        self.addCleanup(logger.enable, "database.retrospective")
        self.addCleanup(logger.enable, "slack.events.view_retrospective_submit")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.teams = {
            "C11111111": [f"U{i:08d}" for i in range(1, 7)],
            "C22222222": [f"U{i:08d}" for i in range(7, 14)],
            "C33333333": [f"U{i:08d}" for i in range(14, 20)],
        }
        for name, value in {
            "DATABASE_PATH": str(Path(self.temporary.name) / "test.db"),
            "SUBMISSION_DESTINATIONS": parse_submission_teams(json.dumps(self.teams)),
            "TEST_SUBMISSION_CHANNEL": "",
            "SESSION_NAME_OVERRIDE": "",
            "ADMIN_IDS": ["U00000001"],
        }.items():
            self.enterContext(patch.object(settings, name, value))
        self.enterContext(patch.object(submission, "cleanup_temp_files"))
        self.save_temp = self.enterContext(patch.object(submission, "save_temp_retrospective"))
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
            chat_postEphemeral=AsyncMock(),
            views_update=AsyncMock(),
        )

    async def submit(self, user, *, guided=False, session="테스트 회차"):
        view = build_retrospective_view(
            channel_id="C99999999", session_name=session,
            test_mode=True, guided_flow_id="test-flow" if guided else None,
        )
        view["state"] = {"values": {
            field: {field + "_input": {"value": "작성한 회고"}}
            for field in ("good_points", "improvements", "learnings", "action_item")
        }}
        view["state"]["values"]["calendar_image"] = {
            "calendar_image_input": {"files": [{"id": "F11111111"}]}
        }
        ack = AsyncMock()
        await submission.handle_view_retrospective_submit(
            ack, {"user": {"id": user}, "view": view}, self.client, view
        )
        return ack

    async def test_all_19_members_direct_and_guided_share_destination(self):
        for guided in (False, True):
            for channel, members in self.teams.items():
                for user in members:
                    self.client.chat_postMessage.reset_mock()
                    ack = await self.submit(user, guided=guided)
                    ack.assert_awaited_once_with()
                    calls = self.client.chat_postMessage.await_args_list
                    self.assertEqual([call.kwargs["channel"] for call in calls], [channel])
                    with closing(get_connection()) as connection:
                        row = connection.execute("SELECT * FROM retrospectives ORDER BY id DESC LIMIT 1").fetchone()
                        self.assertEqual(row["user_id"], user)
                        self.assertEqual(row["slack_channel"], channel)
                        self.assertEqual(row["slack_ts"], "123.456")

    async def test_unknown_member_keeps_modal_and_posts_nothing(self):
        ack = await self.submit("U99999999")
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.client.chat_postMessage.assert_not_awaited()
        self.save_temp.assert_not_called()
        with closing(get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM retrospectives").fetchone()[0], 0)

    async def test_empty_configuration_blocks_submission(self):
        with patch.object(settings, "SUBMISSION_DESTINATIONS", {}):
            ack = await self.submit("U00000001")
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.client.chat_postMessage.assert_not_awaited()

    async def test_post_failure_preserves_draft_and_does_not_write_db(self):
        self.client.chat_postMessage.side_effect = RuntimeError("simulated failure")
        await self.submit("U00000001")
        self.save_temp.assert_called_once()
        self.assertEqual(self.client.chat_postEphemeral.await_args.kwargs["channel"], "C11111111")
        with closing(get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM retrospectives").fetchone()[0], 0)

    async def test_test_override_does_not_redirect_production(self):
        with patch.object(settings, "TEST_SUBMISSION_CHANNEL", "C88888888"):
            await self.submit("U00000001")
            self.assertEqual(self.client.chat_postMessage.await_args.kwargs["channel"], "C88888888")
            await self.submit("U00000001", session="운영 1회차")
            self.assertEqual(self.client.chat_postMessage.await_args.kwargs["channel"], "C11111111")

    async def test_authorized_chooser_can_select_team_channel(self):
        with patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", ["U00000001"]):
            view = build_retrospective_view(
                channel_id="C33333333",
                session_name="운영 1회차",
                test_mode=False,
            )
            view["state"] = {"values": {
                field: {field + "_input": {"value": "작성한 회고"}}
                for field in ("good_points", "improvements", "learnings", "action_item")
            }}
            view["state"]["values"]["calendar_image"] = {
                "calendar_image_input": {"files": []}
            }
            await submission.handle_view_retrospective_submit(
                AsyncMock(), {"user": {"id": "U00000001"}, "view": view}, self.client, view
            )
        self.assertEqual(self.client.chat_postMessage.await_args.kwargs["channel"], "C33333333")

    def test_channel_chooser_view_lists_team_channels(self):
        options = [
            {"text": {"type": "plain_text", "text": "#blue-team"}, "value": "C11111111"},
            {"text": {"type": "plain_text", "text": "#green-team"}, "value": "C22222222"},
        ]
        view = announcement.build_method_selection_view(
            {"channel_id": "C99999999", "session_name": "운영 1회차"}, options
        )
        channel_block = next(block for block in view["blocks"] if block.get("block_id") == "submission_channel")
        self.assertEqual(channel_block["element"]["options"], options)

    async def test_announcement_has_one_button_in_source_channel(self):
        await announcement.handle_post_test_announcement(
            AsyncMock(), {"user": {"id": "U00000001"}, "view": {"private_metadata": "C99999999"}}, self.client
        )
        self.client.chat_postMessage.assert_awaited_once()
        message = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C99999999")
        buttons = [element for block in message["blocks"] for element in block.get("elements", []) if element["type"] == "button"]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(json.loads(buttons[0]["value"])["channel_id"], "C99999999")

    def test_invalid_or_duplicate_assignment_is_rejected(self):
        for value in ('[]', 'invalid', '{"bad": ["U11111111"]}',
                      '{"C11111111": []}', '{"C11111111": ["이름"]}',
                      '{"C11111111": ["U11111111"], "C22222222": ["U11111111"]}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_submission_teams(value)

    async def test_health_http_without_socket_connection(self):
        # 실제 Slack 앱 생성 및 Socket Mode 연결 없이 health 핸들러만 검사한다.
        with patch.dict("sys.modules", {"slack.event_handler": SimpleNamespace(app=None)}):
            main = importlib.import_module("main")
        app = web.Application()
        app.router.add_get("/health", main.health_check)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/health")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "OK")


if __name__ == "__main__":
    unittest.main()
