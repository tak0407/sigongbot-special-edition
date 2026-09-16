"""팀별 온라인 회고 시간 투표 결과 확인과 시간 확정.

확정 버튼은 기존 관리자 세션 인증과 CSRF 보호를 그대로 쓴다. 이미 만들어진
모임은 새 이벤트를 만들지 않고 기존 Calendar 이벤트를 갱신한다.
"""

import datetime
from html import escape
from urllib.parse import quote

from aiohttp import web
from loguru import logger

from config import settings
from dashboard import layout
from dashboard.auth import csrf_token, require_admin
from dashboard.common import rows, to_kst
from dashboard.directory import SLACK_CLIENT, channel_cell, get_directory
from database.online_retro_confirmation import get_confirmation
from database.online_retro_poll import get_poll, list_polls
from slack.events.online_retro_confirmation import (
    ConfirmationError,
    cancel_meeting,
    confirm_meeting_time,
    format_meeting_time,
    slot_to_range,
)
from slack.events.online_retro_poll import TIME_SLOTS

PAGE_PATH = "/online-retro"
STATUS_LABELS = {
    "pending": "생성 중",
    "confirmed": "확정",
    "failed": "실패",
    "cancelled": "취소",
}
NOTICES = {
    "confirmed": "시간을 확정하고 Google Meet 링크를 만들었습니다.",
    "updated": "확정 시간을 바꾸고 기존 Calendar 이벤트를 갱신했습니다.",
    "cancelled": "확정을 취소하고 Calendar 이벤트도 취소했습니다.",
}


async def _collect() -> list[dict]:
    polls = await list_polls()
    for poll in polls:
        poll["confirmation"] = await get_confirmation(poll["id"])
    return polls


def _status_pill(confirmation: dict | None) -> str:
    if confirmation is None:
        return '<span class="pill">미확정</span>'
    status = str(confirmation["status"])
    css = {"confirmed": "completed", "failed": "failed", "pending": "processing"}.get(
        status, ""
    )
    return f'<span class="pill {css}">{escape(STATUS_LABELS.get(status, status))}</span>'


def _vote_table(poll: dict) -> str:
    top = max(poll["counts"].values(), default=0)
    cells = []
    for slot in poll["slots"]:
        count = poll["counts"].get(slot, 0)
        width = int(160 * count / top) if top else 0
        best = ' class="done"' if count and count == top else ""
        cells.append(
            "<tr>"
            f"<td{best}>{escape(slot)}</td>"
            f"<td{best}>{count}명</td>"
            f'<td class="bar"><span style="width:{width}px"></span></td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>시간</th><th>득표</th><th></th></tr></thead>"
        f"<tbody>{rows(cells, 3, '아직 투표가 없습니다.')}</tbody></table>"
    )


def _confirmation_table(confirmation: dict) -> str:
    starts_at = datetime.datetime.fromisoformat(confirmation["starts_at"])
    ends_at = datetime.datetime.fromisoformat(confirmation["ends_at"])
    meet_url = confirmation["meet_url"] or ""
    meet_cell = (
        f'<a href="{escape(meet_url)}" rel="noreferrer noopener" target="_blank">'
        f"{escape(meet_url)}</a>"
        if meet_url
        else "-"
    )
    access = confirmation["meet_access_type"] or "기본값 유지"
    error = ""
    if confirmation["last_error"]:
        error = (
            '<tr><th>마지막 오류</th>'
            f'<td class="error">{escape(confirmation["last_error"])}</td></tr>'
        )
    return f"""<table><tbody>
<tr><th>확정 시간</th><td>{escape(format_meeting_time(starts_at, ends_at))}</td></tr>
<tr><th>Meet 링크</th><td>{meet_cell}</td></tr>
<tr><th>Calendar 이벤트</th><td><code>{escape(confirmation['calendar_event_id'])}</code></td></tr>
<tr><th>입장 정책</th><td>{escape(str(access))}</td></tr>
<tr><th>갱신</th><td>{escape(to_kst(confirmation['updated_at']))} (KST)</td></tr>
{error}
</tbody></table>"""


def _confirm_form(poll: dict, confirmation: dict | None, csrf: str) -> str:
    selected = confirmation["slot"] if confirmation else ""
    options = "".join(
        f'<option value="{escape(slot)}"{" selected" if slot == selected else ""}>'
        f"{escape(slot)}</option>"
        for slot in TIME_SLOTS
    )
    label = "시간 변경 및 Meet 갱신" if confirmation else "시간 확정 및 Meet 생성"
    cancel = ""
    if confirmation and confirmation["status"] == "confirmed":
        cancel = (
            f'<form class="filters" method="post" action="{PAGE_PATH}/{poll["id"]}/cancel">'
            f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">'
            '<button type="submit">확정 취소</button></form>'
        )
    return (
        f'<form class="filters" method="post" action="{PAGE_PATH}/{poll["id"]}/confirm">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">'
        f'<select name="slot" aria-label="확정할 시간">{options}</select>'
        f'<button type="submit">{escape(label)}</button>'
        "</form>" + cancel
    )


def _poll_section(poll: dict, directory: dict, csrf: str) -> str:
    confirmation = poll["confirmation"]
    test_mark = " 🧪 테스트" if poll["is_test"] else ""
    detail = _confirmation_table(confirmation) if confirmation else ""
    return f"""<section>
<h2>{escape(poll['team_name'])}{test_mark} · {escape(poll['meeting_date'])} {_status_pill(confirmation)}</h2>
<p><small>{channel_cell(directory, poll['team_channel'])} · {escape(poll['session_name'])}
· 투표 {poll['voters']}명</small></p>
{_vote_table(poll)}
{_confirm_form(poll, confirmation, csrf)}
{detail}
</section>"""


def _notice(request: web.Request) -> str:
    if message := NOTICES.get(request.query.get("done", "")):
        return f'<div class="warn">{escape(message)}</div>'
    if warning := request.query.get("warning", "").strip():
        return f'<div class="warn">{escape(warning[:300])}</div>'
    if error := request.query.get("error", "").strip():
        return f'<div class="warn"><b>실패</b><div class="error">{escape(error[:300])}</div></div>'
    return ""


def _google_state() -> str:
    if settings.google_credentials() is None:
        return (
            '<div class="warn">Google 인증정보가 없어 Meet 링크를 만들 수 없습니다. '
            "운영 <code>.env</code>의 <code>GOOGLE_OAUTH_*</code> 값을 먼저 설정하세요.</div>"
        )
    if not settings.GOOGLE_MEET_ACCESS_TYPE:
        return (
            '<div class="warn"><code>GOOGLE_MEET_ACCESS_TYPE</code>이 비어 있어 Meet 입장 정책을 '
            "바꾸지 않습니다. 운영자가 없어도 팀원이 입장하게 하려면 <code>OPEN</code>으로 두거나, "
            "<code>ONLINE_RETRO_TEAM_ATTENDEES</code>로 팀원을 Calendar 참석자로 초대하세요.</div>"
        )
    return ""


@require_admin
async def handle(request: web.Request) -> web.Response:
    polls = await _collect()
    directory = await get_directory(request, {poll["team_channel"] for poll in polls})
    csrf = csrf_token(request)
    sections = "".join(_poll_section(poll, directory, csrf) for poll in polls)
    if not sections:
        sections = "<p>아직 생성된 시간 투표가 없습니다.</p>"
    body = f"{_notice(request)}{_google_state()}{sections}"
    return web.Response(
        text=layout.render(
            title="온라인 회고 확정",
            active=PAGE_PATH,
            heading="온라인 회고 확정",
            subtitle="팀별 시간 투표 결과를 보고 모임 시간을 확정합니다. 확정하면 Calendar 이벤트와 Google Meet 링크가 만들어집니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


async def _load_poll(request: web.Request) -> dict:
    try:
        poll_id = int(request.match_info["poll_id"])
    except ValueError:
        raise web.HTTPNotFound(text="투표 ID가 올바르지 않습니다.")
    poll = await get_poll(poll_id)
    if poll is None:
        raise web.HTTPNotFound(text=f"ID {poll_id} 시간 투표를 찾을 수 없습니다.")
    return poll


def _redirect(**query: str) -> web.HTTPFound:
    parts = "&".join(f"{key}={quote(value)}" for key, value in query.items() if value)
    return web.HTTPFound(f"{PAGE_PATH}?{parts}" if parts else PAGE_PATH)


@require_admin
async def handle_confirm(request: web.Request) -> web.StreamResponse:
    poll = await _load_poll(request)
    form = await request.post()
    slot = str(form.get("slot", "")).strip()
    existing = await get_confirmation(poll["id"])
    try:
        slot_to_range(poll["meeting_date"], slot)
    except (ConfirmationError, ValueError) as error:
        raise _redirect(error=str(error))

    client = request.app.get(SLACK_CLIENT)
    if client is None:
        raise _redirect(error="Slack 클라이언트를 사용할 수 없습니다.")
    try:
        _, warning = await confirm_meeting_time(client, poll=poll, slot=slot)
    except ConfirmationError as error:
        raise _redirect(error=str(error))
    except Exception as error:  # Slack 게시 실패 등은 확정 기록을 남긴 채 알린다.
        logger.exception("온라인 회고 확정 처리에 실패했습니다 - poll_id={}", poll["id"])
        raise _redirect(error=f"확정 처리 중 오류가 발생했어요: {error}")

    done = "updated" if existing and existing["status"] == "confirmed" else "confirmed"
    raise _redirect(done=done, warning=warning)


@require_admin
async def handle_cancel(request: web.Request) -> web.StreamResponse:
    poll = await _load_poll(request)
    try:
        confirmation = await cancel_meeting(poll_id=poll["id"])
    except ConfirmationError as error:
        raise _redirect(error=str(error))
    if confirmation is None:
        raise _redirect(error="취소할 확정 모임이 없습니다.")
    logger.info("온라인 회고 확정을 취소했습니다 - poll_id={}", poll["id"])
    raise _redirect(done="cancelled")
