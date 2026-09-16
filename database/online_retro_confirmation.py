"""확정된 팀별 온라인 회고 시간과 Google Calendar/Meet 생성 결과 저장소.

Google API를 부르기 전에 확정 요청을 `pending`으로 먼저 남기고, 성공한 뒤에
이벤트 ID와 Meet 주소를 채운다. 그래서 호출 도중에 봇이 죽어도 어떤 팀의 확정이
진행 중이었는지 DB만 보면 알 수 있고, 재시도는 같은 행을 갱신한다.
"""

import asyncio

from database.sqlite import get_connection

SCHEDULABLE_STATUS = "confirmed"


async def reserve_confirmation(
    *,
    poll: dict,
    slot: str,
    starts_at: str,
    ends_at: str,
    calendar_event_id: str,
    conference_request_id: str,
) -> dict:
    """확정 요청을 pending으로 기록하고 현재 행을 돌려준다.

    이미 있는 행이면 시간만 갱신한다. `calendar_event_id`와
    `conference_request_id`는 처음 값을 그대로 지켜, 재시도가 새 이벤트나 새
    회의를 만들지 않게 한다.
    """

    def upsert() -> dict:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO online_retro_confirmed_meetings (
                    poll_id, meeting_date, session_name, team_channel, team_name,
                    slot, starts_at, ends_at, calendar_event_id,
                    conference_request_id, status, is_test
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(poll_id) DO UPDATE SET
                    slot = excluded.slot,
                    starts_at = excluded.starts_at,
                    ends_at = excluded.ends_at,
                    status = 'pending',
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    poll["id"],
                    poll["meeting_date"],
                    poll["session_name"],
                    poll["team_channel"],
                    poll["team_name"],
                    slot,
                    starts_at,
                    ends_at,
                    calendar_event_id,
                    conference_request_id,
                    int(bool(poll["is_test"])),
                ),
            )
            row = connection.execute(
                "SELECT * FROM online_retro_confirmed_meetings WHERE poll_id = ?",
                (poll["id"],),
            ).fetchone()
            return dict(row)

    return await asyncio.to_thread(upsert)


async def mark_confirmed(
    *,
    confirmation_id: int,
    calendar_event_id: str,
    meet_url: str,
    meet_access_type: str | None,
) -> dict:
    def update() -> dict:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE online_retro_confirmed_meetings
                   SET calendar_event_id = ?,
                       meet_url = ?,
                       meet_access_type = ?,
                       status = 'confirmed',
                       last_error = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (calendar_event_id, meet_url, meet_access_type, confirmation_id),
            )
            row = connection.execute(
                "SELECT * FROM online_retro_confirmed_meetings WHERE id = ?",
                (confirmation_id,),
            ).fetchone()
            return dict(row)

    return await asyncio.to_thread(update)


async def mark_failed(*, confirmation_id: int, error: str) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE online_retro_confirmed_meetings
                   SET status = 'failed',
                       last_error = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (error[:500], confirmation_id),
            )

    await asyncio.to_thread(update)


async def mark_cancelled(confirmation_id: int) -> None:
    """이벤트를 비우지 않고 상태만 바꾼다. 같은 ID로 다시 확정할 수 있다."""

    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE online_retro_confirmed_meetings
                   SET status = 'cancelled',
                       last_error = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (confirmation_id,),
            )

    await asyncio.to_thread(update)


async def mark_announced(*, confirmation_id: int, slack_ts: str) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE online_retro_confirmed_meetings
                   SET announced_slack_ts = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (slack_ts, confirmation_id),
            )

    await asyncio.to_thread(update)


async def get_confirmation(poll_id: int) -> dict | None:
    def select() -> dict | None:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM online_retro_confirmed_meetings WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(select)


async def find_confirmation(*, session_name: str, team_channel: str) -> dict | None:
    """스케줄러와 Slack 핸들러가 회차·팀으로 확정 모임을 찾을 때 쓴다."""

    def select() -> dict | None:
        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM online_retro_confirmed_meetings
                 WHERE session_name = ? AND team_channel = ? AND status = ?
                 ORDER BY starts_at DESC
                 LIMIT 1
                """,
                (session_name, team_channel, SCHEDULABLE_STATUS),
            ).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(select)


async def list_confirmed() -> list[dict]:
    """스케줄러가 쓸 수 있는 확정 모임만 돌려준다."""

    def select() -> list[dict]:
        with get_connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM online_retro_confirmed_meetings
                     WHERE status = ?
                       AND meet_url IS NOT NULL
                       AND meet_url <> ''
                     ORDER BY starts_at
                    """,
                    (SCHEDULABLE_STATUS,),
                )
            ]

    return await asyncio.to_thread(select)
