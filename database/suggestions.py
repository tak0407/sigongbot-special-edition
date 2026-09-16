"""봇 개선 제안 저장 및 상태 관리."""

import asyncio
from typing import Any

from loguru import logger

from database.sqlite import get_connection

STATUSES = ("pending", "in_progress", "completed")


async def create_suggestion(
    *, category: str, content: str, user_id: str, submission_channel: str
) -> dict[str, Any]:
    """관리자 알림보다 먼저 제안을 영속화한다."""

    def insert() -> dict[str, Any]:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO bot_improvement_suggestions (
                    category, content, user_id, submission_channel
                ) VALUES (?, ?, ?, ?)
                """,
                (category, content, user_id, submission_channel),
            )
            row = connection.execute(
                "SELECT * FROM bot_improvement_suggestions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return dict(row)

    result = await asyncio.to_thread(insert)
    logger.info(
        "봇 개선 제안 저장 성공 - suggestion_id={}, user_id={}",
        result["id"],
        user_id,
    )
    return result


def update_suggestion_status(suggestion_id: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE bot_improvement_suggestions
               SET status = ?, updated_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (status, suggestion_id),
        )
    return cursor.rowcount > 0
