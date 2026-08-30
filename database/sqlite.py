import os
import sqlite3
from pathlib import Path

from config import settings


def get_connection() -> sqlite3.Connection:
    database_path = Path(settings.DATABASE_PATH).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize_database() -> None:
    database_path = Path(settings.DATABASE_PATH).expanduser()
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS retrospectives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_name TEXT NOT NULL,
                slack_channel TEXT NOT NULL,
                slack_ts TEXT NOT NULL,
                good_points TEXT NOT NULL,
                improvements TEXT NOT NULL,
                learnings TEXT NOT NULL,
                action_item TEXT NOT NULL,
                emotion_score INTEGER CHECK (
                    emotion_score BETWEEN 1 AND 10 OR emotion_score IS NULL
                ),
                emotion_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS retrospectives_user_id_idx
                ON retrospectives(user_id);

            CREATE TABLE IF NOT EXISTS ai_review_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                slack_channel TEXT NOT NULL,
                slack_ts TEXT NOT NULL,
                file_id TEXT NOT NULL,
                calendar_type TEXT NOT NULL DEFAULT 'auto',
                retrospective_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                feedback TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS ai_review_jobs_status_idx
                ON ai_review_jobs(status, created_at, id);

            CREATE TABLE IF NOT EXISTS guided_reflections (
                flow_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                slack_channel TEXT NOT NULL,
                session_name TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                answers_json TEXT NOT NULL DEFAULT '[]',
                formatted_json TEXT,
                current_index INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS guided_reflections_user_idx
                ON guided_reflections(user_id, created_at);

            UPDATE ai_review_jobs
               SET status = 'pending', updated_at = CURRENT_TIMESTAMP
             WHERE status = 'processing';
            """
        )
        guided_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(guided_reflections)")
        }
        if "formatted_json" not in guided_columns:
            connection.execute(
                "ALTER TABLE guided_reflections ADD COLUMN formatted_json TEXT"
            )

    os.chmod(database_path.parent, 0o700)
    os.chmod(database_path, 0o600)
