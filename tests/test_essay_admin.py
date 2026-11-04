"""관리자 회고 화면을 임시 SQLite와 Slack 경계 대역으로 검증한다."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from loguru import logger

from config import settings
from database.retrospective import create_retrospective, get_retrospective_by_id
from database.sqlite import initialize_database
from slack.events import view_admin_menu as admin


def as_dict(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


class EssayAdminTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")))
        self.enterContext(patch.object(settings, "ADMIN_IDS", ["UADMIN"]))
        for module in ("database.retrospective", "database.migrations", "slack.events.view_admin_menu"):
            logger.disable(module)
            self.addCleanup(logger.enable, module)
        initialize_database()
        self.client = SimpleNamespace(
            views_open=AsyncMock(), views_update=AsyncMock(), views_push=AsyncMock(),
            conversations_history=AsyncMock(return_value={
                "ok": True, "messages": [{"ts": "123.456", "blocks": []}]
            }),
            chat_update=AsyncMock(),
        )
        self.body = {"user": {"id": "UADMIN"}, "trigger_id": "trigger", "view": {"id": "VEDIT"}}

    async def create_structured(self, **overrides):
        data = {
            "user_id": "UWRITER", "session_name": "테스트 회차", "slack_channel": "CSTORED",
            "slack_ts": "123.456", "good_points": "좋음", "improvements": "개선",
            "learnings": "배움", "action_item": "실천", "emotion_score": 7,
            "emotion_reason": "기분 좋음",
        }
        data.update(overrides)
        return await create_retrospective(**data)

    async def create_essay(self, **overrides):
        data = {"content_format": "essay", "essay_title": "이번의 생각", "essay_body": "자유로운 경험과 생각. " * 20}
        data.update(overrides)
        return await self.create_structured(**data)

    def essay_values(self, *, title="고친 제목", body="바꾼 경험과 생각을 적어요. " * 20, **overrides):
        fields = {"essay_title": title, "essay_body": body}
        fields.update(overrides)
        return {field: {field + "_input": {"value": value}} for field, value in fields.items()}

    async def preview(self, row):
        ack = AsyncMock()
        await admin.handle_view_admin_menu(ack, self.body, self.client, {
            "state": {"values": {"retrospective_id": {"retrospective_id_input": {"value": str(row["id"])}}}}
        })
        return ack

    async def edit_form(self, row):
        await admin.handle_admin_action_edit(AsyncMock(), self.body, self.client, {"value": str(row["id"])})
        self.assertIsNotNone(self.client.views_update.await_args, "수정 모달이 열려야 합니다.")
        return as_dict(self.client.views_update.await_args.kwargs["view"])

    def structured_values(self, **overrides):
        fields = {"good_points": "새로운 좋음", "improvements": "새로운 개선", "learnings": "새로운 배움", "action_item": "새로운 실천"}
        fields.update(overrides)
        return {field: {field + "_input": {"value": value}} for field, value in fields.items()}

    async def save(self, row, values, *, metadata=None, body=None):
        ack = AsyncMock()
        await admin.handle_view_admin_edit_retrospective(
            ack, body or self.body, self.client, {
                "private_metadata": metadata or f"{row['id']}|CSTORED|123.456",
                "state": {"values": values},
            },
        )
        return ack

    async def test_preview_keeps_all_long_structured_fields_inside_slack_limits(self):
        row = await self.create_structured(**{field: char * 1000 for field, char in (
            ("good_points", "가"), ("improvements", "나"), ("learnings", "다"), ("action_item", "라")
        )})
        await self.preview(row)
        self.assertIsNotNone(self.client.views_open.await_args, "1000자 항목의 회고도 열려야 합니다.")
        view = as_dict(self.client.views_open.await_args.kwargs["view"])
        sections = [block["text"]["text"] for block in view["blocks"] if block["type"] == "section"]
        for text in sections:
            self.assertLessEqual(len(text), 3000)
        for char in ("가", "나", "다", "라"):
            self.assertIn(char * 1000, "\n".join(sections))

    async def test_structured_edit_form_accepts_existing_thousand_character_fields(self):
        row = await self.create_structured(good_points="가" * 1000, emotion_reason="감" * 1000)
        form = await self.edit_form(row)
        blocks = {block.get("block_id"): block for block in form["blocks"]}
        for field in ("good_points", "improvements", "learnings", "action_item", "emotion_reason"):
            self.assertEqual(blocks[field]["element"]["max_length"], 1000)
        self.assertEqual(blocks["good_points"]["element"]["initial_value"], "가" * 1000)

    async def test_structured_save_rejects_oversized_input_and_keeps_form(self):
        row = await self.create_structured()
        ack = await self.save(row, self.structured_values(good_points="가" * 1001))
        self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
        self.assertIn("good_points", ack.await_args.kwargs["errors"])
        self.assertEqual((await get_retrospective_by_id(row["id"]))["good_points"], "좋음")
        self.assertEqual(self.client.chat_update.await_count, 0)

    async def test_save_rejects_oversized_emotion_reason(self):
        row = await self.create_structured()
        ack = await self.save(row, self.structured_values(emotion_reason="감" * 1001))
        self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
        self.assertIn("emotion_reason", ack.await_args.kwargs["errors"])
        self.assertEqual((await get_retrospective_by_id(row["id"]))["emotion_reason"], "기분 좋음")

    async def test_save_can_clear_optional_emotions(self):
        row = await self.create_structured()
        ack = await self.save(row, self.structured_values(emotion_score=None, emotion_reason=None))
        self.assertEqual(ack.await_args.kwargs, {})
        updated = await get_retrospective_by_id(row["id"])
        self.assertIsNone(updated["emotion_score"])
        self.assertIsNone(updated["emotion_reason"])

    async def test_save_preserves_omitted_emotions_in_posted_message(self):
        row = await self.create_structured()
        await self.save(row, self.structured_values())
        blocks = [as_dict(block) for block in self.client.chat_update.await_args.kwargs["blocks"]]
        texts = "\n".join(block.get("text", {}).get("text", "") for block in blocks)
        self.assertIn("7/10", texts)
        self.assertIn("기분 좋음", texts)

    async def test_save_uses_db_message_address_instead_of_forged_metadata(self):
        row = await self.create_structured()
        await self.save(row, self.structured_values(), metadata=f"{row['id']}|CFORGED|666.666|essay")
        self.assertEqual(self.client.chat_update.await_args.kwargs["channel"], "CSTORED")
        self.assertEqual(self.client.chat_update.await_args.kwargs["ts"], "123.456")

    async def test_save_preserves_existing_calendar_image(self):
        row = await self.create_structured()
        image = {"type": "image", "slack_file": {"id": "FIMAGE"}, "alt_text": "시간 기록 이미지"}
        self.client.conversations_history.return_value = {
            "ok": True, "messages": [{"ts": "123.456", "blocks": [image]}]
        }
        await self.save(row, self.structured_values())
        blocks = [as_dict(block) for block in self.client.chat_update.await_args.kwargs["blocks"]]
        self.assertIn(image, blocks)

    async def test_image_lookup_failure_saves_db_but_keeps_existing_slack_message(self):
        row = await self.create_structured()
        self.client.conversations_history.side_effect = RuntimeError("temporary Slack failure")
        ack = await self.save(row, self.structured_values())
        self.assertEqual(ack.await_args.kwargs, {})
        self.assertEqual((await get_retrospective_by_id(row["id"]))["good_points"], "새로운 좋음")
        self.assertEqual(self.client.chat_update.await_count, 0)
        result = as_dict(self.client.views_open.await_args.kwargs["view"])
        self.assertIn("오류", result["blocks"][0]["text"]["text"])

    async def test_essay_preview_renders_title_and_full_body_as_plain_text(self):
        row = await self.create_essay(essay_title="*제목* <@U123>", essay_body="가" * 3000)
        await self.preview(row)
        view = as_dict(self.client.views_open.await_args.kwargs["view"])
        texts = [block["text"] for block in view["blocks"] if block["type"] == "section"]
        self.assertIn({"type": "plain_text", "text": "*제목* <@U123>"}, texts)
        self.assertIn({"type": "plain_text", "text": "가" * 3000}, texts)

    async def test_essay_edit_form_keeps_initial_text_and_correct_limits(self):
        row = await self.create_essay(essay_body="가" * 3000, essay_title="나" * 150)
        form = await self.edit_form(row)
        blocks = {block.get("block_id"): block for block in form["blocks"]}
        self.assertNotIn("good_points", blocks)
        self.assertTrue(blocks["essay_title"]["optional"])
        self.assertEqual(blocks["essay_title"]["element"]["initial_value"], "나" * 150)
        self.assertEqual(blocks["essay_title"]["element"]["max_length"], 150)
        self.assertEqual(blocks["essay_body"]["element"]["initial_value"], "가" * 3000)
        self.assertEqual(blocks["essay_body"]["element"]["min_length"], 100)
        self.assertEqual(blocks["essay_body"]["element"]["max_length"], 3000)
        self.assertEqual(blocks["emotion_reason"]["element"]["max_length"], 1000)

    async def test_essay_save_keeps_raw_text_and_can_clear_title(self):
        row = await self.create_essay()
        body = "  " + "경험" * 60 + "\n생각  "
        ack = await self.save(row, self.essay_values(title=None, body=body))
        self.assertEqual(ack.await_args.kwargs, {})
        updated = await get_retrospective_by_id(row["id"])
        self.assertEqual(updated["content_format"], "essay")
        self.assertEqual(updated["essay_body"], body)
        self.assertIsNone(updated["essay_title"])
        self.assertEqual(updated["good_points"], "")
        blocks = [as_dict(block) for block in self.client.chat_update.await_args.kwargs["blocks"]]
        self.assertIn({"type": "section", "text": {"type": "plain_text", "text": body}}, blocks)

    async def test_invalid_essay_body_keeps_modal_and_db_unchanged(self):
        row = await self.create_essay()
        for body in (None, "가" * 99, " " * 5 + "가" * 99, "가" * 3001):
            with self.subTest(body_length=len(body or "")):
                ack = await self.save(row, self.essay_values(body=body))
                self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
                self.assertIn("essay_body", ack.await_args.kwargs["errors"])
                self.assertEqual((await get_retrospective_by_id(row["id"]))["essay_body"], row["essay_body"])

    async def test_oversized_essay_title_error_targets_title_input(self):
        row = await self.create_essay()
        ack = await self.save(row, self.essay_values(title="가" * 151))
        self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
        self.assertIn("essay_title", ack.await_args.kwargs["errors"])
        self.assertEqual((await get_retrospective_by_id(row["id"]))["essay_title"], "이번의 생각")

    async def test_essay_cannot_be_converted_using_structured_form_and_forged_metadata(self):
        row = await self.create_essay()
        ack = await self.save(row, self.structured_values(), metadata=f"{row['id']}|CFORGED|9.9|structured")
        self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
        updated = await get_retrospective_by_id(row["id"])
        self.assertEqual(updated["content_format"], "essay")
        self.assertEqual(updated["essay_body"], row["essay_body"])

    async def test_unauthorized_essay_save_error_targets_existing_input(self):
        row = await self.create_essay()
        ack = await self.save(row, self.essay_values(), body={**self.body, "user": {"id": "UOTHER"}})
        self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
        self.assertIn("essay_body", ack.await_args.kwargs["errors"])
        self.assertEqual((await get_retrospective_by_id(row["id"]))["essay_title"], "이번의 생각")


if __name__ == "__main__":
    unittest.main()
