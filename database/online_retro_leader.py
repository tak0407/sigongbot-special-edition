"""온라인 회고 모임 이끌이 추첨과 재시작 후 결과 보존."""

import asyncio
import datetime
import random

from database.sqlite import get_connection


async def choose_meeting_leader(
    *, session_name: str, team_channel: str,
    starts_at: datetime.datetime, now: datetime.datetime,
) -> str | None:
    """시작 시각 출석자 중 한 명을 뽑는다. 아무도 없었다면 입장자가 생길 때 뽑는다."""
    if now < starts_at:
        return None
    start = starts_at.astimezone(datetime.timezone.utc).isoformat()
    current = now.astimezone(datetime.timezone.utc).isoformat()

    def select() -> str | None:
        with get_connection() as connection:
            # 병렬 스케줄러와 재시도도 같은 추첨 결과를 읽도록 선택·저장을 묶는다.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT user_id FROM online_retro_meeting_leaders
                 WHERE session_name = ? AND team_channel = ? AND starts_at = ?
                """,
                (session_name, team_channel, start),
            ).fetchone()
            if existing:
                return existing["user_id"]
            query = """
                SELECT user_id FROM online_retro_attendance
                 WHERE session_name = ? AND team_channel = ?
                   AND julianday(attended_at) <= julianday(?)
                 ORDER BY user_id
            """
            candidates = [row["user_id"] for row in connection.execute(
                query, (session_name, team_channel, start)
            )]
            if not candidates:
                candidates = [row["user_id"] for row in connection.execute(
                    query, (session_name, team_channel, current)
                )]
            if not candidates:
                return None
            user_id = random.choice(candidates)
            connection.execute(
                """
                INSERT INTO online_retro_meeting_leaders
                    (session_name, team_channel, starts_at, user_id)
                VALUES (?, ?, ?, ?)
                """,
                (session_name, team_channel, start, user_id),
            )
            return user_id

    return await asyncio.to_thread(select)
