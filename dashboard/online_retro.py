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
from dashboard.common import to_kst
from dashboard.directory import SLACK_CLIENT, channel_cell, get_directory, user_label
from database.online_retro_confirmation import get_confirmation
from database.online_retro_poll import get_poll, list_polls
from slack.events.online_retro_confirmation import (
    ConfirmationError,
    cancel_meeting,
    confirm_meeting_time,
    format_meeting_time,
    slot_to_range,
)
from slack.events.online_retro_poll import (
    ALL_OPTIONS,
    DEFAULT_POLL_INTRO,
    MAX_POLL_INTRO_LENGTH,
    TIME_SLOTS,
    UNAVAILABLE,
    UNAVAILABLE_LABEL,
    meeting_day,
    post_time_poll,
)
from utils import get_current_session_info, tz_now

PAGE_PATH = "/online-retro"
POLL_WINDOW = f"{TIME_SLOTS[0].split('~')[0]}~{TIME_SLOTS[-1].split('~')[1]}"
# 6기 팀 채널은 `blue-team-weekly-report`처럼 색 이름으로 시작한다. 여기에 없는
# 이름이면 지난 투표의 팀 이름, 그것도 없으면 채널 이름을 쓴다.
TEAM_COLORS = {
    "blue": "블루",
    "green": "그린",
    "yellow": "옐로우",
    "red": "레드",
    "orange": "오렌지",
    "purple": "퍼플",
    "pink": "핑크",
    "black": "블랙",
    "white": "화이트",
    "gray": "그레이",
    "grey": "그레이",
    "navy": "네이비",
    "mint": "민트",
    "brown": "브라운",
}
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


def team_name_from_channel(channel_name: str) -> str:
    """'blue-team-weekly-report' → '블루팀'. 색 이름으로 시작하지 않으면 ''."""
    color = TEAM_COLORS.get(channel_name.split("-", 1)[0].lower())
    return f"{color}팀" if color else ""


def _team_channels() -> list[str]:
    return sorted(set(settings.SUBMISSION_DESTINATIONS.values()))


def _team_size(team_channel: str) -> int:
    return sum(
        1 for channel in settings.SUBMISSION_DESTINATIONS.values() if channel == team_channel
    )


def _posted_channels(polls: list[dict], meeting_date: str) -> set[str]:
    return {
        poll["team_channel"]
        for poll in polls
        if poll["meeting_date"] == meeting_date and not poll["is_test"] and poll["slack_ts"]
    }


def _latest_team_names(polls: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for poll in polls:  # list_polls()는 최근 날짜부터 돌려준다.
        if not poll["is_test"]:
            names.setdefault(poll["team_channel"], poll["team_name"])
    return names


def _latest_intro(polls: list[dict]) -> str:
    """가장 나중에 만든 투표의 공지 문구. 다음 달에는 이 문구에서 고치면 된다."""
    latest = max(
        (poll for poll in polls if not poll["is_test"]),
        key=lambda poll: poll["id"],
        default=None,
    )
    return (latest or {}).get("intro_template") or DEFAULT_POLL_INTRO


def _team_labels(channels: list[str], directory: dict, polls: list[dict]) -> dict[str, str]:
    previous = _latest_team_names(polls)
    labels = {}
    for channel in channels:
        channel_name = directory["channels"].get(channel, "")
        labels[channel] = (
            team_name_from_channel(channel_name)
            or previous.get(channel)
            or (f"#{channel_name}" if channel_name else channel)
        )
    return labels


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
    if not settings.ANNOUNCEMENT_CHANNEL:
        return (
            '<div class="warn"><code>ANNOUNCEMENT_CHANNEL</code>이 비어 있어 '
            "시간 투표를 올릴 곳이 없습니다.</div>"
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
    labels = _team_labels(channels, directory, polls)
    posted = _posted_channels(polls, meeting_date)
    targets = "".join(
        f"<li><b>{escape(labels[channel])}</b> · 팀원 {_team_size(channel)}명"
        f"{' · 이미 올림' if channel in posted else ''}</li>"
        for channel in sorted(channels, key=labels.__getitem__)
    )
    return f"""<div class="toolbar">
<button type="button" class="primary" data-dialog="poll-dialog">투표 올리기</button>
</div>
<dialog id="poll-dialog" aria-labelledby="poll-title">
<div class="dialog-head"><h2 id="poll-title">시간 투표 올리기</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<small>공지 채널 {channel_cell(directory, settings.ANNOUNCEMENT_CHANNEL)}에 {POLL_WINDOW} 사이 1시간 단위 시간 투표를 하나 올립니다.
누가 고르든 그 사람 팀의 투표로 기록되고, 팀별 결과는 이 화면에서 확정합니다. 날짜는 다음 둘째 일요일이 기본입니다.
{session_note} 같은 날짜로 다시 눌러도 다시 올리지 않고, 빠진 팀만 그 공지에 더합니다.</small>
<form method="post" action="{PAGE_PATH}/polls">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<div class="filters"><input aria-label="모임 날짜" type="date" name="meeting_date" value="{meeting_date}" min="{today.isoformat()}" required></div>
<div class="field"><small>공지 문구 · <code>{{날짜}}</code>는 모임 날짜({escape(meeting_day(meeting_date))})로 바뀝니다. 비우면 기본 문구를 씁니다.</small>
<textarea aria-label="투표 공지 문구" name="intro" rows="4" maxlength="{MAX_POLL_INTRO_LENGTH}">{escape(_latest_intro(polls))}</textarea></div>
<div class="field"><small>팀 · 팀 이름은 채널 이름에서 정합니다.</small><ul class="retro-targets">{targets}</ul></div>
<div class="filters"><button type="submit" class="primary">공지 채널에 투표 올리기</button></div>
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


def _top_count(poll: dict) -> int:
    return max(poll["counts"].get(slot, 0) for slot in TIME_SLOTS)


def _votes(poll: dict, directory: dict) -> str:
    """시간대별 득표를 한 줄씩. 가장 많이 고른 시간은 강조하고, 시간대에 마우스를
    올리면 그 시간을 고른 사람이 보인다."""
    scale = max(poll["counts"].values(), default=0)
    top = _top_count(poll)
    cells = []
    for slot in ALL_OPTIONS:
        count = poll["counts"].get(slot, 0)
        css = ' class="top"' if slot != UNAVAILABLE and top and count == top else ""
        names = sorted(
            user_label(directory, user_id)
            for user_id, chosen in poll["votes"].items()
            if slot in chosen
        )
        title = f' title="{escape(", ".join(names))}"' if names else ""
        width = round(100 * count / scale) if scale else 0
        cells.append(
            f"<span{css}{title}>{escape(UNAVAILABLE_LABEL if slot == UNAVAILABLE else slot)}</span>"
            f'<span class="bar"><span style="width:{width}%"></span></span>'
            f"<span{css}>{count}명</span>"
        )
    return f'<div class="votes">{"".join(cells)}</div>'


def _short_slot(slot: str) -> str:
    """'19:00~20:00' → '19시'."""
    return UNAVAILABLE_LABEL if slot == UNAVAILABLE else f"{int(slot[:2])}시"


def _voters(poll: dict, directory: dict) -> str:
    """사람별로 고른 시간과 아직 응답하지 않은 팀원. 관리자 화면에만 보인다."""
    order = {slot: index for index, slot in enumerate(ALL_OPTIONS)}
    answered = sorted(
        (
            user_label(directory, user_id),
            ", ".join(
                _short_slot(slot)
                for slot in sorted(chosen, key=lambda slot: order.get(slot, len(order)))
            ),
        )
        for user_id, chosen in poll["votes"].items()
    )
    waiting = sorted(
        user_label(directory, user_id)
        for user_id, channel in settings.SUBMISSION_DESTINATIONS.items()
        if channel == poll["team_channel"] and user_id not in poll["votes"]
    )
    if not answered and not waiting:
        return ""
    summary = f"응답 {len(answered)}명" + (f" · 미응답 {len(waiting)}명" if waiting else "")
    items = "".join(f"<li><b>{escape(name)}</b> {escape(chosen)}</li>" for name, chosen in answered)
    missing = f'<div class="meta">미응답: {escape(", ".join(waiting))}</div>' if waiting else ""
    return (
        f'<details class="voters"><summary>{summary}</summary>'
        f"{f'<ul>{items}</ul>' if items else ''}{missing}</details>"
    )


def _confirm_controls(poll: dict, confirmation: dict | None, csrf: str) -> str:
    # 확정 전에는 가장 많이 고른 시간을 미리 골라 둔다. 동률이면 이른 시간.
    top = _top_count(poll)
    best = next((slot for slot in TIME_SLOTS if top and poll["counts"].get(slot) == top), "")
    selected = (confirmation or {}).get("slot") or best
    options = "".join(
        f'<option value="{escape(slot)}"{" selected" if slot == selected else ""}>'
        f"{escape(slot)}</option>"
        for slot in TIME_SLOTS
    )
    confirmed = bool(confirmation and confirmation["status"] == "confirmed")
    label = "시간 변경 및 Meet 갱신" if confirmed else "시간 확정 및 Meet 생성"
    cancel = ""
    if confirmed:
        cancel = (
            f'<form class="inline" method="post" action="{PAGE_PATH}/{poll["id"]}/cancel">'
            f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">'
            '<button type="submit" class="danger">확정 취소</button></form>'
        )
    return (
        f'<form method="post" action="{PAGE_PATH}/{poll["id"]}/confirm">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">'
        f'<select name="slot" aria-label="확정할 시간">{options}</select>'
        f'<button type="submit">{escape(label)}</button>'
        "</form>" + cancel
    )


def _confirmation_detail(confirmation: dict | None) -> str:
    if confirmation is None:
        return ""
    starts_at = datetime.datetime.fromisoformat(confirmation["starts_at"])
    ends_at = datetime.datetime.fromisoformat(confirmation["ends_at"])
    meet_url = confirmation["meet_url"] or ""
    link = (
        f' · <a href="{escape(meet_url)}" rel="noreferrer noopener" target="_blank">Meet 열기</a>'
        if meet_url
        else ""
    )
    access = confirmation["meet_access_type"] or "기본값 유지"
    error = (
        f'<div class="error">{escape(confirmation["last_error"])}</div>'
        if confirmation["last_error"]
        else ""
    )
    return (
        f'<div class="meta"><b>{escape(format_meeting_time(starts_at, ends_at))}</b>{link}<br>'
        f"입장 정책 {escape(str(access))} · 갱신 {escape(to_kst(confirmation['updated_at']))} KST<br>"
        f"이벤트 <code>{escape(confirmation['calendar_event_id'])}</code></div>{error}"
    )


def _confirmable(poll: dict) -> bool:
    """실제 팀 채널을 가리키는 테스트 투표는 확정하지 않는다.

    확정하면 그 팀 채널에 공지가 나가고 팀원 Gmail로 Calendar 초대가 간다.
    """
    return not poll["is_test"] or poll["team_channel"] not in _team_channels()


def _team_card(poll: dict, directory: dict, csrf: str) -> str:
    confirmation = poll["confirmation"]
    size = _team_size(poll["team_channel"])
    responded = (
        f"팀원 {size}명 중 {poll['voters']}명 응답" if size else f"{poll['voters']}명 응답"
    )
    controls = (
        _confirm_controls(poll, confirmation, csrf)
        if _confirmable(poll)
        else '<div class="meta">실제 팀을 가리키는 테스트 투표라 확정하지 않습니다.</div>'
    )
    return f"""<article class="retro-team">
<h3>{escape(poll['team_name'])} {_status_pill(confirmation)}</h3>
<div class="meta">{channel_cell(directory, poll['team_channel'])} · {responded}</div>
{_votes(poll, directory)}
{_voters(poll, directory)}
{controls}
{_confirmation_detail(confirmation)}
</article>"""


def _date_block(polls: list[dict], directory: dict, csrf: str) -> str:
    """모임 하루치. 팀 카드를 나란히 놓아 한눈에 비교한다."""
    first = polls[0]
    confirmed = sum(
        1
        for poll in polls
        if poll["confirmation"] and poll["confirmation"]["status"] == "confirmed"
    )
    test_mark = " 🧪 테스트" if first["is_test"] else ""
    cards = "".join(_team_card(poll, directory, csrf) for poll in polls)
    return (
        f'<h2 class="retro-date">{escape(meeting_day(first["meeting_date"]))} · '
        f'{escape(first["session_name"])}{test_mark}'
        f"<small>확정 {confirmed}/{len(polls)}팀</small></h2>"
        f'<div class="retro-teams">{cards}</div>'
    )


def _poll_sections(polls: list[dict], directory: dict, csrf: str) -> str:
    """다가오는 모임은 펼치고, 지난 모임과 테스트 투표는 접어 둔다."""
    if not polls:
        return "<p>아직 생성된 시간 투표가 없습니다.</p>"
    today = tz_now().date().isoformat()
    groups: dict[tuple[str, bool], list[dict]] = {}
    for poll in polls:
        groups.setdefault((poll["meeting_date"], bool(poll["is_test"])), []).append(poll)
    upcoming = sorted(key for key in groups if not key[1] and key[0] >= today)
    past = sorted((key for key in groups if not key[1] and key[0] < today), reverse=True)
    tests = sorted((key for key in groups if key[1]), reverse=True)

    html = "".join(
        f"<section>{_date_block(groups[key], directory, csrf)}</section>" for key in upcoming
    )
    if not upcoming:
        html = "<p>다가오는 모임의 시간 투표가 없습니다. <b>투표 올리기</b>로 팀 채널에 투표를 올리세요.</p>"
    for title, keys in (("지난 투표", past), ("테스트 투표", tests)):
        if keys:
            count = sum(len(groups[key]) for key in keys)
            inner = "".join(_date_block(groups[key], directory, csrf) for key in keys)
            html += f'<details class="section"><summary>{title} {count}건</summary>{inner}</details>'
    return html


def _notice(request: web.Request) -> str:
    if message := NOTICES.get(request.query.get("done", "")):
        # 확정은 됐어도 참석자 초대나 입장 정책에 경고가 남을 수 있다.
        warning = request.query.get("warning", "").strip()
        detail = f"<div>{escape(warning[:300])}</div>" if warning else ""
        return f'<div class="warn">{escape(message)}{detail}</div>'
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
    return ""


def _invite_note() -> str:
    if settings.GOOGLE_MEET_ACCESS_TYPE:
        return ""
    return (
        "<p><small>시간을 확정하면 팀원 Slack 프로필의 Gmail 주소를 Calendar 참석자로 "
        "자동 초대합니다. 초대받은 팀원은 운영자가 없어도 노크 없이 Meet에 들어옵니다. "
        "Gmail이 아닌 Google 계정은 <code>ONLINE_RETRO_TEAM_ATTENDEES</code>에 추가하세요.</small></p>"
    )


@require_admin
async def handle(request: web.Request) -> web.Response:
    polls = await _collect()
    directory = await get_directory(
        request,
        {poll["team_channel"] for poll in polls}
        | set(_team_channels())
        | {settings.ANNOUNCEMENT_CHANNEL},
    )
    csrf = csrf_token(request)
    body = (
        f"{_notice(request)}{_google_state()}{_poll_dialog(polls, directory, csrf)}"
        f"{_poll_sections(polls, directory, csrf)}{_invite_note()}{DIALOG_SCRIPT}"
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


def _parse_intro(raw: str) -> str | None:
    """비우거나 기본 문구 그대로면 None으로 남겨 기본 문구를 따라가게 한다."""
    intro = raw.replace("\r\n", "\n").strip()
    if len(intro) > MAX_POLL_INTRO_LENGTH:
        raise PollError(f"공지 문구는 {MAX_POLL_INTRO_LENGTH}자까지 쓸 수 있어요.")
    return None if not intro or intro == DEFAULT_POLL_INTRO else intro


@require_admin
async def handle_post_polls(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    channels = _team_channels()
    try:
        if not channels:
            raise PollError("SUBMISSION_TEAMS에 팀 채널이 없어 투표를 올릴 곳이 없어요.")
        if not settings.ANNOUNCEMENT_CHANNEL:
            raise PollError("ANNOUNCEMENT_CHANNEL이 비어 있어 투표를 올릴 곳이 없어요.")
        meeting_date = _parse_meeting_date(str(form.get("meeting_date", "")))
        session_name = session_for_meeting(meeting_date.isoformat())
        intro = _parse_intro(str(form.get("intro", "")))
    except PollError as error:
        raise _redirect(error=str(error))

    client = request.app.get(SLACK_CLIENT)
    if client is None:
        raise _redirect(error="Slack 클라이언트를 사용할 수 없습니다.")

    labels = _team_labels(
        channels, await get_directory(request, set(channels)), await list_polls()
    )
    teams = sorted(((channel, labels[channel]) for channel in channels), key=lambda team: team[1])
    day = f"{meeting_date.month}월 {meeting_date.day}일({session_name})"
    try:
        result = await post_time_poll(
            client,
            teams=teams,
            meeting_date=meeting_date,
            session_name=session_name,
            message_channel=settings.ANNOUNCEMENT_CHANNEL,
            intro_template=intro,
        )
    except Exception as error:  # 투표 행은 남으니 다시 누르면 그대로 올린다.
        logger.exception("온라인 회고 시간 투표를 올리지 못했습니다 - date={}", meeting_date)
        reason = (
            error.response.get("error", "unknown")
            if isinstance(error, SlackApiError)
            else type(error).__name__
        )
        raise _redirect(
            error=(
                f"{day} 시간 투표를 공지 채널에 올리지 못했어요({reason}). "
                "봇이 공지 채널에 들어가 있는지 확인한 뒤 다시 눌러 주세요."
            )
        )
    logger.info(
        "온라인 회고 시간 투표 - date={} session={} added={} new_message={}",
        meeting_date,
        session_name,
        len(result.added),
        result.new_message,
    )
    if result.new_message:
        summary = f"{day} 시간 투표를 공지 채널에 올렸습니다: {', '.join(result.added)}."
    elif result.added:
        summary = f"{day} 공지에 빠진 팀을 더했습니다: {', '.join(result.added)}."
    else:
        summary = f"{day} 시간 투표는 이미 공지 채널에 올라가 있어요."
    raise _redirect(polls=summary)


@require_admin
async def handle_confirm(request: web.Request) -> web.StreamResponse:
    poll = await _load_poll(request)
    if not _confirmable(poll):
        raise _redirect(error="실제 팀을 가리키는 테스트 투표는 확정할 수 없어요.")
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
