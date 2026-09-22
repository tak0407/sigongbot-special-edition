"""회차 일정: 마감일과 남은 회차. 일정이 소진되기 전에 알아채기 위한 화면."""

import asyncio
import datetime
import re
from html import escape
from urllib.parse import urlencode

from aiohttp import web
from loguru import logger

from dashboard import layout
from dashboard.announcement import PLACEHOLDER_HELP, state
from dashboard.auth import csrf_token, require_admin
from dashboard.common import KST, rows, separate_submission_count
from database.sessions import (
    MAX_ANNOUNCEMENT_LENGTH,
    MAX_POSTPONE_WEEKS,
    ScheduleError,
    add_session,
    delete_session,
    get_template,
    list_sessions,
    postpone_from,
    rename_session,
    update_due_at,
)
from utils import format_remaining_time, get_current_session_info, tz_now

LOW_REMAINING = 2
COHORT = re.compile(r"^(\d+기)")
# 회차는 매주 한 번이라 마감 간격이 두 주에 가까우면 회고 없이 쉬어간 주다.
# 마감 시각을 조금 옮겨 둔 회차도 같이 잡히도록 하루 여유를 둔다.
WEEK = 7
REST_MIN_DAYS = WEEK * 2 - 1
POSTPONE_CHOICES = (1, 2, 3, 4)


def _cohort(name: str) -> str:
    matched = COHORT.match(name)
    return matched.group(1) if matched else "초기"


def _rest_weeks(previous: dict | None, row: dict) -> int:
    """앞 회차와의 간격에서 회고 없이 쉬어가는 주가 몇 주인지 센다.

    기수가 바뀌는 자리는 원래 몇 주씩 비므로 쉬어가는 주로 보지 않는다.
    """
    if previous is None or _cohort(previous["name"]) != _cohort(row["name"]):
        return 0
    gap = (row["due_at"] - previous["due_at"]).days
    if gap < REST_MIN_DAYS:
        return 0
    return max(round(gap / WEEK) - 1, 1)


def _collect(show_all: bool) -> dict:
    _, current_name, remaining, is_active = get_current_session_info()
    now = tz_now()

    schedule = list_sessions()
    names = [row["name"] for row in schedule]
    dues = [row["due_at"] for row in schedule]

    current_cohort = _cohort(current_name) if current_name else _cohort(names[-1])
    items = []
    movable = []
    upcoming = 0
    previous = None
    for row in schedule:
        name, due = row["name"], row["due_at"]
        # 쉬어가는 주는 걸러 내기 전에 센다. 기수만 보는 중이어도 앞뒤 회차
        # 간격은 전체 일정에서 나온다.
        rest_weeks = _rest_weeks(previous, row)
        previous = row
        if due > now:
            upcoming += 1
            movable.append(row)
        if not show_all and _cohort(name) != current_cohort:
            continue
        items.append(
            {
                **row,
                "name": name,
                "due": due,
                "past": due <= now,
                "current": name == current_name and is_active,
                "submitters": row["submissions"],
                # 마감이 지난 회차의 마감 시각을 옮기면 그 구간에 제출된 회고가
                # 다른 회차에 속한 것처럼 집계되므로 예정 회차만 고칠 수 있다.
                "editable": due > now,
                # 기록이 하나라도 붙으면 회차 이름을 바꿀 수 없다. 여기서는
                # 제출 건수만 보고, 출석 같은 다른 기록은 저장할 때 걸러진다.
                "changeable": (
                    due > now
                    and row["announced_at"] is None
                    and row["submissions"] == 0
                ),
                "announcement_state": state(row, now)[0],
                "rest_weeks": rest_weeks,
            }
        )
    return {
        "items": items,
        "movable": movable,
        "template": get_template(),
        "upcoming": upcoming,
        "current_name": current_name or "진행 중인 회차 없음",
        "current_cohort": current_cohort,
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "is_active": is_active,
        "last_due": dues[-1],
        "last_name": names[-1],
        "show_all": show_all,
    }


def _due_form(request, row: dict) -> str:
    """예정 회차만 마감 시각을 고칠 수 있게 폼을 연다."""
    if not row["editable"]:
        return ""
    value = row["due"].strftime("%Y-%m-%dT%H:%M")
    return (
        '<form method="post" class="inline" action="/schedule/due">'
        f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">'
        f'<input type="hidden" name="name" value="{escape(row["name"])}">'
        f'<input aria-label="마감 시각 (KST)" type="datetime-local" name="due_at" value="{value}" required>'
        "<button type=\"submit\">변경</button></form>"
    )


def _name_form(request, row: dict) -> str:
    """아직 기록이 붙지 않은 예정 회차만 이름을 바꾸거나 지울 수 있다."""
    if not row["changeable"]:
        return ""
    token = csrf_token(request)
    name = escape(row["name"])
    return (
        '<form method="post" class="inline" action="/schedule/rename">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{name}">'
        f'<input aria-label="새 회차 이름" type="text" name="new_name" value="{name}" required>'
        '<button type="submit">이름 변경</button></form>'
        '<form method="post" class="inline" action="/schedule/delete"'
        f' onsubmit="return confirm(\'{name} 회차를 지울까요?\')">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{name}">'
        '<button type="submit">삭제</button></form>'
    )


def _rest_row(weeks: int) -> str:
    return (
        '<tr class="rest"><td colspan="7">'
        f'쉬어가는 주 · {weeks}주 · 회고 없음'
        "</td></tr>"
    )


def _postpone_form(request, data: dict) -> str:
    """연휴로 쉬어갈 때 고른 회차부터 뒤쪽을 통째로 미루는 폼."""
    if not data["movable"]:
        return "<small>미룰 예정 회차가 없습니다.</small>"
    options = "".join(
        f'<option value="{escape(row["name"])}">'
        f'{escape(row["name"])} · {row["due_at"].strftime("%m-%d")}'
        f'({"월화수목금토일"[row["due_at"].weekday()]}) 마감</option>'
        for row in data["movable"]
    )
    weeks = "".join(
        f'<option value="{count}">{count}주</option>' for count in POSTPONE_CHOICES
    )
    return (
        '<form method="post" action="/schedule/postpone" class="filters">'
        f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">'
        f'<select aria-label="미룰 첫 회차" name="name">{options}</select>'
        f'<select aria-label="미룰 주 수" name="weeks">{weeks}</select>'
        '<button type="submit">이 회차부터 미루기</button></form>'
    )


def _schedule_rows(request, data: dict) -> str:
    items = []
    for row in data["items"]:
        if row["rest_weeks"]:
            items.append(_rest_row(row["rest_weeks"]))
        if row["current"]:
            status = '<span class="pill now">진행 중</span>'
        elif row["past"]:
            status = '<span class="pill">마감</span>'
        else:
            status = '<span class="pill pending">예정</span>'
        link = f'/schedule/announcement?{urlencode({"name": row["name"]})}'
        items.append(
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{row['due'].strftime('%Y-%m-%d %H:%M')} ({'월화수목금토일'[row['due'].weekday()]})</td>"
            f"<td>{status}</td>"
            f"<td>{row['submitters']}명{separate_submission_count(row['separate_submissions'])}</td>"
            f"<td>{row['announcement_state']} <a href=\"{link}\">문구</a></td>"
            f"<td>{_due_form(request, row)}</td>"
            f"<td>{_name_form(request, row)}</td>"
            "</tr>"
        )
    return rows(items, 7, "표시할 회차가 없습니다.")


@require_admin
async def handle(request: web.Request) -> web.Response:
    show_all = request.query.get("all") == "1"
    data = await asyncio.to_thread(_collect, show_all)

    warning = ""
    if data["upcoming"] == 0:
        warning = (
            '<div class="warn"><b>남은 회차가 없습니다.</b> '
            f'마지막 회차는 {escape(data["last_name"])}'
            f'({data["last_due"].strftime("%Y-%m-%d")})였습니다. '
            "아래에서 다음 회차를 추가해야 새 회차로 넘어갑니다.</div>"
        )
    elif data["upcoming"] <= LOW_REMAINING:
        warning = (
            f'<div class="warn">남은 회차가 {data["upcoming"]}개뿐입니다. '
            f'{data["last_due"].strftime("%Y-%m-%d")} 이후 일정을 미리 채워 두세요.</div>'
        )

    notice = ""
    if request.query.get("added"):
        notice = f'<div class="warn">`{escape(request.query["added"])}` 회차를 추가했습니다.</div>'
    elif request.query.get("updated"):
        notice = f'<div class="warn">`{escape(request.query["updated"])}` 마감 시각을 바꿨습니다.</div>'
    elif request.query.get("postponed"):
        moved = request.query.get("moved", "")
        stale = (
            " 이미 나간 공지에는 예전 마감이 적혀 있으니 채널에 따로 알려 주세요."
            if request.query.get("announced")
            else ""
        )
        filled = request.query.get("filled", "")
        made = (
            f" 비는 주는 `{escape(filled)}`(으)로 세워 뒀습니다. "
            "공지는 꺼져 있으니, 쉬어갈 주면 지우고 쓸 주면 이름과 공지를 정하세요."
            if filled
            else ""
        )
        notice = (
            f'<div class="warn">`{escape(request.query["postponed"])}`부터 '
            f'{escape(moved)}개 회차를 {escape(request.query.get("weeks", ""))}주씩 '
            f"미뤘습니다.{made}{stale}</div>"
        )
    elif request.query.get("renamed"):
        notice = (
            f'<div class="warn">`{escape(request.query["renamed"])}` 회차 이름을 '
            f'`{escape(request.query.get("to", ""))}`(으)로 바꿨습니다.</div>'
        )
    elif request.query.get("deleted"):
        notice = f'<div class="warn">`{escape(request.query["deleted"])}` 회차를 지웠습니다.</div>'
    elif request.query.get("template"):
        notice = '<div class="warn">제출 공지 기본 문구를 저장했습니다.</div>'
    elif request.query.get("error"):
        notice = f'<div class="warn">{escape(request.query["error"])}</div>'

    toggle = (
        '<a href="/schedule">현재 기수만 보기</a>'
        if show_all
        else '<a href="/schedule?all=1">전체 회차 보기</a>'
    )
    body = f"""
<section class="cards">
<div class="card">현재 회차<div class="number">{escape(data['current_name'])}</div><small>{'진행 중' if data['is_active'] else '마감'} · 마감까지 {data['remaining']}</small></div>
<div class="{'card alert' if data['upcoming'] <= LOW_REMAINING else 'card'}">남은 회차<div class="number">{data['upcoming']}개</div><small>마지막 마감 {data['last_due'].strftime('%Y-%m-%d')}</small></div>
</section>
{warning}
<div class="filters"><small>{escape(data['current_cohort'])} 일정</small>{toggle}</div>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>회차</th><th>마감 (KST)</th><th>상태</th><th>제출자</th><th>제출 공지</th><th>마감 변경</th><th>이름·삭제</th></tr></thead>
<tbody>{_schedule_rows(request, data)}</tbody></table></div>
<small>마감이 지난 회차는 바꿀 수 없습니다. 그 구간에 제출된 회고가 다른 회차에 속한 것처럼 집계되기 때문입니다.
회차 이름은 회고에 그대로 기록되는 값이라, 아직 제출도 공지도 없는 예정 회차만 이름을 바꾸거나 지울 수 있습니다.</small>
<h2>연휴로 회차 미루기</h2>
<small>추석·설처럼 한 주 쉬어갈 때 씁니다. 고른 회차부터 마지막 회차까지 한꺼번에 밀리므로
회차 이름과 순서, 회차 사이 간격은 그대로고 기수만 그만큼 늦게 끝납니다.
밀고 나서 비는 주에는 `추가 회차`가 공지가 꺼진 채로 세워집니다. 정말 쉬어갈 주면 위 표에서 지우고,
보충 회차로 쓸 거면 이름을 바꾸고 공지를 켜세요.
아직 나가지 않은 제출 공지도 같은 간격으로 따라 밀립니다. 세워진 `추가 회차`를 지우면 그 주는
위 표에 `쉬어가는 주`로 보입니다.
한 번에 최대 {MAX_POSTPONE_WEEKS}주까지 미룰 수 있습니다.</small>
{_postpone_form(request, data)}
<h2>회차 추가</h2>
<small>마지막 회차({escape(data['last_name'])}, {data['last_due'].strftime('%Y-%m-%d %H:%M')}) 뒤에만 붙일 수 있습니다.</small>
<form method="post" action="/schedule/add" class="filters">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<input aria-label="새 회차 이름" type="text" name="name" placeholder="7기 1회차" required>
<input aria-label="마감 시각 (KST)" type="datetime-local" name="due_at" required>
<button type="submit">추가</button></form>
<h2>제출 공지 기본 문구</h2>
<small>공지 시각이 되면 이 문구가 회차마다 나갑니다. {PLACEHOLDER_HELP}
특정 회차만 다르게 쓰려면 위 표의 `문구`에서 그 회차만 덮어씁니다.</small>
<form method="post" action="/schedule/announcement/template">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<textarea aria-label="공지 문구" name="body" rows="8" maxlength="{MAX_ANNOUNCEMENT_LENGTH}" required>{escape(data['template'])}</textarea>
<div class="filters"><button type="submit">기본 문구 저장</button></div></form>
{notice}
"""
    return web.Response(
        text=layout.render(
            title="회차 일정",
            active="/schedule",
            heading="회차 일정",
            subtitle="마감일과 남은 회차를 확인합니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


def _parse_due(raw: str) -> datetime.datetime:
    """datetime-local 입력을 KST 기준 시각으로 읽는다."""
    try:
        parsed = datetime.datetime.fromisoformat((raw or "").strip())
    except ValueError:
        raise ScheduleError("마감 시각 형식이 올바르지 않습니다.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _redirect(**query) -> web.HTTPFound:
    return web.HTTPFound("/schedule?" + urlencode(query))


@require_admin
async def handle_add(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        due_at = _parse_due(str(form.get("due_at", "")))
        await asyncio.to_thread(add_session, name, due_at)
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 회차를 추가합니다 - name={} due_at={}", name, due_at)
    raise _redirect(added=name)


def _parse_weeks(raw: str) -> int:
    try:
        return int((raw or "").strip())
    except ValueError:
        raise ScheduleError("미룰 주 수를 고르세요.") from None


@require_admin
async def handle_postpone(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        weeks = _parse_weeks(str(form.get("weeks", "")))
        moved = await asyncio.to_thread(postpone_from, name, weeks, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info(
        "관리자 웹에서 회차를 미룹니다 - name={} weeks={} moved={}",
        name,
        weeks,
        len(moved["names"]),
    )
    extra = {"announced": "1"} if moved["announced"] else {}
    if moved["filled"]:
        extra["filled"] = ", ".join(moved["filled"])
    raise _redirect(
        postponed=name, weeks=weeks, moved=len(moved["names"]), **extra
    )


@require_admin
async def handle_rename(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    new_name = str(form.get("new_name", "")).strip()
    try:
        saved = await asyncio.to_thread(rename_session, name, new_name, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 회차 이름을 바꿉니다 - name={} new_name={}", name, saved)
    raise _redirect(renamed=name, to=saved)


@require_admin
async def handle_delete(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        await asyncio.to_thread(delete_session, name, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 회차를 지웁니다 - name={}", name)
    raise _redirect(deleted=name)


@require_admin
async def handle_update_due(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        due_at = _parse_due(str(form.get("due_at", "")))
        await asyncio.to_thread(update_due_at, name, due_at, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 마감을 바꿉니다 - name={} due_at={}", name, due_at)
    raise _redirect(updated=name)
