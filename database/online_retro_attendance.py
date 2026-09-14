"""온라인 회고 모임 출석 기록."""

import asyncio

from database.sqlite import get_connection


async def record_attendance(
    *, session_name: str, team_channel: str, user_id: str
) -> bool:
    """첫 출석 시각만 저장하고, 새 기록인지 반환한다."""

    def insert() -> bool:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO online_retro_attendance (
                    session_name, team_channel, user_id
                ) VALUES (?, ?, ?)
                """,
                (session_name, team_channel, user_id),
            )
            return cursor.rowcount == 1

    return await asyncio.to_thread(insert)


async def list_attendees(session_name: str, team_channel: str) -> list[dict]:
    """첫 출석 순서대로 참여자를 반환한다."""

    def select() -> list[dict]:
        with get_connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT user_id, attended_at
                      FROM online_retro_attendance
                     WHERE session_name = ? AND team_channel = ?
                     ORDER BY attended_at, id
                    """,
                    (session_name, team_channel),
                )
            ]

    return await asyncio.to_thread(select)


async def has_attended(
    *, session_name: str, team_channel: str, user_id: str
) -> bool:
    """사용자가 해당 회차에 출석 체크했는지 확인한다."""

    def select() -> bool:
        with get_connection() as connection:
            return connection.execute(
                """
                SELECT 1
                  FROM online_retro_attendance
                 WHERE session_name = ? AND team_channel = ? AND user_id = ?
                """,
                (session_name, team_channel, user_id),
            ).fetchone() is not None

    return await asyncio.to_thread(select)
