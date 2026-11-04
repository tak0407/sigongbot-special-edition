import asyncio
import sqlite3
from typing import Any

from loguru import logger

from exception import RetrospectiveAlreadySubmitted
from database.sqlite import get_connection
from retrospective_content import validate_essay

STRUCTURED_CONTENT_FIELDS = (
    "good_points",
    "improvements",
    "learnings",
    "action_item",
)
SUBMISSION_CONTENT_FIELDS = (
    *STRUCTURED_CONTENT_FIELDS,
    "emotion_score",
    "emotion_reason",
    "content_format",
    "essay_title",
    "essay_body",
)


def _normalize_content(data: dict[str, Any]) -> dict[str, Any]:
    """한 형식의 본문만 저장하고 에세이 규칙을 모든 저장 경로에 적용한다."""
    content = {field: data[field] for field in SUBMISSION_CONTENT_FIELDS}
    if content["content_format"] not in ("structured", "essay"):
        raise ValueError("지원하지 않는 회고 형식입니다.")
    if content["content_format"] == "essay":
        content["essay_title"], content["essay_body"] = validate_essay(
            content["essay_title"], content["essay_body"]
        )
        for field in STRUCTURED_CONTENT_FIELDS:
            content[field] = ""
    else:
        content["essay_title"] = None
        content["essay_body"] = None
    return content


def _to_dict(row) -> dict[str, Any]:
    return dict(row)


async def create_retrospective(
    user_id: str,
    session_name: str,
    slack_channel: str,
    slack_ts: str,
    good_points: str,
    improvements: str,
    learnings: str,
    action_item: str,
    emotion_score: int | None = None,
    emotion_reason: str | None = None,
    *,
    content_format: str = "structured",
    essay_title: str | None = None,
    essay_body: str | None = None,
) -> dict[str, Any]:
    content = _normalize_content({
        "good_points": good_points,
        "improvements": improvements,
        "learnings": learnings,
        "action_item": action_item,
        "emotion_score": emotion_score,
        "emotion_reason": emotion_reason,
        "content_format": content_format,
        "essay_title": essay_title,
        "essay_body": essay_body,
    })

    def insert() -> dict[str, Any]:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO retrospectives (
                    user_id, session_name, slack_channel, slack_ts,
                    good_points, improvements, learnings, action_item,
                    emotion_score, emotion_reason,
                    content_format, essay_title, essay_body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_name,
                    slack_channel,
                    slack_ts,
                    *(content[field] for field in SUBMISSION_CONTENT_FIELDS),
                ),
            )
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _to_dict(row)

    result = await asyncio.to_thread(insert)
    logger.info(f"회고 저장 성공 - User: {user_id}")
    return result


async def start_retrospective_submission(
    user_id: str,
    session_name: str,
    slack_channel: str,
    good_points: str,
    improvements: str,
    learnings: str,
    action_item: str,
    emotion_score: int | None = None,
    emotion_reason: str | None = None,
    is_test: bool = False,
    *,
    content_format: str = "structured",
    essay_title: str | None = None,
    essay_body: str | None = None,
) -> dict[str, Any]:
    """Slack 게시 전에 회고를 `pending` 상태로 먼저 저장한다.

    DB에 먼저 쓰기 때문에 저장이 실패하면 Slack에도 게시되지 않는다.
    같은 회차에 이미 게시 완료된 회고가 있으면 `RetrospectiveAlreadySubmitted`를
    올리고, 게시되지 못한 `pending` 회고가 남아 있으면 그 행을 재사용한다.
    """

    content = _normalize_content({
        "good_points": good_points,
        "improvements": improvements,
        "learnings": learnings,
        "action_item": action_item,
        "emotion_score": emotion_score,
        "emotion_reason": emotion_reason,
        "content_format": content_format,
        "essay_title": essay_title,
        "essay_body": essay_body,
    })
    values = tuple(content[field] for field in SUBMISSION_CONTENT_FIELDS)

    def insert() -> dict[str, Any]:
        with get_connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO retrospectives (
                        user_id, session_name, slack_channel, slack_ts,
                        good_points, improvements, learnings, action_item,
                        emotion_score, emotion_reason,
                        content_format, essay_title, essay_body,
                        slack_post_status, is_test_submission
                    ) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        user_id,
                        session_name,
                        slack_channel,
                        *values,
                        int(is_test),
                    ),
                )
            except sqlite3.IntegrityError:
                return _reuse_pending(connection, user_id, session_name, slack_channel, values)
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _to_dict(row)

    result = await asyncio.to_thread(insert)
    logger.info(f"회고 임시 저장 - User: {user_id}, ID: {result['id']}")
    return result


def _reuse_pending(
    connection: sqlite3.Connection,
    user_id: str,
    session_name: str,
    slack_channel: str,
    values: tuple[Any, ...],
) -> dict[str, Any]:
    """UNIQUE 제약에 걸린 경우 미게시 회고만 재사용한다."""
    existing = connection.execute(
        """
        SELECT * FROM retrospectives
         WHERE user_id = ? AND session_name = ? AND is_test_submission = 0
         ORDER BY id DESC
         LIMIT 1
        """,
        (user_id, session_name),
    ).fetchone()
    if existing is None:
        raise  # UNIQUE 이외의 제약 위반이므로 원래 IntegrityError를 그대로 올린다.
    if existing["slack_post_status"] != "pending":
        raise RetrospectiveAlreadySubmitted(
            f"{user_id}님은 {session_name} 회고를 이미 제출했습니다."
        )
    assignments = ", ".join(f"{field} = ?" for field in SUBMISSION_CONTENT_FIELDS)
    connection.execute(
        f"""
        UPDATE retrospectives
           SET slack_channel = ?, {assignments}, updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        (slack_channel, *values, existing["id"]),
    )
    row = connection.execute(
        "SELECT * FROM retrospectives WHERE id = ?", (existing["id"],)
    ).fetchone()
    return _to_dict(row)


async def mark_retrospective_posted(retrospective_id: int, slack_ts: str) -> dict[str, Any]:
    """Slack 게시에 성공한 회고에 메시지 타임스탬프를 기록한다."""

    def update() -> dict[str, Any]:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE retrospectives
                   SET slack_ts = ?,
                       slack_post_status = 'posted',
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (slack_ts, retrospective_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(
                    f"ID {retrospective_id}에 해당하는 회고를 찾을 수 없습니다."
                )
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (retrospective_id,)
            ).fetchone()
            return _to_dict(row)

    return await asyncio.to_thread(update)


async def discard_pending_retrospective(retrospective_id: int) -> bool:
    """Slack 게시에 실패한 미게시 회고를 되돌린다."""

    def delete() -> bool:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM retrospectives
                 WHERE id = ? AND slack_post_status = 'pending'
                """,
                (retrospective_id,),
            )
            return cursor.rowcount > 0

    discarded = await asyncio.to_thread(delete)
    if discarded:
        logger.info(f"미게시 회고 정리 - ID: {retrospective_id}")
    return discarded


async def count_pending_retrospectives() -> int:
    def select() -> int:
        with get_connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM retrospectives WHERE slack_post_status = 'pending'"
            ).fetchone()[0]

    return await asyncio.to_thread(select)


async def get_retrospective_by_id(retrospective_id: int) -> dict[str, Any]:
    def select() -> dict[str, Any]:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (retrospective_id,)
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"ID {retrospective_id}에 해당하는 회고를 찾을 수 없습니다."
                )
            return _to_dict(row)

    return await asyncio.to_thread(select)


async def get_retrospectives_by_user_id(user_id: str) -> list[dict[str, Any]]:
    def select() -> list[dict[str, Any]]:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM retrospectives
                 WHERE user_id = ?
                 ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
            return [_to_dict(row) for row in rows]

    return await asyncio.to_thread(select)


async def check_user_submitted_this_session(user_id: str, session_name: str) -> bool:
    def select() -> bool:
        with get_connection() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM retrospectives
                     WHERE user_id = ? AND session_name = ?
                     LIMIT 1
                    """,
                    (user_id, session_name),
                ).fetchone()
                is not None
            )

    return await asyncio.to_thread(select)


async def get_submitted_user_ids(
    session_name: str, *, include_test: bool = True, posted_only: bool = False
) -> set[str]:
    """회차에 회고를 남긴 사용자 ID를 모은다.

    미제출자에게 개인 리마인더를 보내는 쪽은 테스트 제출을 제출로 세면 안 된다.
    기존 호출부의 동작을 바꾸지 않으려고 `include_test` 기본값은 True로 둔다.
    `posted_only`를 켜면 DB에만 pending으로 남은 기록은 제출 완료로 세지 않는다.
    """

    def select() -> set[str]:
        query = "SELECT DISTINCT user_id FROM retrospectives WHERE session_name = ?"
        if not include_test:
            query += " AND is_test_submission = 0"
        if posted_only:
            query += " AND slack_post_status = 'posted'"
        with get_connection() as connection:
            return {
                row["user_id"] for row in connection.execute(query, (session_name,))
            }

    return await asyncio.to_thread(select)


async def update_retrospective(
    retrospective_id: int, data: dict[str, Any]
) -> dict[str, Any]:
    allowed = {
        "good_points",
        "improvements",
        "learnings",
        "action_item",
        "emotion_score",
        "emotion_reason",
        "essay_title",
        "essay_body",
    }
    updates = {key: value for key, value in data.items() if key in allowed}
    if not updates:
        return await get_retrospective_by_id(retrospective_id)

    def update() -> dict[str, Any]:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (retrospective_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(
                    f"ID {retrospective_id}에 해당하는 회고를 찾을 수 없습니다."
                )
            content = _normalize_content({**dict(existing), **updates})
            assignments = ", ".join(f"{field} = ?" for field in SUBMISSION_CONTENT_FIELDS)
            connection.execute(
                f"""
                UPDATE retrospectives
                   SET {assignments}, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                [*(content[field] for field in SUBMISSION_CONTENT_FIELDS), retrospective_id],
            )
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (retrospective_id,)
            ).fetchone()
            return _to_dict(row)

    result = await asyncio.to_thread(update)
    logger.info(f"회고 업데이트 성공 - ID: {retrospective_id}")
    return result


async def delete_retrospective(retrospective_id: int) -> bool:
    def delete() -> bool:
        with get_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM retrospectives WHERE id = ?", (retrospective_id,)
            )
            return cursor.rowcount > 0

    deleted = await asyncio.to_thread(delete)
    if deleted:
        logger.info(f"회고 삭제 성공 - ID: {retrospective_id}")
    return deleted


async def get_latest_retrospectives(limit: int = 10) -> list[dict[str, Any]]:
    def select() -> list[dict[str, Any]]:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM retrospectives
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_to_dict(row) for row in rows]

    return await asyncio.to_thread(select)
