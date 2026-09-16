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
