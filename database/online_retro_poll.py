"""팀별 온라인 회고 시간 투표 저장소."""

import asyncio
import json

from database.sqlite import get_connection


async def create_poll(
    *,
    meeting_date: str,
    session_name: str,
    team_channel: str,
    team_name: str,
    slots: list[str],
    is_test: bool,
    intro_template: str | None = None,
) -> dict:
    """팀·날짜마다 투표 하나만 만든다.

    게시에 실패해 아직 Slack에 없는 투표는 다시 올릴 때 팀 이름과 문구를
    새 값으로 바꾼다. 이미 올라간 투표는 그대로 둔다.
    """

    def insert() -> dict:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO online_retro_time_polls (
                    meeting_date, session_name, team_channel, team_name,
                    slots_json, is_test, intro_template
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_date, team_channel, is_test) DO UPDATE SET
                    team_name = excluded.team_name,
                    intro_template = excluded.intro_template
                 WHERE online_retro_time_polls.slack_ts IS NULL
                """,
                (
                    meeting_date,
                    session_name,
                    team_channel,
                    team_name,
                    json.dumps(slots, ensure_ascii=False),
                    int(is_test),
                    intro_template,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM online_retro_time_polls
                 WHERE meeting_date = ? AND team_channel = ? AND is_test = ?
                """,
                (meeting_date, team_channel, int(is_test)),
            ).fetchone()
            return dict(row)

    return await asyncio.to_thread(insert)


async def mark_poll_posted(poll_id: int, slack_ts: str, message_channel: str) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_time_polls SET slack_ts = ?, message_channel = ? WHERE id = ?",
                (slack_ts, message_channel, poll_id),
            )

    await asyncio.to_thread(update)


async def find_poll(*, meeting_date: str, team_channel: str, is_test: bool) -> dict | None:
    """공지 하나에서 누른 사람의 팀 투표를 찾는다."""

    def select() -> dict | None:
        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM online_retro_time_polls
                 WHERE meeting_date = ? AND team_channel = ? AND is_test = ?
                """,
                (meeting_date, team_channel, int(is_test)),
            ).fetchone()
        return dict(row) if row else None

    return await asyncio.to_thread(select)


async def message_polls(*, meeting_date: str, is_test: bool) -> list[dict]:
    """같은 날 공지 하나에 묶인 팀 투표들과 팀별 응답 인원."""

    def select() -> list[dict]:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, COUNT(v.user_id) AS voters
                  FROM online_retro_time_polls p
                  LEFT JOIN online_retro_time_votes v ON v.poll_id = p.id
                 WHERE p.meeting_date = ? AND p.is_test = ?
                 GROUP BY p.id
                 ORDER BY p.team_name, p.id
                """,
                (meeting_date, int(is_test)),
            ).fetchall()
        return [dict(row) for row in rows]

    return await asyncio.to_thread(select)


async def get_poll(poll_id: int) -> dict | None:
    def select() -> dict | None:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM online_retro_time_polls WHERE id = ?", (poll_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["slots"] = json.loads(result.pop("slots_json"))
            return result

    return await asyncio.to_thread(select)


async def save_vote(*, poll_id: int, user_id: str, slots: list[str]) -> None:
    def upsert() -> None:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO online_retro_time_votes (poll_id, user_id, slots_json)
                VALUES (?, ?, ?)
                ON CONFLICT(poll_id, user_id) DO UPDATE SET
                    slots_json = excluded.slots_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (poll_id, user_id, json.dumps(slots, ensure_ascii=False)),
            )

    await asyncio.to_thread(upsert)


async def get_vote(poll_id: int, user_id: str) -> list[str]:
    """사용자의 이전 선택. 투표 창을 다시 열 때 미리 체크해 둔다."""

    def select() -> list[str]:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT slots_json FROM online_retro_time_votes WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()
        return json.loads(row[0]) if row else []

    return await asyncio.to_thread(select)


async def list_polls() -> list[dict]:
    """관리자 화면용. 투표별 슬롯 집계와 참여 인원을 함께 돌려준다."""

    def select() -> list[dict]:
        with get_connection() as connection:
            polls = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM online_retro_time_polls
                     ORDER BY meeting_date DESC, team_name
                    """
                )
            ]
            votes: dict[int, list[str]] = {}
            for row in connection.execute(
                "SELECT poll_id, slots_json FROM online_retro_time_votes"
            ):
                votes.setdefault(row["poll_id"], []).append(row["slots_json"])
        for poll in polls:
            poll["slots"] = json.loads(poll.pop("slots_json"))
            counts = {slot: 0 for slot in poll["slots"]}
            raw_votes = votes.get(poll["id"], [])
            for payload in raw_votes:
                for slot in json.loads(payload):
                    if slot in counts:
                        counts[slot] += 1
            poll["counts"] = counts
            poll["voters"] = len(raw_votes)
        return polls

    return await asyncio.to_thread(select)
