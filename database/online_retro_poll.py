"""팀별 온라인 회고 시간 투표 저장소."""

import asyncio
import datetime
import json

from config import settings
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
            result = []
            for row in rows:
                poll = dict(row)
                confirmation = connection.execute(
                    "SELECT * FROM online_retro_confirmed_meetings WHERE poll_id = ?",
                    (poll["id"],),
                ).fetchone()
                poll["confirmation"] = dict(confirmation) if confirmation else None
                poll["attendee_ids"] = []
                if confirmation and confirmation["status"] == "confirmed":
                    poll["attendee_ids"] = [
                        vote["user_id"] for vote in connection.execute(
                            "SELECT user_id, slots_json FROM online_retro_time_votes WHERE poll_id = ? ORDER BY user_id",
                            (poll["id"],),
                        )
                        if confirmation["slot"] in json.loads(vote["slots_json"])
                    ]
                result.append(poll)
        return result

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


def _sync_completion(connection, poll: dict, now: datetime.datetime) -> None:
    members = {
        user for user, channel in settings.SUBMISSION_DESTINATIONS.items()
        if channel == poll["team_channel"]
    }
    voters = {
        row["user_id"] for row in connection.execute(
            "SELECT user_id FROM online_retro_time_votes WHERE poll_id = ?", (poll["id"],)
        )
    }
    complete = not poll["is_test"] and bool(members) and members <= voters
    if complete and not poll["all_voted_at"]:
        connection.execute(
            "UPDATE online_retro_time_polls SET all_voted_at = ?, message_dirty = message_dirty + 1 WHERE id = ?",
            (now.isoformat(), poll["id"]),
        )
    elif not complete and poll["all_voted_at"]:
        connection.execute(
            """UPDATE online_retro_time_polls
                  SET all_voted_at = NULL, auto_confirm_attempted_at = NULL,
                      auto_confirm_error = NULL, message_dirty = message_dirty + 1
                WHERE id = ?""",
            (poll["id"],),
        )


async def sync_poll_completion(
    poll_id: int, *, now: datetime.datetime
) -> dict:
    """기존 투표와 팀 구성 변경도 확인한다. 최초 전원 응답 시각은 유지한다."""
    def update() -> dict:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            poll = dict(connection.execute(
                "SELECT * FROM online_retro_time_polls WHERE id = ?", (poll_id,)
            ).fetchone())
            _sync_completion(connection, poll, now)
            return dict(connection.execute(
                "SELECT * FROM online_retro_time_polls WHERE id = ?", (poll_id,)
            ).fetchone())

    return await asyncio.to_thread(update)


async def save_vote(
    *, poll_id: int, user_id: str, slots: list[str],
    now: datetime.datetime | None = None,
) -> bool:
    """응답과 전원 완료 시각을 같은 트랜잭션에 저장한다. 마감된 투표는 바꾸지 않는다."""
    now = now or datetime.datetime.now(datetime.timezone.utc)

    def upsert() -> bool:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            confirmation = connection.execute(
                "SELECT status FROM online_retro_confirmed_meetings WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            if confirmation and confirmation["status"] in {"pending", "confirmed"}:
                return False
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
            poll = dict(connection.execute(
                "SELECT * FROM online_retro_time_polls WHERE id = ?", (poll_id,)
            ).fetchone())
            _sync_completion(connection, poll, now)
            connection.execute(
                "UPDATE online_retro_time_polls SET message_dirty = message_dirty + 1 WHERE id = ?",
                (poll_id,),
            )
            return True

    return await asyncio.to_thread(upsert)


async def poll_is_closed(poll_id: int) -> bool:
    def select() -> bool:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT status FROM online_retro_confirmed_meetings WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            return bool(row and row["status"] in {"pending", "confirmed"})
    return await asyncio.to_thread(select)


async def mark_auto_confirm_attempt(poll_id: int, *, now: datetime.datetime) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_time_polls SET auto_confirm_attempted_at = ? WHERE id = ?",
                (now.isoformat(), poll_id),
            )
    await asyncio.to_thread(update)


async def set_auto_confirm_error(poll_id: int, error: str | None) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                """UPDATE online_retro_time_polls
                      SET auto_confirm_error = ?, message_dirty = message_dirty + 1
                    WHERE id = ? AND auto_confirm_error IS NOT ?""",
                (error[:500] if error else None, poll_id, error[:500] if error else None),
            )
    await asyncio.to_thread(update)


async def mark_poll_message_dirty(poll_id: int) -> None:
    def update() -> None:
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_time_polls SET message_dirty = message_dirty + 1 WHERE id = ?",
                (poll_id,),
            )
    await asyncio.to_thread(update)


async def mark_poll_message_clean(shown: list[dict]) -> None:
    """그린 버전만 완료 처리한다. Slack 요청 도중 새 응답이 저장되면 대기를 유지한다."""
    def update() -> None:
        with get_connection() as connection:
            for poll in shown:
                connection.execute(
                    "UPDATE online_retro_time_polls SET message_dirty = 0 WHERE id = ? AND message_dirty = ?",
                    (poll["id"], poll["message_dirty"]),
                )
                confirmation = poll.get("confirmation")
                if confirmation and confirmation["status"] == "confirmed":
                    connection.execute(
                        "UPDATE online_retro_confirmed_meetings SET announced_slack_ts = ? WHERE id = ?",
                        (poll["slack_ts"], confirmation["id"]),
                    )
    await asyncio.to_thread(update)


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


async def get_slot_voters(poll_id: int, slot: str) -> list[str]:
    """해당 투표에서 지정한 시간에 참석 가능하다고 응답한 사용자만 돌려준다."""

    def select() -> list[str]:
        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id, slots_json FROM online_retro_time_votes
                 WHERE poll_id = ? ORDER BY user_id
                """,
                (poll_id,),
            ).fetchall()
        return [row["user_id"] for row in rows if slot in json.loads(row["slots_json"])]

    return await asyncio.to_thread(select)


async def list_polls() -> list[dict]:
    """관리자 화면용. 투표별 슬롯 집계, 참여 인원, 사람별 선택을 함께 돌려준다."""

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
            votes: dict[int, dict[str, list[str]]] = {}
            for row in connection.execute(
                "SELECT poll_id, user_id, slots_json FROM online_retro_time_votes"
            ):
                votes.setdefault(row["poll_id"], {})[row["user_id"]] = json.loads(
                    row["slots_json"]
                )
        for poll in polls:
            poll["slots"] = json.loads(poll.pop("slots_json"))
            poll["votes"] = votes.get(poll["id"], {})
            counts = {slot: 0 for slot in poll["slots"]}
            for chosen in poll["votes"].values():
                # 시간대 목록이 바뀐 뒤 예전 투표에 들어온 응답도 센다.
                for slot in chosen:
                    counts[slot] = counts.get(slot, 0) + 1
            poll["counts"] = counts
            poll["voters"] = len(poll["votes"])
        return polls

    return await asyncio.to_thread(select)
