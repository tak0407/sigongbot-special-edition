"""매회차 제출 공지 문구 편집.

기본 문구 하나가 모든 회차에 쓰이고, 특정 회차만 다르게 쓰고 싶을 때 그 회차
문구를 덮어쓴다. 회차 일정 화면(`/schedule`)에서 들어온다.
"""

import asyncio
import datetime
from html import escape
from urllib.parse import urlencode

from aiohttp import web
from loguru import logger

from config import settings
from dashboard import layout
from dashboard.auth import csrf_token, require_admin
from dashboard.common import KST
from database.sessions import (
    MAX_ANNOUNCEMENT_LENGTH,
    ScheduleError,
    get_session,
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


def _redirect(name: str, **query) -> web.HTTPFound:
    return web.HTTPFound(
        "/schedule/announcement?" + urlencode({"name": name, **query})
    )


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


def _body(session: dict) -> tuple[str, bool]:
    """(편집창에 채울 문구, 회차 전용 문구인지)."""
    if session["announcement"] is not None:
        return session["announcement"], True
    return get_template(), False


def _collect(name: str) -> dict | None:
    session = get_session(name)
    if session is None:
        return None
    body, overridden = _body(session)
    return {**session, "body": body, "overridden": overridden}


@require_admin
async def handle(request: web.Request) -> web.Response:
    name = request.query.get("name", "")
    data = await asyncio.to_thread(_collect, name)
    if data is None:
        raise web.HTTPFound("/schedule?" + urlencode({"error": f"`{name}` 회차를 찾을 수 없습니다."}))

    now = tz_now()
    badge, blocked = state(data, now)
    preview = render_announcement(data["body"], name=name, due_at=data["due_at"])

    notice = ""
    if request.query.get("saved"):
        notice = '<div class="warn">공지 문구를 저장했습니다.</div>'
    elif request.query.get("reset"):
        notice = '<div class="warn">기본 문구를 쓰도록 되돌렸습니다.</div>'
    elif request.query.get("scheduled"):
        notice = f'<div class="warn">공지 시각을 {escape(request.query["scheduled"])}로 바꿨습니다.</div>'
    elif request.query.get("off"):
        notice = '<div class="warn">이 회차는 공지를 보내지 않습니다.</div>'
    elif request.query.get("error"):
        notice = f'<div class="warn">{escape(request.query["error"])}</div>'

    if blocked:
        forms = f"<p><small>{escape(blocked)}</small></p>"
    else:
        announce_value = (
            data["announce_at"].strftime("%Y-%m-%dT%H:%M")
            if data["announce_at"]
            else data["due_at"].strftime("%Y-%m-%dT%H:%M")
        )
        token = csrf_token(request)
        forms = f"""
<h2>공지 시각</h2>
<small>마감보다 앞서야 합니다. 이미 지난 시각으로 두면 1분 안에 바로 나갑니다.</small>
<form method="post" action="/schedule/announcement/time" class="filters">
<input type="hidden" name="csrf_token" value="{token}">
<input type="hidden" name="name" value="{escape(name)}">
<input aria-label="공지 시각 (KST)" type="datetime-local" name="announce_at" value="{announce_value}" required>
<button type="submit">시각 변경</button>
<button type="submit" name="off" value="1">공지 끄기</button></form>

<h2>문구</h2>
<small>{PLACEHOLDER_HELP} 비워 두지 말고, {MAX_ANNOUNCEMENT_LENGTH}자 안에서 씁니다.</small>
<form method="post" action="/schedule/announcement">
<input type="hidden" name="csrf_token" value="{token}">
<input type="hidden" name="name" value="{escape(name)}">
<textarea aria-label="공지 문구" name="body" rows="12" maxlength="{MAX_ANNOUNCEMENT_LENGTH}" required>{escape(data["body"])}</textarea>
<div class="filters"><button type="submit">이 회차만 저장</button>
<button type="submit" name="reset" value="1">기본 문구로 되돌리기</button></div></form>
"""

    body = f"""
<section class="cards">
<div class="card">상태<div class="number" style="font-size:18px">{badge}</div>
<small>마감 {data['due_at'].strftime('%Y-%m-%d %H:%M')}</small></div>
<div class="card">문구<div class="number" style="font-size:18px">{'이 회차 전용' if data['overridden'] else '기본 문구'}</div>
<small>{'기본 문구를 바꿔도 이 회차는 그대로입니다.' if data['overridden'] else '기본 문구를 바꾸면 함께 바뀝니다.'}</small></div>
</section>
{notice}
<h2>미리보기</h2>
<div class="body-field"><div class="text">{escape(preview)}</div></div>
<small>공지 채널: {escape(settings.ANNOUNCEMENT_CHANNEL or '설정 안 됨')} · 본문 아래에 `회고 제출하기` 버튼이 함께 나갑니다.</small>
{forms}
<p><a href="/schedule">회차 일정으로 돌아가기</a></p>
"""
    return web.Response(
        text=layout.render(
            title=f"{name} 제출 공지",
            active="/schedule",
            heading=f"{name} 제출 공지",
            subtitle="이 회차에 나갈 공지 문구와 시각입니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


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
    raise _redirect(name, **({"reset": "1"} if reset else {"saved": "1"}))


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
        raise _redirect(name, off="1")
    raise _redirect(name, scheduled=announce_at.strftime("%Y-%m-%d %H:%M"))


@require_admin
async def handle_save_template(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    try:
        await asyncio.to_thread(set_template, str(form.get("body", "")))
    except ScheduleError as error:
        raise web.HTTPFound("/schedule?" + urlencode({"error": str(error)}))
    logger.info("관리자 웹에서 제출 공지 기본 문구를 바꿉니다.")
    raise web.HTTPFound("/schedule?template=1")
