"""매회차 제출 공지 문구 편집.

기본 문구 하나가 모든 회차에 쓰이고, 특정 회차만 다르게 쓰고 싶을 때 그 회차
문구를 덮어쓴다. 편집은 회차 일정 화면(`/schedule`)의 회차별 모달에서 하고,
저장하면 회차 일정으로 돌아간다.
"""

import asyncio
import datetime
from html import escape
from urllib.parse import urlencode

from aiohttp import web
from loguru import logger

from config import settings
from dashboard.auth import csrf_token, require_admin
from dashboard.common import KST
from database.sessions import (
    MAX_ANNOUNCEMENT_LENGTH,
    ScheduleError,
    get_template,
    render_announcement,
    set_announcement,
    set_template,
    update_announce_at,
)
from utils import tz_now

PLACEHOLDER_HELP = (
    "<code>{회차}</code>는 회차 이름으로, <code>{마감}</code>은 마감 시각으로 "
    "발송 시점에 바뀝니다. <code>&lt;!here&gt;</code>를 지우면 채널 멘션 없이 나갑니다."
)


def _parse_time(raw: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat((raw or "").strip())
    except ValueError:
        raise ScheduleError("공지 시각 형식이 올바르지 않습니다.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _redirect(name: str, result: str = "", **query) -> web.HTTPFound:
    """회차 일정으로 돌아간다. 결과 안내는 회차 일정 화면이 그린다."""
    if result:
        query = {"announcement": name, "result": result, **query}
    return web.HTTPFound("/schedule?" + urlencode(query))


def state(session: dict, now: datetime.datetime) -> tuple[str, str]:
    """(상태 표시, 왜 못 고치는지) 를 돌려준다. 고칠 수 있으면 사유는 빈 문자열."""
    if session["announced_at"]:
        sent = session["announced_at"].astimezone(KST).strftime("%m-%d %H:%M")
        return (
            f'<span class="pill completed">발송 {sent}</span>',
            "이미 나간 공지입니다. 문구를 고쳐도 다시 보내지 않습니다.",
        )
    if session["due_at"] <= now:
        return '<span class="pill">마감</span>', "이미 마감된 회차입니다."
    # 쉬어가는 주는 공지 시각이 있어도 나가지 않는다. 회차로 쓰면 그 시각에 나간다.
    if session.get("resting"):
        return '<span class="pill">쉬어가는 동안 공지 없음</span>', ""
    if session["announce_at"] is None:
        return '<span class="pill failed">공지 꺼짐</span>', ""
    when = session["announce_at"].strftime("%m-%d %H:%M")
    if session["announce_at"] <= now:
        return f'<span class="pill pending">발송 대기 {when}</span>', ""
    return f'<span class="pill pending">{when} 예정</span>', ""


def _body(session: dict, template: str) -> tuple[str, bool]:
    """(편집창에 채울 문구, 회차 전용 문구인지)."""
    if session["announcement"] is not None:
        return session["announcement"], True
    return template, False


def announcement_dialog(
    request, session: dict, dialog_id: str, *, template: str, now: datetime.datetime
) -> str:
    """회차 하나의 제출 공지를 고치는 모달.

    이미 나갔거나 마감된 회차는 미리보기와 못 고치는 이유만 보여 준다.
    """
    name = session["name"]
    badge, blocked = state(session, now)
    body, overridden = _body(session, template)
    preview = render_announcement(body, name=name, due_at=session["due_at"])
    kind = (
        "이 회차 전용 문구 · 기본 문구를 바꿔도 이 회차는 그대로입니다."
        if overridden
        else "기본 문구 · 기본 문구를 바꾸면 함께 바뀝니다."
    )

    if blocked:
        forms = f"<p><small>{escape(blocked)}</small></p>"
    else:
        announce_value = (session["announce_at"] or session["due_at"]).strftime("%Y-%m-%dT%H:%M")
        token = csrf_token(request)
        forms = f"""
<div class="field"><small>공지 시각 · 마감보다 앞서야 하고, 이미 지난 시각으로 두면 1분 안에 바로 나갑니다.</small>
<form method="post" action="/schedule/announcement/time" class="inline">
<input type="hidden" name="csrf_token" value="{token}">
<input type="hidden" name="name" value="{escape(name)}">
<input aria-label="공지 시각 (KST)" type="datetime-local" name="announce_at" value="{announce_value}" required>
<button type="submit">시각 변경</button>
<button type="submit" name="off" value="1">공지 끄기</button></form></div>
<div class="field"><small>문구 · {PLACEHOLDER_HELP}</small>
<form method="post" action="/schedule/announcement">
<input type="hidden" name="csrf_token" value="{token}">
<input type="hidden" name="name" value="{escape(name)}">
<textarea aria-label="공지 문구" name="body" rows="8" maxlength="{MAX_ANNOUNCEMENT_LENGTH}" required>{escape(body)}</textarea>
<div class="actions"><button type="submit" class="primary">이 회차만 저장</button>
<button type="submit" name="reset" value="1">기본 문구로 되돌리기</button></div></form></div>"""

    return f"""<dialog id="{dialog_id}" data-announcement="{escape(name)}" aria-labelledby="{dialog_id}-title">
<div class="dialog-head"><h2 id="{dialog_id}-title">{escape(name)} 제출 공지</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<div class="field"><small>상태</small>{badge} <small>마감 {session['due_at'].strftime('%Y-%m-%d %H:%M')}</small></div>
<div class="field"><small>미리보기 · {escape(kind)}</small><div class="body-field"><div class="text">{escape(preview)}</div></div>
<small>공지 채널: {escape(settings.ANNOUNCEMENT_CHANNEL or '설정 안 됨')} · 본문 아래에 `회고 제출하기` 버튼이 함께 나갑니다.</small></div>
{forms}
</dialog>"""


@require_admin
async def handle(request: web.Request) -> web.StreamResponse:
    """예전 공지 편집 페이지 주소. 편집은 회차 일정의 모달로 옮겨서 그리로 보낸다."""
    raise web.HTTPFound("/schedule")


@require_admin
async def handle_save(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    reset = bool(form.get("reset"))
    try:
        await asyncio.to_thread(
            set_announcement,
            name,
            None if reset else str(form.get("body", "")),
            now=tz_now(),
        )
    except ScheduleError as error:
        raise _redirect(name, error=str(error))
    logger.info("관리자 웹에서 회차 공지 문구를 {}합니다 - name={}", "초기화" if reset else "저장", name)
    raise _redirect(name, "reset" if reset else "saved")


@require_admin
async def handle_schedule(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    off = bool(form.get("off"))
    try:
        announce_at = None if off else _parse_time(str(form.get("announce_at", "")))
        await asyncio.to_thread(update_announce_at, name, announce_at, now=tz_now())
    except ScheduleError as error:
        raise _redirect(name, error=str(error))
    logger.info("관리자 웹에서 공지 시각을 바꿉니다 - name={} announce_at={}", name, announce_at)
    if off:
        raise _redirect(name, "off")
    raise _redirect(name, "scheduled", at=announce_at.strftime("%Y-%m-%d %H:%M"))


@require_admin
async def handle_save_template(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    try:
        await asyncio.to_thread(set_template, str(form.get("body", "")))
    except ScheduleError as error:
        raise web.HTTPFound("/schedule?" + urlencode({"error": str(error)}))
    logger.info("관리자 웹에서 제출 공지 기본 문구를 바꿉니다.")
    raise web.HTTPFound("/schedule?template=1")
