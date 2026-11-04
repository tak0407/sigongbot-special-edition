import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from slack_sdk.models.views import View

from ai_review import formatter
from config import settings
from database.sqlite import get_connection, initialize_database
from slack.events import action_view_retrospective_detail as detail
from slack.events import command_my_retrospectives as my_retrospectives
from slack.events import test_announcement as selection
from slack.events import view_retrospective_submit as submission
from slack.events.command_retrospective import build_retrospective_view


BODY = "이번 회고 기간에 겪은 경험을 돌아보며 나의 생각과 느낀 점을 적었습니다. " * 5


def block_dicts(blocks):
    return [block.to_dict() if hasattr(block, "to_dict") else block for block in blocks]


class EssayFormTest(unittest.IsolatedAsyncioTestCase):
    def test_essay_form_has_optional_title_and_bounded_body(self):
        view = build_retrospective_view(
            channel_id="C11111111", session_name="테스트 회차", test_mode=True,
            content_format="essay", initial_values={"essay_title": "제목", "essay_body": BODY},
        )
        View(**view).validate_json()
        inputs = {block["block_id"]: block for block in view["blocks"] if block["type"] == "input"}
        self.assertTrue(inputs["essay_title"]["optional"])
        self.assertEqual(inputs["essay_body"]["element"]["min_length"], 100)
        self.assertEqual(inputs["essay_body"]["element"]["max_length"], 3000)
        self.assertEqual(inputs["essay_body"]["element"]["initial_value"], BODY)
        self.assertNotIn("good_points", inputs)
        self.assertTrue(inputs["calendar_image"]["optional"])
        self.assertEqual(json.loads(view["private_metadata"])["content_format"], "essay")

    def test_structured_form_accepts_one_thousand_characters_per_field(self):
        view = build_retrospective_view(channel_id="C11111111", session_name="테스트 회차", test_mode=True)
        for block in view["blocks"]:
            if block["type"] == "input" and block["element"]["type"] == "plain_text_input":
                self.assertEqual(block["element"]["max_length"], 1000)

    async def test_selection_opens_the_essay_form_and_recovers_its_draft(self):
        view = selection.build_method_selection_view({
            "channel_id": "C11111111", "session_name": "테스트 회차", "test_mode": True,
        })
        radio = next(b["element"] for b in view["blocks"] if b.get("block_id") == "retrospective_method")
        self.assertIn("essay", [option["value"] for option in radio["options"]])
        view["state"] = {"values": {"retrospective_method": {
            "method_input": {"selected_option": {"value": "essay"}}
        }}}
        ack = AsyncMock()
        with patch.object(selection, "get_latest_temp_retrospective", return_value={"essay_body": BODY}):
            await selection.handle_method_select(ack, {"user": {"id": "U11111111"}, "view": view}, AsyncMock())
        result = ack.await_args.kwargs
        self.assertEqual(result["response_action"], "update")
        self.assertEqual(json.loads(result["view"]["private_metadata"])["content_format"], "essay")
        body = next(b for b in result["view"]["blocks"] if b.get("block_id") == "essay_body")
        self.assertEqual(body["element"]["initial_value"], BODY)

    def test_guided_fallback_keeps_up_to_one_thousand_characters(self):
        result = selection._fallback_format({
            "questions": [{"stage": "good_points", "questions": ["질문"]}],
            "answers": [["가" * 1100]],
        })
        self.assertEqual(result["good_points"], "가" * 1000)

    async def test_ai_formatter_accepts_a_result_over_five_hundred_characters(self):
        responses = [{"stage": "good_points", "answer": "나의 경험"}]
        with patch.object(formatter, "run_antigravity", new=AsyncMock(return_value={
            "structured_output": {"good_points": "가" * 700, "improvements": "", "learnings": "", "action_item": ""}
        })):
            result = await formatter.format_guided_answers(responses)
        self.assertIn("가" * 700, result["good_points"])


class EssaySubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")))
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", {"U11111111": "C11111111"}))
        self.enterContext(patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", []))
        self.enterContext(patch.object(settings, "TEST_SUBMISSION_CHANNEL", ""))
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", ""))
        self.draft = self.enterContext(patch.object(submission, "save_temp_retrospective"))
        self.enterContext(patch.object(submission, "cleanup_temp_files"))
        initialize_database()

    def view(self, body=BODY, title="나의 회고 <태그>"):
        view = build_retrospective_view(channel_id="C11111111", session_name="6기 에세이 테스트", test_mode=True, content_format="essay")
        view["state"] = {"values": {
            "essay_title": {"essay_title_input": {"value": title}},
            "essay_body": {"essay_body_input": {"value": body}},
        }}
        return view

    def client(self):
        return SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "123.456"}), chat_postEphemeral=AsyncMock())

    def rows(self):
        with get_connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM retrospectives ORDER BY id")]

    async def submit(self, client, view=None):
        view = view or self.view()
        ack = AsyncMock()
        await submission.handle_view_retrospective_submit(ack, {"user": {"id": "U11111111"}, "view": view}, client, view)
        return ack

    async def test_essay_is_saved_before_posting_and_keeps_the_original_plain_text(self):
        client = self.client()
        body = "<!channel> <@U22222222> " + BODY
        async def assert_saved(**kwargs):
            row = self.rows()[0]
            self.assertEqual(row["slack_post_status"], "pending")
            self.assertEqual(row["essay_body"], body)
            return {"ts": "123.456"}
        client.chat_postMessage.side_effect = assert_saved
        ack = await self.submit(client, self.view(body=body))
        ack.assert_awaited_once_with()
        row = self.rows()[0]
        self.assertEqual(row["content_format"], "essay")
        self.assertEqual(row["good_points"], "")
        self.assertEqual(row["slack_post_status"], "posted")
        blocks = block_dicts(client.chat_postMessage.await_args.kwargs["blocks"])
        self.assertIn({"type": "plain_text", "text": body}, [block.get("text") for block in blocks])

    async def test_time_record_image_is_posted_with_the_essay(self):
        client = self.client()
        view = self.view()
        view["state"]["values"]["calendar_image"] = {"calendar_image_input": {"files": [{"id": "F11111111"}]}}
        await self.submit(client, view)
        blocks = block_dicts(client.chat_postMessage.await_args.kwargs["blocks"])
        self.assertEqual(next(block for block in blocks if block["type"] == "image")["slack_file"], {"id": "F11111111"})

    async def test_invalid_body_lengths_and_whitespace_are_rejected_before_saving(self):
        for body in ["가" * 99, "가" * 3001, " " * 120]:
            with self.subTest(length=len(body)):
                client = self.client()
                ack = await self.submit(client, self.view(body=body))
                self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
                self.assertIn("essay_body", ack.await_args.kwargs["errors"])
                client.chat_postMessage.assert_not_awaited()
                self.assertEqual(self.rows(), [])

    async def test_minimum_length_is_accepted_and_optional_title_can_be_empty(self):
        client = self.client()
        await self.submit(client, self.view(body="가" * 100, title=None))
        self.assertEqual(self.rows()[0]["essay_body"], "가" * 100)
        self.assertIsNone(self.rows()[0]["essay_title"])

    async def test_maximum_length_is_accepted(self):
        client = self.client()
        await self.submit(client, self.view(body="가" * 3000))
        self.assertEqual(len(self.rows()[0]["essay_body"]), 3000)

    async def test_long_title_reports_the_title_input(self):
        client = self.client()
        ack = await self.submit(client, self.view(title="가" * 151))
        self.assertIn("essay_title", ack.await_args.kwargs["errors"])
        self.assertEqual(self.rows(), [])

    async def test_database_failure_keeps_the_draft_and_never_posts(self):
        client = self.client()
        with patch.object(submission, "start_retrospective_submission", side_effect=sqlite3.OperationalError("disk error")):
            ack = await self.submit(client)
        self.assertIn("essay_body", ack.await_args.kwargs["errors"])
        client.chat_postMessage.assert_not_awaited()
        self.assertEqual(self.draft.call_args.args[1]["essay_body"], BODY)

    async def test_post_failure_removes_pending_row_and_keeps_essay_draft(self):
        client = self.client()
        client.chat_postMessage.side_effect = RuntimeError("Slack down")
        await self.submit(client)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.draft.call_args.args[1]["content_format"], "essay")
        self.assertEqual(self.draft.call_args.args[1]["essay_body"], BODY)

    async def test_duplicate_submission_error_points_to_the_essay_body(self):
        await self.submit(self.client())
        client = self.client()
        ack = await self.submit(client)
        self.assertIn("essay_body", ack.await_args.kwargs["errors"])
        client.chat_postMessage.assert_not_awaited()
        self.assertEqual(len(self.rows()), 1)

    async def test_unassigned_user_error_points_to_the_essay_body(self):
        with patch.object(settings, "SUBMISSION_DESTINATIONS", {}):
            ack = await self.submit(self.client())
        self.assertIn("essay_body", ack.await_args.kwargs["errors"])
        self.assertEqual(self.rows(), [])

    async def test_forged_format_does_not_bypass_essay_minimum(self):
        client = self.client()
        view = self.view(body="짧은 본문")
        metadata = json.loads(view["private_metadata"])
        metadata["content_format"] = "structured"
        view["private_metadata"] = json.dumps(metadata)
        ack = await self.submit(client, view)
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        client.chat_postMessage.assert_not_awaited()
        self.assertEqual(self.rows(), [])

    async def test_structured_submission_accepts_one_thousand_and_rejects_more(self):
        client = self.client()
        view = build_retrospective_view(channel_id="C11111111", session_name="6기 에세이 테스트", test_mode=True)
        fields = ("good_points", "improvements", "learnings", "action_item")
        view["state"] = {"values": {field: {field + "_input": {"value": "가" * 1001}} for field in fields}}
        ack = await self.submit(client, view)
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        client.chat_postMessage.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        for field in fields:
            view["state"]["values"][field][field + "_input"]["value"] = "가" * 1000
        await self.submit(client, view)
        self.assertEqual(self.rows()[0]["good_points"], "가" * 1000)

    async def test_my_retrospectives_list_opens_a_newly_saved_sqlite_essay(self):
        await self.submit(self.client())
        client = SimpleNamespace(views_open=AsyncMock())
        await my_retrospectives.handle_command_my_retrospectives(
            AsyncMock(), {"user_id": "U11111111", "trigger_id": "T1"}, client
        )
        view = client.views_open.await_args.kwargs["view"].to_dict()
        buttons = [block["accessory"] for block in view["blocks"] if block.get("accessory")]
        self.assertTrue(any(button.get("action_id") == "view_retrospective_detail" and button.get("value") == str(self.rows()[0]["id"]) for button in buttons))


class EssayDetailTest(unittest.IsolatedAsyncioTestCase):
    async def test_my_retrospective_detail_displays_the_essay(self):
        row = {"id": 1, "user_id": "U11111111", "session_name": "6기 3회차", "created_at": "2026-11-02 00:00:00", "content_format": "essay", "essay_title": "돌아본 하루", "essay_body": BODY, "good_points": "", "improvements": "", "learnings": "", "action_item": "", "emotion_score": None}
        client = SimpleNamespace(views_push=AsyncMock(), views_open=AsyncMock())
        with patch.object(detail, "get_retrospective_by_id", new=AsyncMock(return_value=row)):
            await detail.handle_action_view_retrospective_detail(AsyncMock(), {"user": {"id": "U11111111"}, "trigger_id": "T1"}, client, {"value": "1"})
        view = client.views_push.await_args.kwargs["view"].to_dict()
        self.assertIn({"type": "plain_text", "text": BODY}, [block.get("text") for block in view["blocks"]])
