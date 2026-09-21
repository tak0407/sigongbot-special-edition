"""회차 회고 패스 기록과 사용 가능 여부 판정.

패스는 기수당 MAX_PASS_COUNT회까지, 직전 회차에 쓰지 않았을 때만 쓸 수 있다.
판정에 필요한 조회를 한곳에 모아 두어 모달과 리마인더가 같은 규칙을 본다.
"""

import asyncio
import sqlite3
from typing import NamedTuple

from constants import MAX_PASS_COUNT
from database.sessions import load_schedule
from database.sqlite import get_connection
from utils import cohort_of


class PassAlreadyUsed(Exception):
    """같은 회차에 패스가 이미 기록돼 있다."""


class PassEligibility(NamedTuple):
    allowed: bool
    remaining: int
    reason: str


def previous_session(session_name: str) -> str | None:
    """같은 기수에서 바로 앞 회차를 돌려준다. 기수의 첫 회차면 None."""
    names, _ = load_schedule()
    try:
        index = names.index(session_name)
    except ValueError:
        return None
    if index == 0:
        return None
    earlier = names[index - 1]
    if cohort_of(earlier) != cohort_of(session_name):
        return None
    return earlier


def _used_sessions(connection, user_id: str, cohort: str) -> set[str]:
    return {
        row["session_name"]
        for row in connection.execute(
            "SELECT session_name FROM session_passes WHERE user_id = ? AND cohort = ?",
            (user_id, cohort),
        )
    }


def check_eligibility(user_id: str, session_name: str) -> PassEligibility:
    """패스를 쓸 수 있는지, 남은 횟수와 함께 판정한다."""
    cohort = cohort_of(session_name)
    with get_connection() as connection:
        used = _used_sessions(connection, user_id, cohort)
    remaining = max(0, MAX_PASS_COUNT - len(used))

    if session_name in used:
        return PassEligibility(False, remaining, f"이미 `{session_name}` 패스를 사용했어요.")
    if remaining == 0:
        return PassEligibility(
            False,
            0,
            f"{cohort}에 쓸 수 있는 패스 {MAX_PASS_COUNT}회를 모두 사용했어요.",
        )

    earlier = previous_session(session_name)
    if earlier and earlier in used:
        return PassEligibility(
            False,
            remaining,
            f"직전 회차(`{earlier}`)에 패스를 사용해서 이번 회차에는 연속으로 쓸 수 없어요.",
        )
    return PassEligibility(True, remaining, "")


def record_pass(
    *, user_id: str, session_name: str, team_channel: str, slack_ts: str | None = None
) -> int:
    """패스를 기록한다. 같은 회차에 두 번 기록되면 PassAlreadyUsed를 올린다."""
    with get_connection() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO session_passes
                    (user_id, session_name, cohort, team_channel, slack_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, session_name, cohort_of(session_name), team_channel, slack_ts),
            )
        except sqlite3.IntegrityError as error:
            raise PassAlreadyUsed(session_name) from error
        return int(cursor.lastrowid)


def attach_message(pass_id: int, slack_ts: str) -> None:
    """공개 안내를 올린 뒤 그 메시지 ts를 기록한다."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE session_passes SET slack_ts = ? WHERE id = ?", (slack_ts, pass_id)
        )


def passed_user_ids(session_name: str) -> set[str]:
    with get_connection() as connection:
        return {
            row["user_id"]
            for row in connection.execute(
                "SELECT user_id FROM session_passes WHERE session_name = ?",
                (session_name,),
            )
        }


def passes_by_session() -> dict[str, set[str]]:
    """관리자 화면이 제출·미제출과 구분해 표시할 수 있도록 회차별로 모은다."""
    result: dict[str, set[str]] = {}
    with get_connection() as connection:
        for row in connection.execute(
            "SELECT session_name, user_id FROM session_passes"
        ):
            result.setdefault(row["session_name"], set()).add(row["user_id"])
    return result


async def check_eligibility_async(user_id: str, session_name: str) -> PassEligibility:
    return await asyncio.to_thread(check_eligibility, user_id, session_name)


async def passed_user_ids_async(session_name: str) -> set[str]:
    return await asyncio.to_thread(passed_user_ids, session_name)
