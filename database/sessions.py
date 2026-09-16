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
            SELECT s.name, s.due_at, s.announce_at, s.announcement, s.announced_at,
                   (SELECT COUNT(*) FROM retrospectives r
                     WHERE r.session_name = s.name AND r.is_test_submission = 0)
                   AS submissions
              FROM sessions s
             ORDER BY s.due_at, s.name
            """
        ).fetchall()
    return [{**_session(row), "submissions": row["submissions"]} for row in found]


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
        # 공지 시각을 비워 두면 그 회차만 조용히 공지가 빠진다. 마감 기준
        # 기본값을 채워 두고, 필요하면 관리자 웹에서 옮긴다.
        connection.execute(
            "INSERT INTO sessions (name, due_at, announce_at) VALUES (?, ?, ?)",
            (name, due_at.isoformat(), default_announce_at(due_at).isoformat()),
        )
    invalidate()


def update_due_at(name: str, due_at: datetime.datetime, *, now: datetime.datetime) -> None:
    """아직 오지 않은 회차의 마감 시각만 바꾼다.

    지난 회차의 마감을 옮기면 그 구간에 제출된 회고가 다른 회차에 속한 것처럼
    집계되므로, 이미 마감된 회차는 건드리지 않는다.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT due_at, announce_at, announced_at FROM sessions WHERE name = ?",
            (name,),
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

        # 마감을 옮기면 아직 나가지 않은 공지도 같은 간격으로 따라 옮긴다.
        # 그대로 두면 공지 시각이 마감 뒤로 넘어가 그 회차 공지가 조용히 빠진다.
        announce_at = None
        previous = _time(row["announce_at"])
        if previous is not None and row["announced_at"] is None:
            shifted = previous + (due_at - datetime.datetime.fromisoformat(row["due_at"]))
            announce_at = shifted.isoformat()
        connection.execute(
            """
            UPDATE sessions
               SET due_at = ?,
                   announce_at = COALESCE(?, announce_at),
                   updated_at = CURRENT_TIMESTAMP
             WHERE name = ?
            """,
            (due_at.isoformat(), announce_at, name),
        )
    invalidate()


# --- 매회차 제출 공지 -------------------------------------------------------
#
# 문구는 두 단계다. `announcement_templates`의 기본 문구가 모든 회차에 쓰이고,
# `sessions.announcement`에 값이 있으면 그 회차만 덮어쓴다. 발송 여부는
# `sessions.announced_at`으로 판단하므로 같은 회차 공지가 두 번 나가지 않는다.

TEMPLATE_KIND = "session_submission"
MAX_ANNOUNCEMENT_LENGTH = 2000
WEEKDAYS = "월화수목금토일"


def _time(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _session(row) -> dict:
    return {
        "name": row["name"],
        "due_at": datetime.datetime.fromisoformat(row["due_at"]),
        "announce_at": _time(row["announce_at"]),
        "announcement": row["announcement"],
        "announced_at": _time(row["announced_at"]),
    }


def default_announce_at(due_at: datetime.datetime) -> datetime.datetime:
    from constants import ANNOUNCE_LEAD

    return due_at - ANNOUNCE_LEAD


def get_template() -> str:
    """제출 공지 기본 문구. 비어 있으면 시드 원본인 코드 상수를 쓴다."""
    from constants import DEFAULT_SUBMISSION_ANNOUNCEMENT

    try:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT body FROM announcement_templates WHERE kind = ?",
                (TEMPLATE_KIND,),
            ).fetchone()
    except Exception as error:
        logger.warning("공지 기본 문구를 읽지 못해 코드 상수를 사용합니다 - {}", error)
        return DEFAULT_SUBMISSION_ANNOUNCEMENT
    return row["body"] if row else DEFAULT_SUBMISSION_ANNOUNCEMENT


def _validate_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise ScheduleError("공지 문구가 비어 있습니다.")
    if len(body) > MAX_ANNOUNCEMENT_LENGTH:
        raise ScheduleError(
            f"공지 문구는 {MAX_ANNOUNCEMENT_LENGTH}자까지 쓸 수 있습니다."
        )
    return body


def set_template(body: str) -> None:
    body = _validate_body(body)
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO announcement_templates (kind, body) VALUES (?, ?)
            ON CONFLICT(kind) DO UPDATE
               SET body = excluded.body, updated_at = CURRENT_TIMESTAMP
            """,
            (TEMPLATE_KIND, body),
        )


def render_announcement(body: str, *, name: str, due_at: datetime.datetime) -> str:
    """문구의 치환자를 회차 값으로 바꾼다.

    `str.format`을 쓰지 않는다. 문구에 남은 중괄호 하나가 발송 직전에
    예외를 던지면 그 회차 공지가 통째로 빠진다.
    """
    due = (
        f"{due_at.month}월 {due_at.day}일 "
        f"{WEEKDAYS[due_at.weekday()]}요일 {due_at:%H:%M}"
    )
    return body.replace("{회차}", name).replace("{마감}", due)


def get_session(name: str) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT name, due_at, announce_at, announcement, announced_at"
            "  FROM sessions WHERE name = ?",
            (name,),
        ).fetchone()
    return _session(row) if row else None


def _editable(connection, name: str, now: datetime.datetime):
    """공지를 아직 고칠 수 있는 회차인지 확인하고 행을 돌려준다."""
    row = connection.execute(
        "SELECT due_at, announce_at, announced_at FROM sessions WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise ScheduleError(f"`{name}` 회차를 찾을 수 없습니다.")
    if row["announced_at"]:
        raise ScheduleError(
            f"`{name}` 공지는 이미 나갔습니다. 문구를 고쳐도 다시 보내지 않습니다."
        )
    if datetime.datetime.fromisoformat(row["due_at"]) <= now:
        raise ScheduleError(f"이미 마감된 `{name}`의 공지는 고칠 수 없습니다.")
    return row


def set_announcement(
    name: str, body: str | None, *, now: datetime.datetime
) -> None:
    """회차 문구를 덮어쓴다. `body`가 None이면 기본 문구로 되돌린다."""
    with get_connection() as connection:
        _editable(connection, name, now)
        connection.execute(
            "UPDATE sessions SET announcement = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE name = ?",
            (_validate_body(body) if body is not None else None, name),
        )


def update_announce_at(
    name: str, announce_at: datetime.datetime | None, *, now: datetime.datetime
) -> None:
    """공지 시각을 옮긴다. `announce_at`이 None이면 그 회차 공지를 끈다."""
    with get_connection() as connection:
        row = _editable(connection, name, now)
        if announce_at is not None:
            due_at = datetime.datetime.fromisoformat(row["due_at"])
            if announce_at >= due_at:
                raise ScheduleError("공지 시각은 마감보다 앞서야 합니다.")
        connection.execute(
            "UPDATE sessions SET announce_at = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE name = ?",
            (announce_at.isoformat() if announce_at else None, name),
        )


def pending_announcements(now: datetime.datetime) -> list[dict]:
    """지금 보내야 할 회차 공지. 문구는 치환까지 끝난 상태로 돌려준다.

    `announce_at <= now < due_at`인 회차만 고른다. 마감이 지난 회차를 빼기
    때문에, 봇이 오래 멈췄다 올라와도 묵은 공지가 한꺼번에 나가지 않는다.
    """
    with get_connection() as connection:
        found = connection.execute(
            "SELECT name, due_at, announce_at, announcement, announced_at"
            "  FROM sessions"
            " WHERE announce_at IS NOT NULL AND announced_at IS NULL"
            " ORDER BY due_at, name"
        ).fetchall()

    # 저장된 시각은 모두 타임존이 붙어 있지만 오프셋 표기가 섞일 수 있어
    # 문자열이 아니라 datetime으로 비교한다.
    template = None
    ready = []
    for row in found:
        session = _session(row)
        if not (session["announce_at"] <= now < session["due_at"]):
            continue
        if session["announcement"] is None and template is None:
            template = get_template()
        body = session["announcement"] or template
        ready.append(
            {
                "name": session["name"],
                "due_at": session["due_at"],
                "body": render_announcement(
                    body, name=session["name"], due_at=session["due_at"]
                ),
            }
        )
    return ready


def mark_announced(name: str, sent_at: datetime.datetime) -> bool:
    """공지 발송을 기록한다. 이미 기록돼 있으면 False."""
    with get_connection() as connection:
        changed = connection.execute(
            "UPDATE sessions SET announced_at = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE name = ? AND announced_at IS NULL",
            (sent_at.isoformat(), name),
        ).rowcount
    return changed > 0
