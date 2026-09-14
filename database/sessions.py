"""회차 일정 읽기와 편집.

일정은 `sessions` 테이블이 정본이고, 마이그레이션 9에서 `constants.py` 값으로
시드된다. `get_current_session_info()`가 회고 제출·공지·집계 전부의 기준이라
호출이 잦으므로 읽은 결과를 캐시하고, 편집할 때만 비운다.
"""

import datetime
import re
import threading

from loguru import logger

from database.sqlite import get_connection

# 회차 이름은 회고 행에 문자열로 박히는 조인 키라 길이와 모양을 좁게 잡는다.
NAME_PATTERN = re.compile(r"^[0-9A-Za-z가-힣][0-9A-Za-z가-힣 ]{0,30}$")

_lock = threading.Lock()
_cache: tuple[list[str], list[datetime.datetime]] | None = None


class ScheduleError(Exception):
    """관리자에게 그대로 보여 줄 수 있는 일정 편집 오류."""


def _read() -> tuple[list[str], list[datetime.datetime]]:
    with get_connection() as connection:
        found = connection.execute(
            "SELECT name, due_at FROM sessions ORDER BY due_at, name"
        ).fetchall()
    names = [row["name"] for row in found]
    dues = [datetime.datetime.fromisoformat(row["due_at"]) for row in found]
    return names, dues


def load_schedule() -> tuple[list[str], list[datetime.datetime]]:
    """(회차 이름, 마감 시각)을 마감 순으로 돌려준다.

    일정을 읽지 못하면 봇이 회차를 판정하지 못해 제출 자체가 막힌다.
    그래서 테이블이 없거나 비어 있으면 시드 원본인 코드 상수로 되돌아간다.
    """
    global _cache

    with _lock:
        if _cache is not None:
            return _cache
        try:
            names, dues = _read()
        except Exception as error:
            logger.warning("회차 일정을 읽지 못해 코드 상수를 사용합니다 - {}", error)
            names, dues = [], []
        if not names:
            from constants import DUE_DATES, SESSION_NAMES

            names, dues = list(SESSION_NAMES), list(DUE_DATES)
        _cache = (names, dues)
        return _cache


def invalidate() -> None:
    global _cache

    with _lock:
        _cache = None


def list_sessions() -> list[dict]:
    """편집 화면이 쓰는 회차 목록. 제출 건수를 함께 센다."""
    with get_connection() as connection:
        found = connection.execute(
            """
            SELECT s.name, s.due_at,
                   (SELECT COUNT(*) FROM retrospectives r
                     WHERE r.session_name = s.name AND r.is_test_submission = 0)
                   AS submissions
              FROM sessions s
             ORDER BY s.due_at, s.name
            """
        ).fetchall()
    return [
        {
            "name": row["name"],
            "due_at": datetime.datetime.fromisoformat(row["due_at"]),
            "submissions": row["submissions"],
        }
        for row in found
    ]


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_PATTERN.match(name):
        raise ScheduleError("회차 이름은 한글·영문·숫자와 공백만 쓸 수 있습니다.")
    return name


def add_session(name: str, due_at: datetime.datetime) -> None:
    """새 회차를 맨 뒤에 붙인다.

    회차 판정은 마감 시각이 오름차순이라는 전제 위에서 돌아간다. 중간에
    끼워 넣으면 이미 제출된 회고가 다른 회차 구간으로 넘어가므로 막는다.
    """
    name = _validate_name(name)
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sessions WHERE name = ?", (name,)
        ).fetchone()
        if exists:
            raise ScheduleError(f"`{name}`은(는) 이미 있는 회차입니다.")
        last = connection.execute(
            "SELECT name, due_at FROM sessions ORDER BY due_at DESC, name DESC LIMIT 1"
        ).fetchone()
        if last and due_at <= datetime.datetime.fromisoformat(last["due_at"]):
            raise ScheduleError(
                f"마감은 마지막 회차(`{last['name']}`)보다 뒤여야 합니다."
            )
        connection.execute(
            "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
            (name, due_at.isoformat()),
        )
    invalidate()


def update_due_at(name: str, due_at: datetime.datetime, *, now: datetime.datetime) -> None:
    """아직 오지 않은 회차의 마감 시각만 바꾼다.

    지난 회차의 마감을 옮기면 그 구간에 제출된 회고가 다른 회차에 속한 것처럼
    집계되므로, 이미 마감된 회차는 건드리지 않는다.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM sessions WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise ScheduleError(f"`{name}` 회차를 찾을 수 없습니다.")
        if datetime.datetime.fromisoformat(row["due_at"]) <= now:
            raise ScheduleError(f"이미 마감된 `{name}`의 마감 시각은 바꿀 수 없습니다.")
        if due_at <= now:
            raise ScheduleError("마감 시각은 현재보다 뒤여야 합니다.")

        neighbours = connection.execute(
            """
            SELECT name, due_at FROM sessions
             WHERE name != ?
             ORDER BY due_at, name
            """,
            (name,),
        ).fetchall()
        # 마감 시각이 같으면 회차 구간이 겹쳐 제출이 어느 회차로 갈지 정해지지 않는다.
        for other in neighbours:
            if datetime.datetime.fromisoformat(other["due_at"]) == due_at:
                raise ScheduleError(
                    f"`{other['name']}`과(와) 마감 시각이 같을 수 없습니다."
                )

        connection.execute(
            "UPDATE sessions SET due_at = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (due_at.isoformat(), name),
        )
    invalidate()
