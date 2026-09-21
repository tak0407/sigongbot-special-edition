import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from database.sqlite import get_connection, initialize_database
from slack.events import command_suggestion as suggestion_event
from slack.events import view_retrospective_submit as submit_event


class SuggestionSlackTest(unittest.IsolatedAsyncioTestCase):
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
        self.enterContext(patch.object(settings, "ADMIN_CHANNEL", "C99999999"))
        initialize_database()

    def view(self) -> dict:
        view = suggestion_event.build_suggestion_view("C11111111")
        view["state"] = {
            "values": {
                "category": {
                    "category_input": {
                        "selected_option": {"value": "feature"}
                    }
                },
                "content": {
                    "content_input": {"value": "제안 내용을 자세히 적었습니다."}
                },
            }
        }
        return view

    async def submit(self, client):
        ack = AsyncMock()
        view = self.view()
        body = {"user": {"id": "U11111111"}, "view": view}
        await suggestion_event.handle_view_suggestion_submit(
            ack, body, client, view
        )
        return ack

    def rows(self) -> list[dict]:
        with closing(get_connection()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM bot_improvement_suggestions ORDER BY id"
                )
            ]

    async def test_command_opens_discoverable_suggestion_modal(self):
        ack = AsyncMock()
        client = SimpleNamespace(views_open=AsyncMock())
        await suggestion_event.handle_command_suggestion(
            ack,
            {"trigger_id": "trigger", "channel_id": "C11111111"},
            client,
        )
        ack.assert_awaited_once()
        view = client.views_open.await_args.kwargs["view"]
        self.assertEqual(view["callback_id"], "bot_improvement_suggestion_submit")
        self.assertIn("C11111111", view["private_metadata"])

    async def test_normal_submission_is_saved_before_admin_notification(self):
        async def assert_saved_first(**kwargs):
            self.assertEqual(len(self.rows()), 1)
            return {"ts": "123.456"}

        client = SimpleNamespace(
            chat_postMessage=AsyncMock(side_effect=assert_saved_first),
            chat_postEphemeral=AsyncMock(),
        )
        ack = await self.submit(client)
        ack.assert_awaited_once_with()
        row = self.rows()[0]
        self.assertEqual(row["category"], "feature")
        self.assertEqual(row["user_id"], "U11111111")
        self.assertEqual(row["submission_channel"], "C11111111")
        self.assertEqual(row["status"], "pending")
        client.chat_postMessage.assert_awaited_once()
        self.assertIn("접수됐어요", client.chat_postEphemeral.await_args.kwargs["text"])

    async def test_database_failure_does_not_notify_admin(self):
        client = SimpleNamespace(
            chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock()
        )
        with patch.object(
            suggestion_event,
            "create_suggestion",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            ack = await self.submit(client)
        client.chat_postMessage.assert_not_awaited()
        client.chat_postEphemeral.assert_not_awaited()
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.assertIn("작성 내용은 유지", ack.await_args.kwargs["errors"]["content"])
        self.assertEqual(self.rows(), [])

    async def test_admin_notification_failure_preserves_saved_suggestion(self):
        client = SimpleNamespace(
            chat_postMessage=AsyncMock(side_effect=RuntimeError("slack down")),
            chat_postEphemeral=AsyncMock(),
        )
        ack = await self.submit(client)
        ack.assert_awaited_once_with()
        self.assertEqual(len(self.rows()), 1)
        notice = client.chat_postEphemeral.await_args.kwargs["text"]
        self.assertIn("안전하게 저장", notice)
        self.assertIn("관리자 알림", notice)


if __name__ == "__main__":
    unittest.main()


class SuggestionEntryFromSubmitTest(unittest.IsolatedAsyncioTestCase):
    """제출 직후 안내와 그 버튼이 여는 모달."""

    async def test_button_opens_the_same_modal_as_the_command(self):
        client = AsyncMock()
        ack = AsyncMock()
        body = {"trigger_id": "T1", "channel": {"id": "C11111111"}}

        await suggestion_event.handle_open_suggestion_modal(ack, body, client)

        ack.assert_awaited_once()
        opened = client.views_open.await_args.kwargs["view"]
        # /제안과 같은 모달이어야 제출 처리 핸들러가 그대로 받는다.
        self.assertEqual(
            opened["callback_id"], "bot_improvement_suggestion_submit"
        )
        self.assertEqual(
            opened, suggestion_event.build_suggestion_view("C11111111")
        )

    async def test_button_survives_a_body_without_channel(self):
        """채널 정보가 없는 인터랙션에서도 모달은 열려야 한다."""
        client = AsyncMock()
        await suggestion_event.handle_open_suggestion_modal(
            AsyncMock(), {"trigger_id": "T1"}, client
        )
        self.assertTrue(client.views_open.await_count)

    async def test_offer_is_private_and_carries_the_button(self):
        client = AsyncMock()
        await submit_event._offer_suggestion(
            client, channel="C11111111", user_id="U11111111"
        )

        kwargs = client.chat_postEphemeral.await_args.kwargs
        # 공개 게시물이 아니라 작성자에게만 보이는 안내다.
        self.assertEqual(kwargs["user"], "U11111111")
        self.assertEqual(kwargs["channel"], "C11111111")
        action_ids = [
            element["action_id"]
            for block in kwargs["blocks"]
            if block["type"] == "actions"
            for element in block["elements"]
        ]
        self.assertIn(suggestion_event.OPEN_SUGGESTION_ACTION_ID, action_ids)
        # 공통 헬퍼가 닫기 버튼을 같은 actions 블록에 넣어 준다.
        self.assertIn("dismiss_ephemeral", action_ids)

    async def test_offer_failure_never_breaks_a_posted_retrospective(self):
        """회고는 이미 게시됐으므로 안내 실패가 제출을 되돌리면 안 된다."""
        client = AsyncMock()
        client.chat_postEphemeral.side_effect = RuntimeError("channel_not_found")
        await submit_event._offer_suggestion(
            client, channel="C11111111", user_id="U11111111"
        )
