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
) -> dict:
    def insert() -> dict:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO online_retro_time_polls (
                    meeting_date, session_name, team_channel, team_name,
                    slots_json, is_test
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_date,
                    session_name,
                    team_channel,
                    team_name,
                    json.dumps(slots, ensure_ascii=False),
                    int(is_test),
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


async def mark_poll_posted(poll_id: int, slack_ts: str) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_time_polls SET slack_ts = ? WHERE id = ?",
                (slack_ts, poll_id),
            )

    await asyncio.to_thread(update)


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


async def vote_counts(poll_id: int, slots: list[str]) -> tuple[dict[str, int], int]:
    def select() -> tuple[dict[str, int], int]:
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT slots_json FROM online_retro_time_votes WHERE poll_id = ?",
                (poll_id,),
            ).fetchall()
        counts = {slot: 0 for slot in slots}
        for row in rows:
            for slot in json.loads(row[0]):
                if slot in counts:
                    counts[slot] += 1
        return counts, len(rows)

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
