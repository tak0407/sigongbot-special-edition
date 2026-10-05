"""팀별 온라인 회고 시간 투표 발송, 결과 확인, 시간 확정.

투표·확정 버튼은 기존 관리자 세션 인증과 CSRF 보호를 그대로 쓴다. 이미 만들어진
모임은 새 이벤트를 만들지 않고 기존 Calendar 이벤트를 갱신한다.
"""

import datetime
from html import escape
from urllib.parse import quote

from aiohttp import web
from loguru import logger
from slack_sdk.errors import SlackApiError

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
from slack.events.online_retro_poll import TIME_SLOTS, post_team_time_poll
from utils import get_current_session_info, tz_now

PAGE_PATH = "/online-retro"
TEAM_NAME_MAX = 20
POLL_WINDOW = f"{TIME_SLOTS[0].split('~')[0]}~{TIME_SLOTS[-1].split('~')[1]}"
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
# 회차 일정 화면과 같은 방식으로 버튼이 가리키는 모달을 연다.
DIALOG_SCRIPT = """<script>
(() => {
 document.addEventListener('click', (event) => {
  const opener = event.target.closest('[data-dialog]');
  if (opener) {
   document.getElementById(opener.dataset.dialog).showModal();
   return;
  }
  if (event.target.tagName === 'DIALOG') event.target.close();
 });
})();
</script>"""


class PollError(RuntimeError):
    """운영자에게 그대로 보여 줄 수 있는 투표 발송 실패 사유."""


def second_sunday_after(day: datetime.date) -> datetime.date:
    """day 다음에 오는 둘째 일요일. 온라인 회고는 매월 둘째 일요일에 모인다."""
    year, month = day.year, day.month
    while True:
        first = datetime.date(year, month, 1)
        candidate = first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7)
        if candidate > day:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def session_for_meeting(meeting_date: str) -> str:
    """투표 시간대 전체가 들어가는 열린 회차.

    모임 중 `회고 작성하기` 버튼은 투표에 적힌 회차로 제출 창을 열고, 공유 순서의
    작성 완료 표시도 그 회차의 제출 기록을 본다. 그래서 그날 실제로 열려 있는
    회차 이름을 넣어야 한다.
    """
    starts_at, _ = slot_to_range(meeting_date, TIME_SLOTS[0])
    _, ends_at = slot_to_range(meeting_date, TIME_SLOTS[-1])
    _, first, _, first_open = get_current_session_info(starts_at)
    _, last, _, last_open = get_current_session_info(
        ends_at - datetime.timedelta(minutes=1)
    )
    if not (first and first == last and first_open and last_open):
        raise PollError(
            f"{meeting_date} {POLL_WINDOW} 동안 열려 있는 회차가 없어요. "
            "회차 일정을 확인해 주세요."
        )
    return first


def _team_channels() -> list[str]:
    return sorted(set(settings.SUBMISSION_DESTINATIONS.values()))


def _posted_channels(polls: list[dict], meeting_date: str) -> set[str]:
    return {
        poll["team_channel"]
        for poll in polls
        if poll["meeting_date"] == meeting_date and not poll["is_test"] and poll["slack_ts"]
    }


def _latest_team_names(polls: list[dict]) -> dict[str, str]:
    """채널마다 가장 최근 투표의 팀 이름. 다음 달에는 다시 입력하지 않아도 된다."""
    names: dict[str, str] = {}
    for poll in polls:  # list_polls()는 최근 날짜부터 돌려준다.
        if not poll["is_test"]:
            names.setdefault(poll["team_channel"], poll["team_name"])
    return names


async def _collect() -> list[dict]:
    polls = await list_polls()
    for poll in polls:
        poll["confirmation"] = await get_confirmation(poll["id"])
    return polls


def _poll_dialog(polls: list[dict], directory: dict, csrf: str) -> str:
    channels = _team_channels()
    if not channels:
        return (
            '<div class="warn"><code>SUBMISSION_TEAMS</code>에 팀 채널이 없어 '
            "시간 투표를 올릴 수 없습니다.</div>"
        )
    today = tz_now().date()
    meeting_date = second_sunday_after(today).isoformat()
    try:
        session_note = (
            "모임에서 쓰는 회고는 그날 열린 회차"
            f"(<b>{escape(session_for_meeting(meeting_date))}</b>)로 제출됩니다."
        )
    except PollError as error:
        session_note = f"<b>{escape(str(error))}</b>"
    names = _latest_team_names(polls)
    posted = _posted_channels(polls, meeting_date)
    teams = "".join(
        '<div class="filters">'
        f"<span>{channel_cell(directory, channel)}"
        f"{' · 이미 올림' if channel in posted else ''}</span>"
        f'<input aria-label="팀 이름" type="text" name="team_name_{escape(channel)}" '
        f'value="{escape(names.get(channel, ""))}" placeholder="팀 이름" '
        f'maxlength="{TEAM_NAME_MAX}" required></div>'
        for channel in channels
    )
    return f"""<div class="toolbar">
<button type="button" class="primary" data-dialog="poll-dialog">투표 올리기</button>
</div>
<dialog id="poll-dialog" aria-labelledby="poll-title">
<div class="dialog-head"><h2 id="poll-title">시간 투표 올리기</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<small>팀 채널마다 {POLL_WINDOW} 사이 1시간 단위 시간 투표를 올립니다. 팀원은 자기 팀 투표에만 참여하고, 결과는 이 화면에서 팀별로 확정합니다.
날짜는 다음 둘째 일요일이 기본입니다. {session_note}
같은 날짜로 다시 눌러도 이미 올린 팀에는 다시 올리지 않습니다.</small>
<form method="post" action="{PAGE_PATH}/polls">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<div class="filters"><input aria-label="모임 날짜" type="date" name="meeting_date" value="{meeting_date}" min="{today.isoformat()}" required></div>
{teams}
<div class="filters"><button type="submit" class="primary">팀 채널에 투표 올리기</button></div>
</form>
</dialog>"""


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
    if summary := request.query.get("polls", "").strip():
        return f'<div class="warn">{escape(summary[:300])}</div>'
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
    directory = await get_directory(
        request, {poll["team_channel"] for poll in polls} | set(_team_channels())
    )
    csrf = csrf_token(request)
    sections = "".join(_poll_section(poll, directory, csrf) for poll in polls)
    if not sections:
        sections = "<p>아직 생성된 시간 투표가 없습니다.</p>"
    body = (
        f"{_notice(request)}{_google_state()}{_poll_dialog(polls, directory, csrf)}"
        f"{sections}{DIALOG_SCRIPT}"
    )
    return web.Response(
        text=layout.render(
            title="온라인 회고 확정",
            active=PAGE_PATH,
            heading="온라인 회고 확정",
            subtitle="팀별 시간 투표를 올리고, 결과를 보고 모임 시간을 확정합니다. 확정하면 Calendar 이벤트와 Google Meet 링크가 만들어집니다.",
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


def _parse_meeting_date(raw: str) -> datetime.date:
    try:
        meeting_date = datetime.date.fromisoformat(raw.strip())
    except ValueError:
        raise PollError("모임 날짜 형식이 올바르지 않습니다.") from None
    if meeting_date < tz_now().date():
        raise PollError("지난 날짜로는 투표를 올릴 수 없어요.")
    return meeting_date


def _team_names(form, channels: list[str]) -> dict[str, str]:
    names = {}
    for channel in channels:
        name = str(form.get(f"team_name_{channel}", "")).strip()
        if not name:
            raise PollError("팀 이름을 모두 입력해 주세요.")
        if len(name) > TEAM_NAME_MAX:
            raise PollError(f"팀 이름은 {TEAM_NAME_MAX}자 이내로 입력해 주세요.")
        names[channel] = name
    return names


@require_admin
async def handle_post_polls(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    channels = _team_channels()
    try:
        if not channels:
            raise PollError("SUBMISSION_TEAMS에 팀 채널이 없어 투표를 올릴 곳이 없어요.")
        meeting_date = _parse_meeting_date(str(form.get("meeting_date", "")))
        session_name = session_for_meeting(meeting_date.isoformat())
        team_names = _team_names(form, channels)
    except PollError as error:
        raise _redirect(error=str(error))

    client = request.app.get(SLACK_CLIENT)
    if client is None:
        raise _redirect(error="Slack 클라이언트를 사용할 수 없습니다.")

    already = _posted_channels(await list_polls(), meeting_date.isoformat())
    posted, skipped, failed = [], [], []
    for channel in channels:
        name = team_names[channel]
        if channel in already:
            skipped.append(name)
            continue
        try:
            await post_team_time_poll(
                client,
                channel=channel,
                team_name=name,
                meeting_date=meeting_date,
                session_name=session_name,
            )
        except Exception as error:  # 한 팀이 실패해도 나머지 팀에는 올린다.
            logger.exception("온라인 회고 시간 투표를 올리지 못했습니다 - team_channel={}", channel)
            reason = (
                error.response.get("error", "unknown")
                if isinstance(error, SlackApiError)
                else type(error).__name__
            )
            failed.append(f"{name}({reason})")
        else:
            posted.append(name)
    logger.info(
        "온라인 회고 시간 투표 - date={} session={} posted={} skipped={} failed={}",
        meeting_date,
        session_name,
        len(posted),
        len(skipped),
        len(failed),
    )

    summary = f"{meeting_date.month}월 {meeting_date.day}일({session_name}) 시간 투표를 "
    summary += f"올렸습니다: {', '.join(posted)}." if posted else "새로 올린 팀은 없습니다."
    if skipped:
        summary += f" 이미 올라가 있던 팀은 건너뛰었습니다: {', '.join(skipped)}."
    if failed:
        raise _redirect(
            error=(
                f"{summary} 올리지 못한 팀: {', '.join(failed)}. "
                "봇이 그 채널에 들어가 있는지 확인한 뒤 다시 누르면 그 팀에만 올립니다."
            )
        )
    raise _redirect(polls=summary)


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
