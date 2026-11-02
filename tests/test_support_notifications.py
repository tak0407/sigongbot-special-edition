import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Settings, settings
from slack.events import message as message_event


class SupportNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(settings, "SUPPORT_CHANNEL", "C11111111"))
        self.enterContext(patch.object(settings, "ADMIN_CHANNEL", "C99999999"))
        self.enterContext(patch.object(settings, "ADMIN_IDS", ["U44444444"]))
        self.enterContext(
            patch.object(
                settings,
                "SUPPORT_NOTIFICATION_USER_IDS",
                ["U22222222", "U33333333"],
                create=True,
            )
        )

    def event(self, **overrides):
        return {
            "type": "message",
            "channel": "C11111111",
            "user": "U11111111",
            "text": "<!channel> 문의 내용을 확인해주세요.",
            "ts": "123.456",
            **overrides,
        }

    def client(self):
        async def open_dm(*, users):
            return {"ok": True, "channel": {"id": {
                "U22222222": "D22222222",
                "U33333333": "D33333333",
            }[users]}}

        return SimpleNamespace(
            conversations_open=AsyncMock(side_effect=open_dm),
            chat_postMessage=AsyncMock(return_value={"ok": True, "ts": "456.789"}),
        )

    async def handle(self, client, **overrides):
        ack = AsyncMock()
        await message_event.handle_message(
            ack, {"event": self.event(**overrides)}, AsyncMock(), client
        )
        ack.assert_awaited_once_with()

    async def test_new_inquiry_is_sent_to_each_configured_dm(self):
        client = self.client()
        await self.handle(client)
        self.assertEqual(
            [call.kwargs["users"] for call in client.conversations_open.await_args_list],
            ["U22222222", "U33333333"],
        )
        messages = [call.kwargs for call in client.chat_postMessage.await_args_list]
        self.assertEqual([message["channel"] for message in messages],
                         ["D22222222", "D33333333"])
        for message in messages:
            self.assertIn("<@U11111111>", message["text"])
            self.assertIn("<#C11111111>", message["text"])
            self.assertEqual(message["blocks"][1]["text"], {
                "type": "plain_text", "text": "<!channel> 문의 내용을 확인해주세요."
            })

    async def test_opening_one_dm_fails_without_blocking_the_other(self):
        client = self.client()
        client.conversations_open.side_effect = [
            RuntimeError("cannot_dm_user"),
            {"ok": True, "channel": {"id": "D33333333"}},
        ]
        await self.handle(client)
        self.assertEqual(client.conversations_open.await_count, 2)
        client.chat_postMessage.assert_awaited_once()
        self.assertEqual(client.chat_postMessage.await_args.kwargs["channel"], "D33333333")

    async def test_posting_one_dm_fails_without_blocking_the_other(self):
        client = self.client()
        client.chat_postMessage.side_effect = [RuntimeError("slack down"), {"ok": True}]
        await self.handle(client)
        self.assertEqual(
            [call.kwargs["channel"] for call in client.chat_postMessage.await_args_list],
            ["D22222222", "D33333333"],
        )

    async def test_duplicate_recipients_receive_only_one_dm(self):
        client = self.client()
        with patch.object(settings, "SUPPORT_NOTIFICATION_USER_IDS", ["U22222222"] * 2):
            await self.handle(client)
        client.conversations_open.assert_awaited_once_with(users="U22222222")
        client.chat_postMessage.assert_awaited_once()

    async def test_no_recipients_does_not_send_to_the_admin_channel(self):
        client = self.client()
        with patch.object(settings, "SUPPORT_NOTIFICATION_USER_IDS", []):
            await self.handle(client)
        client.conversations_open.assert_not_awaited()
        client.chat_postMessage.assert_not_awaited()

    async def test_replies_edits_bots_and_other_channels_do_not_trigger_an_inquiry(self):
        for overrides in [
            {"channel": "C55555555"},
            {"thread_ts": "100.200"},
            {"subtype": "message_changed"},
            {"subtype": "bot_message"},
            {"bot_id": "B11111111"},
        ]:
            with self.subTest(overrides=overrides):
                client = self.client()
                await self.handle(client, **overrides)
                client.conversations_open.assert_not_awaited()
                client.chat_postMessage.assert_not_awaited()

    async def test_long_inquiry_stays_within_slack_plain_text_block_limit(self):
        client = self.client()
        await self.handle(client, text="긴 문의 내용입니다. " * 500)
        messages = [call.kwargs for call in client.chat_postMessage.await_args_list]
        self.assertEqual(len(messages), 2)
        for message in messages:
            content = message["blocks"][1]["text"]
            self.assertEqual(content["type"], "plain_text")
            self.assertLessEqual(len(content["text"]), 3000)
            self.assertTrue(content["text"].startswith("긴 문의 내용입니다."))


class SupportNotificationConfigTest(unittest.TestCase):
    def test_recipients_accept_both_formats_without_changing_admin_privileges(self):
        for value in ["U22222222,U33333333", '["U22222222", "U33333333"]']:
            with self.subTest(value=value), patch.dict(os.environ, {
                "ENV": "prod",
                "ADMIN_IDS": "U44444444",
                "SUPPORT_NOTIFICATION_USER_IDS": value,
            }, clear=True):
                configured = Settings()
                self.assertEqual(configured.SUPPORT_NOTIFICATION_USER_IDS,
                                 ["U22222222", "U33333333"])
                self.assertEqual(configured.ADMIN_IDS, ["U44444444"])
