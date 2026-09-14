"""DB 안정성 보완 검증: 마이그레이션, 중복 방지, Slack/DB 저장 일관성, 백업."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from loguru import logger

from config import parse_submission_teams, settings
from database.migrations import MIGRATIONS, run_migrations
from database.retrospective import (
    discard_pending_retrospective,
    mark_retrospective_posted,
    start_retrospective_submission,
)
from database.sqlite import get_connection, initialize_database
from exception import RetrospectiveAlreadySubmitted
from slack.events import view_retrospective_submit as submission
from slack.events.command_retrospective import build_retrospective_view

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_SCHEMA = """
CREATE TABLE retrospectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    slack_channel TEXT NOT NULL,
    slack_ts TEXT NOT NULL,
    good_points TEXT NOT NULL,
    improvements TEXT NOT NULL,
    learnings TEXT NOT NULL,
    action_item TEXT NOT NULL,
    emotion_score INTEGER,
    emotion_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE guided_reflections (
    flow_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    slack_channel TEXT NOT NULL,
    session_name TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '[]',
    current_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
LEGACY_ROW = """
INSERT INTO retrospectives (
    user_id, session_name, slack_channel, slack_ts,
    good_points, improvements, learnings, action_item
) VALUES (?, '6기 1회차', 'C11111111', '1.0', '좋음', '개선', '학습', '실행')
"""


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


class MigrationTest(unittest.TestCase):
    def setUp(self):
        logger.disable("database.migrations")
        self.addCleanup(logger.enable, "database.migrations")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_path = Path(self.temporary.name) / "test.db"
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(self.database_path)))

    def legacy_database(self, users: tuple[str, ...]) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(LEGACY_SCHEMA)
            for user_id in users:
                connection.execute(LEGACY_ROW, (user_id,))
            connection.commit()

    def test_migrations_are_recorded_and_idempotent(self):
        initialize_database()
        with closing(get_connection()) as connection:
            applied = [row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
        self.assertEqual(applied, [version for version, _, _ in MIGRATIONS])

        initialize_database()  # 재시작해도 같은 마이그레이션을 다시 적용하지 않는다.
        with closing(get_connection()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                len(MIGRATIONS),
            )
            self.assertEqual(run_migrations(connection), [])

    def test_users_table_is_added_to_existing_database(self):
        """이미 1번 마이그레이션을 기록한 DB에도 users 테이블이 생겨야 한다."""
        self.legacy_database(("U11111111",))
        initialize_database()
        with closing(get_connection()) as connection:
            self.assertEqual(
                columns(connection, "users"),
                {"user_id", "real_name", "email", "created_at", "updated_at"},
            )
            connection.execute(
                "INSERT INTO users (user_id, real_name, email) VALUES ('U11111111', '이름', 'a@b.c')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO users (user_id, real_name) VALUES ('U11111111', '중복')"
                )

    def test_admin_authentication_tables_are_migrated(self):
        initialize_database()
        with closing(get_connection()) as connection:
            self.assertEqual(
                columns(connection, "admin_users"),
                {"id", "username", "password_hash", "is_active", "created_at", "updated_at"},
            )
            self.assertIn("token_hash", columns(connection, "admin_sessions"))
            self.assertIn("locked_until", columns(connection, "admin_login_attempts"))

    def test_online_retro_attendance_table_is_migrated(self):
        initialize_database()
        with closing(get_connection()) as connection:
            self.assertEqual(
                columns(connection, "online_retro_attendance"),
                {"id", "session_name", "team_channel", "user_id", "attended_at"},
            )
            connection.execute(
                "INSERT INTO online_retro_attendance (session_name, team_channel, user_id) VALUES ('6기 1회차', 'C11111111', 'U11111111')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO online_retro_attendance (session_name, team_channel, user_id) VALUES ('6기 1회차', 'C11111111', 'U11111111')"
                )
            connection.execute(
                "INSERT INTO online_retro_attendance (session_name, team_channel, user_id) VALUES ('6기 1회차', 'C22222222', 'U11111111')"
            )

    def test_online_retro_poll_tables_are_migrated(self):
        initialize_database()
        with closing(get_connection()) as connection:
            self.assertIn("slots_json", columns(connection, "online_retro_time_polls"))
            self.assertEqual(
                columns(connection, "online_retro_time_votes"),
                {"poll_id", "user_id", "slots_json", "created_at", "updated_at"},
            )

    def test_legacy_database_gains_new_columns(self):
        self.legacy_database(("U11111111",))
        initialize_database()
        with closing(get_connection()) as connection:
            retrospective_columns = columns(connection, "retrospectives")
            self.assertIn("slack_post_status", retrospective_columns)
            self.assertIn("is_test_submission", retrospective_columns)
            self.assertIn("formatted_json", columns(connection, "guided_reflections"))
            row = connection.execute("SELECT * FROM retrospectives").fetchone()
            self.assertEqual(row["slack_post_status"], "posted")
            self.assertEqual(row["is_test_submission"], 0)

    def test_duplicate_rows_postpone_unique_index_without_breaking_startup(self):
        self.legacy_database(("U11111111", "U11111111"))
        initialize_database()  # 중복이 있어도 기동은 계속된다.
        with closing(get_connection()) as connection:
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            self.assertNotIn(4, applied)
            connection.execute(
                "DELETE FROM retrospectives WHERE id NOT IN (SELECT MIN(id) FROM retrospectives)"
            )
            connection.commit()

        initialize_database()  # 중복을 정리하면 다음 기동에서 적용된다.
        with closing(get_connection()) as connection:
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            self.assertIn(4, applied)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(LEGACY_ROW, ("U11111111",))


class SubmissionConsistencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in ("database.retrospective", "database.migrations", "slack.events.view_retrospective_submit"):
            logger.disable(name)
            self.addCleanup(logger.enable, name)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.teams = {"C11111111": ["U11111111", "U22222222"]}
        for name, value in {
            "DATABASE_PATH": str(Path(self.temporary.name) / "test.db"),
            "SUBMISSION_DESTINATIONS": parse_submission_teams(json.dumps(self.teams)),
            "TEST_SUBMISSION_CHANNEL": "",
            "SESSION_NAME_OVERRIDE": "",
        }.items():
            self.enterContext(patch.object(settings, name, value))
        self.enterContext(patch.object(submission, "cleanup_temp_files"))
        self.save_temp = self.enterContext(patch.object(submission, "save_temp_retrospective"))
        initialize_database()
        self.client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
            chat_postEphemeral=AsyncMock(),
        )

    async def submit(self, user="U11111111", session="6기 1회차"):
        view = build_retrospective_view(
            channel_id="C11111111", session_name=session, test_mode=False
        )
        view["state"] = {"values": {
            field: {field + "_input": {"value": "작성한 회고"}}
            for field in ("good_points", "improvements", "learnings", "action_item")
        }}
        view["state"]["values"]["calendar_image"] = {"calendar_image_input": {"files": []}}
        ack = AsyncMock()
        await submission.handle_view_retrospective_submit(
            ack, {"user": {"id": user}, "view": view}, self.client, view
        )
        return ack

    def rows(self) -> list[dict]:
        with closing(get_connection()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM retrospectives ORDER BY id")]

    async def test_successful_submission_is_marked_posted(self):
        await self.submit()
        row = self.rows()[0]
        self.assertEqual(row["slack_post_status"], "posted")
        self.assertEqual(row["slack_ts"], "123.456")
        self.assertEqual(row["is_test_submission"], 0)

    async def test_database_failure_keeps_slack_clean(self):
        with patch.object(submission, "start_retrospective_submission", side_effect=sqlite3.OperationalError("disk I/O error")):
            ack = await self.submit()
        self.client.chat_postMessage.assert_not_awaited()
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.save_temp.assert_called_once()
        self.assertEqual(self.rows(), [])

    async def test_post_failure_rolls_back_pending_row(self):
        self.client.chat_postMessage.side_effect = RuntimeError("slack down")
        await self.submit()
        self.assertEqual(self.rows(), [])
        # 되돌린 뒤에는 같은 회차로 다시 제출할 수 있다.
        self.client.chat_postMessage.side_effect = None
        await self.submit()
        self.assertEqual(len(self.rows()), 1)

    async def test_status_update_failure_keeps_row_and_does_not_repost(self):
        with patch.object(submission, "mark_retrospective_posted", side_effect=RuntimeError("write failed")):
            await self.submit()
        row = self.rows()[0]
        self.assertEqual(row["slack_post_status"], "pending")
        self.client.chat_postMessage.assert_awaited_once()
        self.save_temp.assert_not_called()

    async def test_second_submission_is_blocked_before_posting_again(self):
        await self.submit()
        self.client.chat_postMessage.reset_mock()
        ack = await self.submit()
        self.client.chat_postMessage.assert_not_awaited()
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        self.assertEqual(len(self.rows()), 1)

    async def test_pending_row_is_reused_instead_of_duplicating(self):
        record = await start_retrospective_submission(
            user_id="U11111111",
            session_name="6기 1회차",
            slack_channel="C11111111",
            good_points="이전 시도",
            improvements="이전 시도",
            learnings="이전 시도",
            action_item="이전 시도",
        )
        reused = await start_retrospective_submission(
            user_id="U11111111",
            session_name="6기 1회차",
            slack_channel="C11111111",
            good_points="새 내용",
            improvements="새 내용",
            learnings="새 내용",
            action_item="새 내용",
        )
        self.assertEqual(reused["id"], record["id"])
        self.assertEqual(reused["good_points"], "새 내용")
        self.assertEqual(len(self.rows()), 1)

        await mark_retrospective_posted(record["id"], "999.111")
        with self.assertRaises(RetrospectiveAlreadySubmitted):
            await start_retrospective_submission(
                user_id="U11111111",
                session_name="6기 1회차",
                slack_channel="C11111111",
                good_points="또 다른 시도",
                improvements="또 다른 시도",
                learnings="또 다른 시도",
                action_item="또 다른 시도",
            )
        self.assertFalse(await discard_pending_retrospective(record["id"]))


class BackupScriptTest(unittest.TestCase):
    def setUp(self):
        logger.disable("database.migrations")
        self.addCleanup(logger.enable, "database.migrations")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_path = Path(self.temporary.name) / "test.db"
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(self.database_path)))
        initialize_database()
        with closing(get_connection()) as connection:
            connection.execute(LEGACY_ROW, ("U11111111",))
            connection.commit()

    def run_script(self, *arguments) -> dict:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "backup_database.py"), *arguments],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stderr.strip().splitlines()[-1])

    def test_backup_round_trip_preserves_rows(self):
        archive = Path(self.temporary.name) / "backup.db.gz"
        created = self.run_script("create", "--source", str(self.database_path), "--output", str(archive))
        self.assertEqual(created["integrity_check"], "ok")
        self.assertEqual(created["counts"]["retrospectives"], 1)
        self.assertTrue(archive.exists())

        verified = self.run_script("verify", str(archive))
        self.assertEqual(verified["integrity_check"], "ok")
        self.assertEqual(verified["counts"]["retrospectives"], 1)
        self.assertEqual(verified["schema_version"], MIGRATIONS[-1][0])

    def test_backup_runs_while_writes_are_in_flight(self):
        """WAL 상태에서 커밋되지 않은 변경은 백업에 포함되지 않는다."""
        archive = Path(self.temporary.name) / "backup.db.gz"
        with closing(get_connection()) as connection:
            connection.execute(LEGACY_ROW, ("U22222222",))  # 커밋하지 않은 트랜잭션
            summary = self.run_script("create", "--source", str(self.database_path), "--output", str(archive))
        self.assertEqual(summary["integrity_check"], "ok")
        self.assertEqual(summary["counts"]["retrospectives"], 1)


if __name__ == "__main__":
    unittest.main()
