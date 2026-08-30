import asyncio
from typing import Any

from database.sqlite import get_connection


async def enqueue_ai_review(
    *,
    user_id: str,
    slack_channel: str,
    slack_ts: str,
    file_id: str,
    calendar_type: str,
    retrospective_text: str,
) -> int:
    def insert() -> int:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ai_review_jobs (
                    user_id, slack_channel, slack_ts, file_id,
                    calendar_type, retrospective_text
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    slack_channel,
                    slack_ts,
                    file_id,
                    calendar_type,
                    retrospective_text,
                ),
            )
            return int(cursor.lastrowid)

    return await asyncio.to_thread(insert)


async def claim_next_ai_review() -> dict[str, Any] | None:
    def claim() -> dict[str, Any] | None:
        connection = get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM ai_review_jobs
                 WHERE status = 'pending'
                 ORDER BY created_at, id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE ai_review_jobs
                   SET status = 'processing', attempts = attempts + 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (row["id"],),
            )
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM ai_review_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return dict(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return await asyncio.to_thread(claim)


async def complete_ai_review(job_id: int, feedback: str) -> None:
    def complete() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE ai_review_jobs
                   SET status = 'completed', feedback = ?, last_error = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (feedback, job_id),
            )

    await asyncio.to_thread(complete)


async def fail_ai_review(job_id: int, error: str, *, retry: bool) -> None:
    def fail() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE ai_review_jobs
                   SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                ("pending" if retry else "failed", error[:2000], job_id),
            )

    await asyncio.to_thread(fail)
