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
from database.submission_stats import session_submission_counts

# 회차 이름은 회고 행에 문자열로 박히는 조인 키라 길이와 모양을 좁게 잡는다.
NAME_PATTERN = re.compile(r"^[0-9A-Za-z가-힣][0-9A-Za-z가-힣 ]{0,30}$")

_lock = threading.Lock()
_cache: tuple[list[str], list[datetime.datetime]] | None = None


class ScheduleError(Exception):
    """관리자에게 그대로 보여 줄 수 있는 일정 편집 오류."""


def _read() -> tuple[list[str], list[datetime.datetime]]:
    with get_connection() as connection:
        found = connection.execute(
            # 쉬어가는 주로 표시된 회차는 회차 판정에서 빠진다. 그 주에는
            # 마감이 없고, 그 사이 제출은 다음 회차로 간다.
            "SELECT name, due_at FROM sessions WHERE resting = 0 ORDER BY due_at, name"
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
                   s.resting
              FROM sessions s
             ORDER BY s.due_at, s.name
            """
        ).fetchall()
        counts = {row["session_name"]: row for row in session_submission_counts(connection)}
    return [
        {
            **_session(row),
            "submissions": counts.get(row["name"], {}).get("submitters", 0),
            "separate_submissions": counts.get(row["name"], {}).get("separate_submitters", 0),
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


# 연휴로 한 번에 미룰 수 있는 한도. 실수로 몇 달치 일정을 통째로 밀어 버리는
# 일을 막는 선이고, 그보다 길게 쉬려면 나눠서 미룬다.
MAX_POSTPONE_WEEKS = 8


def postpone_from(name: str, weeks: int, *, now: datetime.datetime) -> dict:
    """연휴로 한 주 쉬어갈 때, 고른 회차부터 마지막 회차까지 한꺼번에 미룬다.

    회차를 하나씩 옮기면 뒤 회차와 마감이 겹치거나 순서가 뒤집힌다. 뒤쪽을
    통째로 같은 간격만큼 미루면 회차 사이 간격과 순서가 그대로 보존되고,
    쉬어가는 주만 일정에서 비워진다. 회차 이름은 회고 행에 박히는 조인 키라
    건드리지 않는다.

    밀고 나면 원래 자리가 비므로 그 자리에 쉬어가는 `추가 회차`를 세워 둔다.
    관리자가 회차로 쓰기로 하면 보충 회차가 되고, 미루기를 되돌리려면
    `withdraw_rest_week`로 그 주를 빼고 뒤를 당긴다.

    돌려주는 값의 `announced`는 이미 공지가 나간 회차이고 `filled`는 새로 세운
    회차다. 공지가 나간 회차의 본문에는 옛 마감이 적혀 있어 관리자가 따로
    알려야 한다.
    """
    if weeks < 1 or weeks > MAX_POSTPONE_WEEKS:
        raise ScheduleError(
            f"미룰 주 수는 1~{MAX_POSTPONE_WEEKS}주 사이여야 합니다."
        )
    delta = datetime.timedelta(weeks=weeks)

    with get_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM sessions WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise ScheduleError(f"`{name}` 회차를 찾을 수 없습니다.")
        from_due = datetime.datetime.fromisoformat(row["due_at"])
        # 마감이 지난 회차를 옮기면 그 구간에 제출된 회고가 다른 회차에 속한
        # 것처럼 집계된다.
        if from_due <= now:
            raise ScheduleError(f"이미 마감된 `{name}`부터는 미룰 수 없습니다.")

        found = connection.execute(
            "SELECT name, due_at, announce_at, announcement, announced_at"
            "  FROM sessions"
        ).fetchall()
        # 저장된 시각은 오프셋 표기가 섞일 수 있어 문자열이 아니라 datetime으로
        # 고른다.
        targets = [row for row in map(_session, found) if row["due_at"] >= from_due]
        targets.sort(key=lambda row: row["due_at"])

        announced = []
        for target in targets:
            # 마감을 옮기면 아직 나가지 않은 공지도 같은 간격으로 따라 옮긴다.
            # 그대로 두면 공지 시각이 마감 뒤로 넘어가 그 회차 공지가 조용히 빠진다.
            announce_at = target["announce_at"]
            if target["announced_at"] is not None:
                announced.append(target["name"])
            elif announce_at is not None:
                announce_at = announce_at + delta
            connection.execute(
                """
                UPDATE sessions
                   SET due_at = ?, announce_at = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE name = ?
                """,
                (
                    (target["due_at"] + delta).isoformat(),
                    announce_at.isoformat() if announce_at else None,
                    target["name"],
                ),
            )
        # 미룬 만큼 비는 주를 `추가 회차`로 채운다. 빈자리를 표에서 이유 없이
        # 비워 두는 대신 회차로 세워 두고, 관리자가 이름을 바꾸거나 지운다.
        filled = []
        for step in range(weeks):
            due = from_due + datetime.timedelta(weeks=step)
            new_name = _filler_name(connection, name)
            # 쉬어가는 주로 세운다. 연휴라 미룬 주이므로 그대로 두면 그 주는
            # 회고 없이 지나가고, 보충 회차로 쓸 때만 관리자가 토글을 끈다.
            # 공지 시각은 기본값으로 채워 둔다. 쉬어가는 동안은 발송 대상에서
            # 빠지므로 관리자 모르게 나가지 않고, 회차로 쓰기로 하면 다른
            # 회차처럼 공지가 나간다.
            connection.execute(
                "INSERT INTO sessions (name, due_at, announce_at, resting)"
                " VALUES (?, ?, ?, 1)",
                (new_name, due.isoformat(), default_announce_at(due).isoformat()),
            )
            filled.append(new_name)
    invalidate()
    return {
        "names": [target["name"] for target in targets],
        "announced": announced,
        "filled": filled,
    }


FILLER_SUFFIX = "추가 회차"


def _filler_name(connection, after: str) -> str:
    """비는 주를 채울 회차 이름을 겹치지 않게 짓는다."""
    from utils import cohort_of

    taken = {row["name"] for row in connection.execute("SELECT name FROM sessions")}
    base = f"{cohort_of(after)} {FILLER_SUFFIX}"
    if base not in taken:
        return base
    for number in range(2, 100):
        candidate = f"{base} {number}"
        if candidate not in taken:
            return candidate
    raise ScheduleError("추가 회차 이름을 지을 자리가 없습니다.")


def _references(connection, name: str) -> str:
    """회차 이름을 값으로 참조하는 행을 표별로 센다.

    회차 이름은 회고·출석·진행 중 회고처럼 여러 표에 그대로 박히는 조인 키다.
    표가 늘어날 때마다 목록을 고쳐야 하는 일이 없도록, `session_name` 칸을
    가진 표를 직접 찾아 센다. 표 이름은 sqlite_master에서 온 값이라 질의에
    끼워 넣어도 외부 입력이 섞이지 않는다.
    """
    found = []
    tables = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'sessions'"
        )
    ]
    for table in tables:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "session_name" not in columns:
            continue
        number = connection.execute(
            f"SELECT COUNT(*) AS number FROM {table} WHERE session_name = ?", (name,)
        ).fetchone()["number"]
        if number:
            found.append(f"{table} {number}건")
    return ", ".join(found)


def _changeable(connection, name: str, now: datetime.datetime) -> None:
    """이름을 바꾸거나 지울 수 있는 회차인지 확인한다.

    회차 이름은 회고 행에 박히는 값이라, 기록이 하나라도 붙은 뒤에 바꾸면 그
    기록이 어느 회차 것인지 알 수 없게 된다. 아직 아무것도 붙지 않은 예정
    회차만 손댈 수 있다.
    """
    row = connection.execute(
        "SELECT due_at, announced_at FROM sessions WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ScheduleError(f"`{name}` 회차를 찾을 수 없습니다.")
    if datetime.datetime.fromisoformat(row["due_at"]) <= now:
        raise ScheduleError(f"이미 마감된 `{name}`은(는) 바꿀 수 없습니다.")
    if row["announced_at"] is not None:
        raise ScheduleError(f"`{name}`은(는) 제출 공지가 이미 나가서 바꿀 수 없습니다.")
    used = _references(connection, name)
    if used:
        raise ScheduleError(f"`{name}`에 이미 쌓인 기록({used})이 있어 바꿀 수 없습니다.")


def rename_session(name: str, new_name: str, *, now: datetime.datetime) -> str:
    """아직 아무 기록도 붙지 않은 예정 회차의 이름을 바꾼다."""
    new_name = _validate_name(new_name)
    with get_connection() as connection:
        _changeable(connection, name, now)
        if new_name != name:
            taken = connection.execute(
                "SELECT 1 FROM sessions WHERE name = ?", (new_name,)
            ).fetchone()
            if taken:
                raise ScheduleError(f"`{new_name}`은(는) 이미 있는 회차입니다.")
        connection.execute(
            "UPDATE sessions SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (new_name, name),
        )
    invalidate()
    return new_name


def set_resting(name: str, resting: bool, *, now: datetime.datetime) -> None:
    """그 주를 쉬어갈지 회차로 쓸지 켜고 끈다.

    쉬어가는 주로 두면 회차 판정과 공지에서 빠져 그 주에는 마감이 없다. 회차를
    지웠다 다시 만들지 않으므로 마음이 바뀌면 그대로 되돌릴 수 있다.
    """
    with get_connection() as connection:
        _changeable(connection, name, now)
        if resting:
            remaining = connection.execute(
                "SELECT COUNT(*) AS number FROM sessions WHERE resting = 0"
            ).fetchone()["number"]
            # 남는 회차가 없으면 일정 정본이 조용히 코드 상수로 돌아간다.
            if remaining <= 1:
                raise ScheduleError("마지막 남은 회차는 쉬어갈 수 없습니다.")
        connection.execute(
            "UPDATE sessions SET resting = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE name = ?",
            (1 if resting else 0, name),
        )
    invalidate()

def withdraw_rest_week(name: str, *, now: datetime.datetime) -> list[str]:
    """쉬어가는 주를 일정에서 빼고 뒤 회차를 한 주씩 당긴다. 미루기를 되돌린다.

    미루기는 뒤 회차를 N주 밀고 비는 자리에 쉬어가는 주를 세운다. 그 주를 하나
    뺄 때마다 뒤를 한 주씩 당기면 미루기 전 일정으로 돌아간다. 쉬어가는 주만
    뺄 수 있다. 회차로 쓰고 있는 주를 빼면 멀쩡한 회차 하나가 사라진다.

    공지가 이미 나간 뒤 회차가 있으면 막는다. 공지에 적힌 마감보다 실제 마감이
    앞당겨지면 그걸 믿고 기다리던 사람이 제출을 놓친다. 돌려주는 값은 당긴 회차다.
    """
    week = datetime.timedelta(weeks=1)
    with get_connection() as connection:
        _changeable(connection, name, now)
        found = [
            _session(row)
            for row in connection.execute(
                "SELECT name, due_at, announce_at, announcement, announced_at, resting"
                "  FROM sessions"
            ).fetchall()
        ]
        found.sort(key=lambda row: row["due_at"])
        target = next(row for row in found if row["name"] == name)
        if not target["resting"]:
            raise ScheduleError(
                f"`{name}`은(는) 회차로 쓰는 중입니다. 먼저 쉬어가기로 바꾼 뒤 빼세요."
            )

        before = [row for row in found if row["due_at"] < target["due_at"]]
        later = [row for row in found if row["due_at"] > target["due_at"]]
        sent = [row["name"] for row in later if row["announced_at"] is not None]
        if sent:
            raise ScheduleError(
                f"`{sent[0]}` 공지가 이미 나가 마감을 앞당길 수 없습니다."
            )
        if later:
            first = later[0]["due_at"] - week
            if first <= now:
                raise ScheduleError(
                    f"당기면 `{later[0]['name']}` 마감이 이미 지난 시각이 됩니다."
                )
            # 당긴 첫 회차가 앞 회차와 겹치거나 앞서면 회차 순서가 뒤집힌다.
            if before and first <= before[-1]["due_at"]:
                raise ScheduleError(
                    f"당기면 `{later[0]['name']}` 마감이 `{before[-1]['name']}`보다 앞서게 됩니다."
                )

        connection.execute("DELETE FROM sessions WHERE name = ?", (name,))
        for row in later:
            announce_at = row["announce_at"]
            if announce_at is not None:
                announce_at = announce_at - week
            connection.execute(
                """
                UPDATE sessions
                   SET due_at = ?, announce_at = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE name = ?
                """,
                (
                    (row["due_at"] - week).isoformat(),
                    announce_at.isoformat() if announce_at else None,
                    row["name"],
                ),
            )
    invalidate()
    return [row["name"] for row in later]


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
        "resting": bool(row["resting"]) if "resting" in row.keys() else False,
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
            "SELECT name, due_at, announce_at, announcement, announced_at, resting"
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
            "SELECT name, due_at, announce_at, announcement, announced_at, resting"
            "  FROM sessions"
            " WHERE announce_at IS NOT NULL AND announced_at IS NULL AND resting = 0"
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


def mark_announced(
    name: str, sent_at: datetime.datetime, *,
    channel_id: str | None = None, message_ts: str | None = None, body: str = "",
) -> bool:
    """공지 발송을 기록한다. 이미 기록돼 있으면 False."""
    with get_connection() as connection:
        changed = connection.execute(
            "UPDATE sessions SET announced_at = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE name = ? AND announced_at IS NULL",
            (sent_at.isoformat(), name),
        ).rowcount
        if changed and channel_id and message_ts:
            connection.execute(
                "INSERT INTO submission_announcement_messages"
                " (session_name, channel_id, message_ts, body) VALUES (?, ?, ?, ?)",
                (name, channel_id, message_ts, body),
            )
    return changed > 0


def announcements_to_close(now: datetime.datetime) -> list[dict]:
    """Persisted messages whose current session deadline has passed."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT m.*, s.due_at FROM submission_announcement_messages m"
            " JOIN sessions s ON s.name = m.session_name WHERE m.closed_at IS NULL"
        ).fetchall()
    return [dict(row) for row in rows
            if datetime.datetime.fromisoformat(row["due_at"]) <= now]


def mark_announcement_closed(session_name: str, now: datetime.datetime) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE submission_announcement_messages SET closed_at = ?"
            " WHERE session_name = ? AND closed_at IS NULL",
            (now.isoformat(), session_name),
        )
