import unittest
from unittest.mock import AsyncMock, Mock, patch

from slack import ephemeral


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.post = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class EphemeralDismissTest(unittest.IsolatedAsyncioTestCase):
    def test_add_dismiss_button_preserves_existing_actions(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "안내"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "continue",
                        "text": {"type": "plain_text", "text": "계속"},
                    }
                ],
            },
        ]

        result = ephemeral.add_dismiss_button(blocks)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["elements"][0]["action_id"], "continue")
        self.assertEqual(result[1]["elements"][1]["action_id"], "dismiss_ephemeral")
        self.assertEqual(result[1]["elements"][1]["text"]["text"], "닫기")
        self.assertEqual(len(blocks[1]["elements"]), 1)

    async def test_text_only_notice_keeps_its_body(self):
        client = AsyncMock()

        await ephemeral.post_ephemeral(
            client, channel="C1", user="U1", text="이미 회고를 공유했어요!"
        )

        blocks = client.chat_postEphemeral.await_args.kwargs["blocks"]
        self.assertEqual(blocks[0]["type"], "section")
        self.assertEqual(blocks[0]["text"]["text"], "이미 회고를 공유했어요!")
        self.assertEqual(blocks[1]["elements"][0]["action_id"], "dismiss_ephemeral")
        self.assertEqual(
            client.chat_postEphemeral.await_args.kwargs["text"],
            "이미 회고를 공유했어요!",
        )

    async def test_oversized_text_falls_back_to_plain_message(self):
        client = AsyncMock()
        text = "가" * (ephemeral._SECTION_TEXT_LIMIT + 1)

        await ephemeral.post_ephemeral(client, channel="C1", user="U1", text=text)

        self.assertIsNone(client.chat_postEphemeral.await_args.kwargs["blocks"])
        self.assertEqual(client.chat_postEphemeral.await_args.kwargs["text"], text)

    async def test_caller_blocks_are_not_duplicated_with_a_section(self):
        client = AsyncMock()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "안내"}}]

        await ephemeral.post_ephemeral(
            client, channel="C1", user="U1", text="안내", blocks=blocks
        )

        sent = client.chat_postEphemeral.await_args.kwargs["blocks"]
        self.assertEqual([block["type"] for block in sent], ["section", "actions"])

    async def test_response_url_requests_original_message_deletion(self):
        response = _FakeResponse(200)
        session = _FakeSession(response)
        with patch.object(ephemeral.aiohttp, "ClientSession", return_value=session) as factory:
            deleted = await ephemeral.delete_ephemeral(
                "https://hooks.slack.test/actions/opaque"
            )

        self.assertTrue(deleted)
        factory.assert_called_once()
        session.post.assert_called_once_with(
            "https://hooks.slack.test/actions/opaque",
            json={"delete_original": True},
        )

    async def test_missing_response_url_is_a_quiet_noop(self):
        with patch.object(ephemeral.aiohttp, "ClientSession") as factory:
            deleted = await ephemeral.delete_ephemeral(None)

        self.assertFalse(deleted)
        factory.assert_not_called()

    async def test_dismiss_action_acks_before_handling_response_url(self):
        ack = AsyncMock()
        with patch.object(ephemeral, "delete_ephemeral", new=AsyncMock()) as delete:
            await ephemeral.handle_dismiss_ephemeral(
                ack,
                {
                    "actions": [{"action_id": "dismiss_ephemeral"}],
                    "response_url": "https://hooks.slack.test/actions/opaque",
                },
            )

        ack.assert_awaited_once_with()
        delete.assert_awaited_once_with("https://hooks.slack.test/actions/opaque")


if __name__ == "__main__":
    unittest.main()
