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
