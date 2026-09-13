"""관리자 웹 페이지들이 함께 쓰는 표시용 헬퍼."""

import datetime
from html import escape
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REFRESH_SECONDS = 60
PAGE_SIZE = 20


def to_kst(value: str | None) -> str:
    """SQLite가 UTC로 남긴 시각 문자열을 한국 시간 표기로 바꾼다."""
    if not value:
        return "-"
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        # 예상 못 한 형식이면 원본을 그대로 보여 준다.
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def to_kst_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(KST)


def since(value: str | None) -> str:
    """마지막 갱신으로부터 얼마나 지났는지 사람이 읽는 형태로 돌려준다."""
    moment = to_kst_datetime(value)
    if moment is None:
        return "-"
    delta = datetime.datetime.now(tz=KST) - moment
    if delta.days > 0:
        return f"{delta.days}일 전"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}시간 전"
    return f"{max(delta.seconds // 60, 0)}분 전"


def truncate(text: str | None, length: int, empty: str = "-") -> str:
    value = (text or "").strip()
    if not value:
        return empty
    collapsed = " ".join(value.split())
    if len(collapsed) <= length:
        return collapsed
    return collapsed[:length] + "…"


def rows(items: list[str], columns: int, empty: str) -> str:
    if items:
        return "".join(items)
    return f'<tr><td colspan="{columns}">{escape(empty)}</td></tr>'


def page_numbers(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int]:
    """(마지막 페이지 번호, 정규화된 현재 페이지)를 돌려준다."""
    last = max(1, (total + size - 1) // size)
    return last, min(max(page, 1), last)
