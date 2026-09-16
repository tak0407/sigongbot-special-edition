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
from dashboard.common import KST, rows
from database.sessions import (
    MAX_ANNOUNCEMENT_LENGTH,
    ScheduleError,
    add_session,
    get_template,
    list_sessions,
    update_due_at,
)
from utils import format_remaining_time, get_current_session_info, tz_now

LOW_REMAINING = 2
COHORT = re.compile(r"^(\d+기)")


def _cohort(name: str) -> str:
    matched = COHORT.match(name)
    return matched.group(1) if matched else "초기"


def _collect(show_all: bool) -> dict:
    _, current_name, remaining, is_active = get_current_session_info()
    now = tz_now()

    schedule = list_sessions()
    names = [row["name"] for row in schedule]
    dues = [row["due_at"] for row in schedule]

    current_cohort = _cohort(current_name) if current_name else _cohort(names[-1])
    items = []
    upcoming = 0
    for row in schedule:
        name, due = row["name"], row["due_at"]
        if due > now:
            upcoming += 1
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
                "announcement_state": state(row, now)[0],
            }
        )
    return {
        "items": items,
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
        f'<input type="datetime-local" name="due_at" value="{value}" required>'
        "<button type=\"submit\">변경</button></form>"
    )


def _schedule_rows(request, data: dict) -> str:
    items = []
    for row in data["items"]:
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
            f"<td>{row['submitters']}명</td>"
            f"<td>{row['announcement_state']} <a href=\"{link}\">문구</a></td>"
            f"<td>{_due_form(request, row)}</td>"
            "</tr>"
        )
    return rows(items, 6, "표시할 회차가 없습니다.")


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
<table><thead><tr><th>회차</th><th>마감 (KST)</th><th>상태</th><th>제출자</th><th>제출 공지</th><th>마감 변경</th></tr></thead>
<tbody>{_schedule_rows(request, data)}</tbody></table>
<small>마감이 지난 회차는 바꿀 수 없습니다. 그 구간에 제출된 회고가 다른 회차에 속한 것처럼 집계되기 때문입니다.
회차 이름은 회고에 그대로 기록되는 값이라 만든 뒤에는 바꿀 수 없습니다.</small>
<h2>회차 추가</h2>
<small>마지막 회차({escape(data['last_name'])}, {data['last_due'].strftime('%Y-%m-%d %H:%M')}) 뒤에만 붙일 수 있습니다.</small>
<form method="post" action="/schedule/add" class="filters">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<input type="text" name="name" placeholder="7기 1회차" required>
<input type="datetime-local" name="due_at" required>
<button type="submit">추가</button></form>
<h2>제출 공지 기본 문구</h2>
<small>공지 시각이 되면 이 문구가 회차마다 나갑니다. {PLACEHOLDER_HELP}
특정 회차만 다르게 쓰려면 위 표의 `문구`에서 그 회차만 덮어씁니다.</small>
<form method="post" action="/schedule/announcement/template">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<textarea name="body" rows="8" maxlength="{MAX_ANNOUNCEMENT_LENGTH}" required>{escape(data['template'])}</textarea>
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
