"""자유 에세이의 마이그레이션과 저장 계약을 임시 SQLite로 검증한다."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from config import settings
from database import migrations
from database.retrospective import (
    check_user_submitted_this_session,
    count_pending_retrospectives,
    create_retrospective,
    discard_pending_retrospective,
    get_latest_retrospectives,
    get_retrospective_by_id,
    get_retrospectives_by_user_id,
    get_submitted_user_ids,
    mark_retrospective_posted,
    start_retrospective_submission,
    update_retrospective,
)
from database.sqlite import initialize_database
from exception import RetrospectiveAlreadySubmitted

LEGACY_FIELDS = ("good_points", "improvements", "learnings", "action_item")


class EssayMigrationTest(unittest.TestCase):
    def setUp(self):
        logger.disable("database.migrations")
        self.addCleanup(logger.enable, "database.migrations")

    def test_migrates_version_18_row_without_changing_legacy_content(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            deployed = tuple(item for item in migrations.MIGRATIONS if item[0] <= 18)
            with patch.object(migrations, "MIGRATIONS", deployed):
                migrations.run_migrations(connection)
            connection.execute(
                """INSERT INTO retrospectives (
                    id, user_id, session_name, slack_channel, slack_ts,
                    good_points, improvements, learnings, action_item
                ) VALUES (42, 'U1', '지난 회차', 'C1', '1.0', '좋음', '개선', '배움', '실행')"""
            )
            connection.commit()

            self.assertTrue(migrations.run_migrations(connection))
            row = dict(connection.execute("SELECT * FROM retrospectives").fetchone())
            self.assertEqual(row["id"], 42)
            self.assertEqual(row["good_points"], "좋음")
            self.assertEqual(row["improvements"], "개선")
            self.assertEqual(row["learnings"], "배움")
            self.assertEqual(row["action_item"], "실행")
            self.assertEqual(row["content_format"], "structured")
            self.assertIsNone(row["essay_title"])
            self.assertIsNone(row["essay_body"])
            self.assertEqual(migrations.run_migrations(connection), [])
            info = {row["name"]: row for row in connection.execute("PRAGMA table_info(retrospectives)")}
            for field in ("good_points", "improvements", "learnings", "action_item", "content_format"):
                self.assertEqual(info[field]["notnull"], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE retrospectives SET content_format = 'unknown'")


class EssayDatabaseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for module in ("database.migrations", "database.retrospective"):
            logger.disable(module)
            self.addCleanup(logger.enable, module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")))
        initialize_database()

    async def start(self, **overrides):
        data = {
            "user_id": "U1", "session_name": "에세이 회차", "slack_channel": "C1",
            "good_points": "좋은 경험", "improvements": "다음 기회", "learnings": "새 배움",
            "action_item": "실행할 일", "content_format": "essay",
            "essay_title": "  이번 기간의 회고  ", "essay_body": "가" * 100,
        }
        data.update(overrides)
        return await start_retrospective_submission(**data)

    async def test_persists_essay_original_and_clears_legacy_text(self):
        body = " \n" + "가" * 100 + "\n<@U123>&경험 "
        row = await self.start(essay_body=body, emotion_score=7, emotion_reason="기뻤다")
        stored = await get_retrospective_by_id(row["id"])
        self.assertEqual(stored["content_format"], "essay")
        self.assertEqual(stored["essay_title"], "이번 기간의 회고")
        self.assertEqual(stored["essay_body"], body)
        self.assertEqual([stored[field] for field in LEGACY_FIELDS], ["", "", "", ""])
        self.assertEqual((stored["emotion_score"], stored["emotion_reason"]), (7, "기뻤다"))
        self.assertEqual((stored["slack_post_status"], stored["slack_ts"]), ("pending", ""))

    async def test_create_essay_validates_before_insert_and_preserves_old_callers(self):
        old = await create_retrospective("U0", "옛 회차", "C1", "1.0", "", "", "", "")
        self.assertEqual(old["content_format"], "structured")
        self.assertIsNone(old["essay_body"])
        row = await create_retrospective(
            "U1", "에세이 회차", "C1", "2.0", "옛값", "옛값", "옛값", "옛값",
            content_format="essay", essay_title=" ", essay_body="나" * 100,
        )
        self.assertEqual(row["slack_post_status"], "posted")
        self.assertIsNone(row["essay_title"])
        self.assertEqual(row["essay_body"], "나" * 100)
        self.assertEqual([row[field] for field in LEGACY_FIELDS], ["", "", "", ""])
        with self.assertRaises(ValueError):
            await create_retrospective(
                "U2", "에세이 회차", "C1", "3.0", "", "", "", "",
                content_format="essay", essay_body="짧은 회고",
            )
        self.assertEqual(len(await get_latest_retrospectives()), 2)

    async def test_essay_body_and_title_boundaries_use_trimmed_minimum_and_raw_maximum(self):
        cases = (
            (None, "제목"), ("", "제목"), (" " * 100, "제목"),
            (" " + "가" * 99 + " ", "제목"), ("가" * 3001, "제목"),
            (" " + "가" * 2999 + " ", "제목"), ("가" * 100, "제" * 151),
            ("가" * 100, " " + "제" * 149 + " "),
        )
        for body, title in cases:
            with self.subTest(body_length=len(body or ""), title_length=len(title)):
                with self.assertRaisesRegex(ValueError, "본문|제목"):
                    await self.start(essay_body=body, essay_title=title)
        self.assertEqual(await get_latest_retrospectives(), [])
        for body, title in (("가" * 100, None), ("나" * 3000, "제" * 150)):
            row = await self.start(essay_body=body, essay_title=title, is_test=True)
            self.assertEqual(row["essay_body"], body)
            self.assertEqual(row["essay_title"], title)

    async def test_rejects_unknown_content_format_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "형식"):
            await self.start(content_format="other")
        with self.assertRaisesRegex(ValueError, "형식"):
            await create_retrospective("U1", "회차", "C1", "1.0", "", "", "", "", content_format="other")
        self.assertEqual(await get_latest_retrospectives(), [])

    async def test_structured_ignores_supplied_essay_content_without_new_minimum(self):
        row = await self.start(content_format="structured", good_points="", essay_title="제" * 200, essay_body="짧음")
        self.assertEqual(row["content_format"], "structured")
        self.assertEqual(row["good_points"], "")
        self.assertEqual(row["improvements"], "다음 기회")
        self.assertIsNone(row["essay_title"])
        self.assertIsNone(row["essay_body"])

    async def test_pending_switch_to_essay_removes_all_structured_content(self):
        original = await self.start(content_format="structured", emotion_score=2, emotion_reason="전 감정")
        row = await self.start(slack_channel="C2", essay_body="나" * 110, emotion_score=None)
        self.assertEqual(row["id"], original["id"])
        self.assertEqual(row["slack_channel"], "C2")
        self.assertEqual(row["content_format"], "essay")
        self.assertEqual(row["essay_body"], "나" * 110)
        self.assertEqual([row[field] for field in LEGACY_FIELDS], ["", "", "", ""])
        self.assertIsNone(row["emotion_score"])
        self.assertIsNone(row["emotion_reason"])
        self.assertEqual(await count_pending_retrospectives(), 1)

    async def test_pending_switch_to_structured_removes_essay_content(self):
        original = await self.start()
        row = await self.start(content_format="structured", good_points="다시 작성")
        self.assertEqual(row["id"], original["id"])
        self.assertEqual(row["content_format"], "structured")
        self.assertEqual(row["good_points"], "다시 작성")
        self.assertIsNone(row["essay_title"])
        self.assertIsNone(row["essay_body"])

    async def test_invalid_pending_retry_keeps_existing_content(self):
        original = await self.start()
        with self.assertRaises(ValueError):
            await self.start(essay_body="짧음", slack_channel="C2")
        self.assertEqual(await get_retrospective_by_id(original["id"]), original)

    async def test_posted_rows_block_cross_format_duplicates_but_test_rows_do_not(self):
        for first_format, second_format in (("essay", "structured"), ("structured", "essay")):
            with self.subTest(first=first_format):
                first = await self.start(session_name=first_format, content_format=first_format)
                await mark_retrospective_posted(first["id"], "1.0")
                with self.assertRaises(RetrospectiveAlreadySubmitted):
                    await self.start(session_name=first_format, content_format=second_format)
                with self.assertRaises(sqlite3.IntegrityError):
                    await create_retrospective(
                        "U1", first_format, "C1", "2.0", "", "", "", "",
                        content_format=second_format, essay_body="나" * 100,
                    )
                test1 = await self.start(session_name=first_format, is_test=True, content_format=first_format)
                test2 = await self.start(session_name=first_format, is_test=True, content_format=second_format)
                self.assertNotEqual(test1["id"], test2["id"])
                self.assertEqual((test1["is_test_submission"], test2["is_test_submission"]), (1, 1))

    async def test_edit_essay_validates_merged_content_and_normalizes_optional_title(self):
        original = await self.start()
        changed = await update_retrospective(original["id"], {"essay_title": " \n ", "emotion_score": 8})
        self.assertEqual(changed["content_format"], "essay")
        self.assertIsNone(changed["essay_title"])
        self.assertEqual(changed["essay_body"], original["essay_body"])
        self.assertEqual(changed["emotion_score"], 8)
        body = " \n" + "나" * 100 + " \n"
        changed = await update_retrospective(original["id"], {"essay_body": body, "good_points": "이전 항목"})
        self.assertEqual(changed["essay_body"], body)
        self.assertEqual(changed["good_points"], "")
        for data in ({"essay_body": None}, {"essay_body": "가" * 99}, {"essay_body": "가" * 3001}, {"essay_title": "제" * 151}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    await update_retrospective(original["id"], data)
                self.assertEqual(await get_retrospective_by_id(original["id"]), changed)

    async def test_edit_keeps_existing_format_and_does_not_validate_structured_minimum(self):
        row = await self.start(content_format="structured", good_points="")
        changed = await update_retrospective(row["id"], {
            "good_points": "", "content_format": "essay", "essay_body": "가" * 100, "essay_title": "남은 제목",
        })
        self.assertEqual(changed["content_format"], "structured")
        self.assertEqual(changed["good_points"], "")
        self.assertIsNone(changed["essay_title"])
        self.assertIsNone(changed["essay_body"])

    async def test_both_formats_share_submission_history_and_pending_semantics(self):
        essay = await self.start()
        structured = await self.start(user_id="U2", content_format="structured")
        test = await self.start(user_id="UTEST", is_test=True)
        self.assertEqual(await count_pending_retrospectives(), 3)
        self.assertTrue(await check_user_submitted_this_session("U1", "에세이 회차"))
        self.assertTrue(await check_user_submitted_this_session("U2", "에세이 회차"))
        self.assertEqual(await get_submitted_user_ids("에세이 회차"), {"U1", "U2", "UTEST"})
        self.assertEqual(await get_submitted_user_ids("에세이 회차", include_test=False), {"U1", "U2"})
        self.assertEqual(len(await get_retrospectives_by_user_id("U1")), 1)
        posted = await mark_retrospective_posted(essay["id"], "2.5")
        self.assertEqual((posted["slack_post_status"], posted["slack_ts"]), ("posted", "2.5"))
        self.assertEqual(posted["essay_body"], "가" * 100)
        self.assertFalse(await discard_pending_retrospective(essay["id"]))
        self.assertTrue(await discard_pending_retrospective(test["id"]))
        self.assertEqual(await count_pending_retrospectives(), 1)
        self.assertEqual({row["id"] for row in await get_latest_retrospectives()}, {essay["id"], structured["id"]})


if __name__ == "__main__":
    unittest.main()
