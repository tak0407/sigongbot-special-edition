"""ENV=prod python -m unittest discover -s tests -v (no Slack or AI connection)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from slack_sdk.models.views import View

from ai_review.formatter import SCHEMA, format_guided_answers
from config import settings
from database.guided_reflection import (
    create_guided_reflection,
    get_guided_reflection,
    go_to_previous_question,
    save_guided_answer,
)
from database.sqlite import initialize_database
from reflection_questions import select_reflection_questions
from slack.events import test_announcement as guided
from slack.events.command_retrospective import build_retrospective_view
from slack.events.view_retrospective_submit import handle_view_retrospective_submit


def payload(flow, answers):
    view = guided.build_guided_question_view(flow)
    View(**view).validate_json()
    inputs = [block for block in view["blocks"] if block["type"] == "input"]
    assert all(block["optional"] for block in inputs)
    view["id"] = "test-view"
    view["state"] = {
        "values": {
            block["block_id"]: {"guided_answer_input": {"value": answer}}
            for block, answer in zip(inputs, answers)
        }
    }
    return view


class GuidedReflectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_navigation_and_results(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(settings, "DATABASE_PATH", str(Path(temp) / "test.db")),
        ):
            initialize_database()
            groups = select_reflection_questions()
            self.assertEqual([g["stage"] for g in groups], SCHEMA["required"])
            self.assertTrue(all(len(g["questions"]) == 3 for g in groups))
            selection = guided.build_method_selection_view(
                {
                    "channel_id": "test-channel",
                    "session_name": "test-session",
                }
            )
            View(**selection).validate_json()
            radio = selection["blocks"][0]["element"]
            self.assertEqual(radio["type"], "radio_buttons")
            self.assertIn("베타", radio["options"][1]["text"]["text"])
            selection["state"] = {
                "values": {
                    "retrospective_method": {
                        "method_input": {"selected_option": {"value": "guided"}}
                    }
                }
            }
            ack = AsyncMock()
            client = SimpleNamespace(
                views_update=AsyncMock(), chat_postEphemeral=AsyncMock()
            )
            await guided.handle_method_select(
                ack, {"user": {"id": "test-user"}, "view": selection}, client
            )
            first_view = ack.call_args.kwargs["view"]
            flow_id = first_view["private_metadata"]
            flow = await get_guided_reflection(flow_id)
            self.assertEqual(
                len([b for b in first_view["blocks"] if b["type"] == "input"]), 3
            )

            view = payload(flow, ["작은 성과", None, "다시 할 행동"])
            await guided.handle_guided_submit(
                ack, {"user": {"id": "test-user"}}, client, view
            )
            flow = await get_guided_reflection(flow_id)
            self.assertEqual(flow["answers"][0], ["작은 성과", "", "다시 할 행동"])

            # Back saves drafts; clearing them must not resurrect the old answer.
            body = {
                "user": {"id": "test-user"},
                "actions": [{"value": flow_id}],
                "view": payload(flow, [None, "아쉬운 상황", None]),
            }
            await guided.handle_guided_previous(ack, body, client)
            flow = await get_guided_reflection(flow_id)
            self.assertEqual(flow["current_index"], 0)
            self.assertEqual(flow["answers"][1], ["", "아쉬운 상황", ""])
            flow = await save_guided_answer(
                flow_id=flow_id, user_id="test-user", answer=flow["answers"][0]
            )
            body["view"] = payload(flow, [None, None, None])
            await guided.handle_guided_skip(ack, body, client)
            flow = await get_guided_reflection(flow_id)
            self.assertEqual(flow["answers"][1], ["", "", ""])
            self.assertEqual(flow["current_index"], 2)

            await guided.handle_guided_submit(
                ack,
                {"user": {"id": "test-user"}},
                client,
                payload(flow, ["배움", "근거", ""]),
            )
            flow = await get_guided_reflection(flow_id)
            with patch.object(
                guided, "_finish_guided_formatting", new=AsyncMock()
            ) as finish:
                body["view"] = payload(flow, [None, None, None])
                await guided.handle_guided_skip(ack, body, client)
                await asyncio.sleep(0)
                finish.assert_awaited_once()
            flow = await get_guided_reflection(flow_id)
            with self.assertRaises(ValueError):
                await save_guided_answer(
                    flow_id=flow_id, user_id="test-user", answer=[]
                )
            with (
                patch.object(
                    guided,
                    "format_guided_answers",
                    new=AsyncMock(side_effect=RuntimeError("offline")),
                ),
                patch.object(guided.logger, "exception"),
            ):
                await guided._finish_guided_formatting(client=client, flow_id=flow_id)
            flow = await get_guided_reflection(flow_id)
            self.assertEqual(flow["formatted"]["improvements"], "")
            self.assertEqual(flow["formatted"]["learnings"], "배움\n\n근거")
            self.assertFalse(flow["formatted"]["from_ai"])
            self.assertNotIn("AI가", client.chat_postEphemeral.call_args.kwargs["text"])
            preview = build_retrospective_view(
                channel_id="test-channel",
                session_name="test",
                initial_values=flow["formatted"],
                test_mode=True,
                guided_flow_id=flow_id,
            )
            self.assertTrue(
                all(
                    b["optional"]
                    for b in preview["blocks"]
                    if b.get("block_id") in SCHEMA["required"]
                )
            )
            direct = build_retrospective_view(
                channel_id="test-channel", session_name="test", test_mode=True
            )
            self.assertTrue(
                all(
                    not b["optional"]
                    for b in direct["blocks"]
                    if b.get("block_id") in SCHEMA["required"]
                )
            )
            preview["state"] = {
                "values": {
                    key: {f"{key}_input": {"value": None}} for key in SCHEMA["required"]
                }
            }
            await handle_view_retrospective_submit(
                ack, {"user": {"id": "test-user"}, "view": preview}, client, preview
            )
            self.assertEqual(ack.call_args.kwargs["response_action"], "errors")

            # Existing single-question sessions remain readable after deployment.
            old = await create_guided_reflection(
                user_id="test-user",
                slack_channel="test-channel",
                session_name="test",
                questions=[
                    {"stage": "experience", "label": "경험", "question": "어땠나요?"}
                ],
            )
            self.assertEqual(
                guided._read_guided_answers(payload(old, ["이전 답변"])), "이전 답변"
            )
            await save_guided_answer(
                flow_id=old["flow_id"], user_id="test-user", answer="이전 답변"
            )
            old = await go_to_previous_question(
                flow_id=old["flow_id"], user_id="test-user"
            )
            self.assertEqual(old["answers"], ["이전 답변"])

            empty = await create_guided_reflection(
                user_id="test-user",
                slack_channel="test-channel",
                session_name="test",
                questions=groups,
            )
            for _ in groups:
                empty = await save_guided_answer(
                    flow_id=empty["flow_id"], user_id="test-user", answer=["", "", ""]
                )
            with patch.object(guided, "format_guided_answers", new=AsyncMock()) as ai:
                await guided._finish_guided_formatting(
                    client=client, flow_id=empty["flow_id"]
                )
                ai.assert_not_awaited()
            empty = await get_guided_reflection(empty["flow_id"])
            self.assertTrue(
                all(empty["formatted"][key] == "" for key in SCHEMA["required"])
            )

            selection["state"]["values"]["retrospective_method"]["method_input"][
                "selected_option"
            ]["value"] = "direct"
            await guided.handle_method_select(
                ack, {"user": {"id": "test-user"}, "view": selection}, client
            )
            self.assertEqual(
                ack.call_args.kwargs["view"]["callback_id"], "retrospective_submit"
            )

    async def test_formatter_keeps_skipped_sections_empty(self):
        responses = [
            {
                "stage": "good_points",
                "label": "잘한 점",
                "question": "무엇?",
                "answer": "성과",
            }
        ]
        result = {key: "모델이 채운 내용" for key in SCHEMA["required"]}
        with patch(
            "ai_review.formatter.run_antigravity",
            new=AsyncMock(return_value={"structured_output": result}),
        ):
            formatted = await format_guided_answers(responses)
        self.assertEqual(formatted["good_points"], "모델이 채운 내용")
        self.assertTrue(all(formatted[key] == "" for key in SCHEMA["required"][1:]))


if __name__ == "__main__":
    unittest.main()
