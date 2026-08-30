import asyncio
from typing import Any

from loguru import logger

from database.sqlite import get_connection


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
) -> dict[str, Any]:
    def insert() -> dict[str, Any]:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO retrospectives (
                    user_id, session_name, slack_channel, slack_ts,
                    good_points, improvements, learnings, action_item,
                    emotion_score, emotion_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_name,
                    slack_channel,
                    slack_ts,
                    good_points,
                    improvements,
                    learnings,
                    action_item,
                    emotion_score,
                    emotion_reason,
                ),
            )
            row = connection.execute(
                "SELECT * FROM retrospectives WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _to_dict(row)

    result = await asyncio.to_thread(insert)
    logger.info(f"회고 저장 성공 - User: {user_id}")
    return result


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
    }
    updates = {key: value for key, value in data.items() if key in allowed}
    if not updates:
        return await get_retrospective_by_id(retrospective_id)

    def update() -> dict[str, Any]:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with get_connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE retrospectives
                   SET {assignments}, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                [*updates.values(), retrospective_id],
            )
            if cursor.rowcount == 0:
                raise ValueError(
                    f"ID {retrospective_id}에 해당하는 회고를 찾을 수 없습니다."
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
