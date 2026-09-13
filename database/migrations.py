"""SQLite 스키마 마이그레이션 러너.

운영 DB와 로컬 DB의 스키마가 갈라지지 않도록 모든 스키마 변경을 번호가 붙은
마이그레이션으로 관리한다. 적용 이력은 `schema_migrations` 테이블에 남는다.

새 스키마 변경은 `MIGRATIONS` 끝에 새 버전으로 추가하고, 이미 배포된
마이그레이션의 내용은 수정하지 않는다.
"""

import sqlite3
from typing import Callable

from loguru import logger

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


class MigrationSkipped(Exception):
    """데이터 정리가 필요해 이번 실행에서는 건너뛰는 마이그레이션."""


BASELINE_STATEMENTS = (
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS retrospectives_user_id_idx
        ON retrospectives(user_id)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ai_review_jobs_status_idx
        ON ai_review_jobs(status, created_at, id)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS guided_reflections_user_idx
        ON guided_reflections(user_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_announcements (
        announcement_key TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _baseline(connection: sqlite3.Connection) -> None:
    for statement in BASELINE_STATEMENTS:
        connection.execute(statement)


def _guided_formatted_json(connection: sqlite3.Connection) -> None:
    if "formatted_json" not in _column_names(connection, "guided_reflections"):
        connection.execute(
            "ALTER TABLE guided_reflections ADD COLUMN formatted_json TEXT"
        )


def _retrospective_post_state(connection: sqlite3.Connection) -> None:
    """Slack 게시 상태와 테스트 제출 여부를 기록할 컬럼을 추가한다."""
    columns = _column_names(connection, "retrospectives")
    if "slack_post_status" not in columns:
        connection.execute(
            """
            ALTER TABLE retrospectives
                ADD COLUMN slack_post_status TEXT NOT NULL DEFAULT 'posted'
            """
        )
    if "is_test_submission" not in columns:
        connection.execute(
            """
            ALTER TABLE retrospectives
                ADD COLUMN is_test_submission INTEGER NOT NULL DEFAULT 0
            """
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS retrospectives_post_status_idx
            ON retrospectives(slack_post_status)
        """
    )


def _retrospective_unique_submission(connection: sqlite3.Connection) -> None:
    """운영 회차에 대해 (user_id, session_name) 중복 제출을 DB에서 차단한다."""
    duplicates = connection.execute(
        """
        SELECT user_id, session_name, COUNT(*) AS count
          FROM retrospectives
         WHERE is_test_submission = 0
         GROUP BY user_id, session_name
        HAVING count > 1
        """
    ).fetchall()
    if duplicates:
        summary = ", ".join(
            f"{row[0]}/{row[1]}({row[2]}건)" for row in duplicates
        )
        raise MigrationSkipped(
            f"중복 회고가 남아 있어 UNIQUE 제약을 적용하지 못했습니다 - {summary}"
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS retrospectives_session_unique_idx
            ON retrospectives(user_id, session_name)
         WHERE is_test_submission = 0
        """
    )


def _users_table(connection: sqlite3.Connection) -> None:
    """회고 작성자 명단.

    `retrospectives.user_id`에 외래키를 걸지 않는다. 명단에 없는 작성자가 있고,
    명단에도 사람이 아닌 앱 계정이 섞여 있다.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            real_name TEXT NOT NULL,
            email TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "baseline_schema", _baseline),
    (2, "guided_reflections_formatted_json", _guided_formatted_json),
    (3, "retrospectives_post_state", _retrospective_post_state),
    (4, "retrospectives_unique_submission", _retrospective_unique_submission),
    (5, "users_table", _users_table),
)


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}


def run_migrations(connection: sqlite3.Connection) -> list[int]:
    """미적용 마이그레이션을 순서대로 실행하고 적용된 버전 목록을 반환한다."""
    previous_isolation = connection.isolation_level
    connection.isolation_level = None  # 마이그레이션 단위 트랜잭션을 직접 관리한다.
    executed: list[int] = []
    try:
        applied = applied_versions(connection)
        for version, name, apply in MIGRATIONS:
            if version in applied:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                apply(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
            except MigrationSkipped as error:
                connection.execute("ROLLBACK")
                logger.warning(f"마이그레이션 보류 - {version}_{name}: {error}")
                continue
            except Exception:
                connection.execute("ROLLBACK")
                logger.error(f"마이그레이션 실패 - {version}_{name}")
                raise
            connection.execute("COMMIT")
            executed.append(version)
            logger.info(f"마이그레이션 적용 - {version}_{name}")
    finally:
        connection.isolation_level = previous_isolation
    return executed
