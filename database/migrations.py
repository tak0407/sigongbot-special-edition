"""SQLite 스키마 마이그레이션 러너.

운영 DB와 로컬 DB의 스키마가 갈라지지 않도록 모든 스키마 변경을 번호가 붙은
마이그레이션으로 관리한다. 적용 이력은 `schema_migrations` 테이블에 남는다.

새 스키마 변경은 `MIGRATIONS` 끝에 새 버전으로 추가하고, 이미 배포된
마이그레이션의 내용은 수정하지 않는다.
"""

import datetime
import sqlite3
from typing import Callable
from zoneinfo import ZoneInfo

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


def _admin_authentication(connection: sqlite3.Connection) -> None:
    """관리자 계정과 웹 세션, 로그인 실패 기록을 추가한다."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            admin_user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx ON admin_sessions(expires_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            attempt_key TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_until TEXT
        )
        """
    )


def _online_retro_attendance(connection: sqlite3.Connection) -> None:
    """온라인 회고 모임의 회차별 출석 기록을 추가한다."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS online_retro_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL,
            team_channel TEXT NOT NULL,
            user_id TEXT NOT NULL,
            attended_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_name, team_channel, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS online_retro_attendance_session_idx
            ON online_retro_attendance(session_name, team_channel, attended_at)
        """
    )


def _online_retro_time_polls(connection: sqlite3.Connection) -> None:
    """팀별 온라인 회고 시간 투표와 사용자 선택을 저장한다."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS online_retro_time_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date TEXT NOT NULL,
            session_name TEXT NOT NULL,
            team_channel TEXT NOT NULL,
            team_name TEXT NOT NULL,
            slots_json TEXT NOT NULL,
            slack_ts TEXT,
            is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(meeting_date, team_channel, is_test)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS online_retro_time_votes (
            poll_id INTEGER NOT NULL REFERENCES online_retro_time_polls(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            slots_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (poll_id, user_id)
        )
        """
    )


def _sessions_table(connection: sqlite3.Connection) -> None:
    """회차 일정을 코드 상수에서 DB로 옮긴다.

    `constants.py`의 값을 그대로 시드해 전환 시점의 동작이 완전히 같도록 한다.
    이후 회차 추가와 마감일 변경은 관리자 웹에서 한다.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            name TEXT PRIMARY KEY,
            due_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS sessions_due_at_idx ON sessions(due_at)"
    )

    from constants import DUE_DATES, SESSION_NAMES

    for name, due in zip(SESSION_NAMES, DUE_DATES):
        connection.execute(
            "INSERT OR IGNORE INTO sessions (name, due_at) VALUES (?, ?)",
            (name, due.isoformat()),
        )


def _session_announcements(connection: sqlite3.Connection) -> None:
    """매회차 제출 공지를 DB로 옮긴다.

    공지 문구가 코드에 박혀 있어 6기 1회차 한 번만 나가고 끝났다. 회차마다
    공지 시각과 문구를 두고, 기본 문구는 `announcement_templates`에서 읽는다.

    `announce_at`은 지난 회차까지 포함해 전부 채운다. 발송은 `announce_at <=
    now < due_at`일 때만 하므로 이미 마감된 회차가 뒤늦게 다시 나가지 않는다.
    """
    columns = _column_names(connection, "sessions")
    if "announce_at" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN announce_at TEXT")
    if "announcement" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN announcement TEXT")
    if "announced_at" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN announced_at TEXT")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS announcement_templates (
            kind TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    from constants import ANNOUNCE_LEAD, DEFAULT_SUBMISSION_ANNOUNCEMENT

    connection.execute(
        "INSERT OR IGNORE INTO announcement_templates (kind, body) VALUES (?, ?)",
        ("session_submission", DEFAULT_SUBMISSION_ANNOUNCEMENT),
    )

    pending = connection.execute(
        "SELECT name, due_at FROM sessions WHERE announce_at IS NULL"
    ).fetchall()
    for row in pending:
        due = datetime.datetime.fromisoformat(row["due_at"])
        connection.execute(
            "UPDATE sessions SET announce_at = ? WHERE name = ?",
            ((due - ANNOUNCE_LEAD).isoformat(), row["name"]),
        )

    # 6기 1회차는 하드코딩된 스케줄러가 이미 보냈다. 마감도 지나 다시 나갈 일은
    # 없지만, 화면에 "예정"으로 보이지 않도록 발송 이력을 옮겨 둔다.
    # `scheduled_announcements.sent_at`은 SQLite가 UTC로 남긴 타임존 없는
    # 문자열이다. `announced_at`은 KST 기준 시각으로 통일해 둬야 화면에서
    # 9시간 어긋나지 않는다.
    sent = connection.execute(
        "SELECT sent_at FROM scheduled_announcements"
        " WHERE announcement_key LIKE '6-1-announcement:%'"
    ).fetchone()
    if sent:
        moment = datetime.datetime.fromisoformat(str(sent["sent_at"]).replace(" ", "T"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        connection.execute(
            "UPDATE sessions SET announced_at = ? WHERE name = ?",
            (moment.astimezone(ZoneInfo("Asia/Seoul")).isoformat(), "6기 1회차"),
        )


def _bot_improvement_suggestions(connection: sqlite3.Connection) -> None:
    """Slack에서 접수한 봇 개선 제안과 운영 처리 상태를 저장한다."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_improvement_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            user_id TEXT NOT NULL,
            submission_channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'completed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS bot_improvement_suggestions_status_idx
            ON bot_improvement_suggestions(status, created_at, id)
        """
    )

MIGRATIONS: tuple[Migration, ...] = (
    (1, "baseline_schema", _baseline),
    (2, "guided_reflections_formatted_json", _guided_formatted_json),
    (3, "retrospectives_post_state", _retrospective_post_state),
    (4, "retrospectives_unique_submission", _retrospective_unique_submission),
    (5, "users_table", _users_table),
    (6, "admin_authentication", _admin_authentication),
    (7, "online_retro_attendance", _online_retro_attendance),
    (8, "online_retro_time_polls", _online_retro_time_polls),
    (9, "sessions_table", _sessions_table),
    (10, "session_announcements", _session_announcements),
    (11, "bot_improvement_suggestions", _bot_improvement_suggestions),
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
