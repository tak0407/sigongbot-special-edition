"""회차 일정: 마감일과 남은 회차. 일정이 소진되기 전에 알아채기 위한 화면."""

import asyncio
import re
from html import escape

from aiohttp import web

from constants import DUE_DATES, SESSION_NAMES
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import rows
from database.sqlite import get_connection
from utils import format_remaining_time, get_current_session_info, tz_now

LOW_REMAINING = 2
COHORT = re.compile(r"^(\d+기)")


def _cohort(name: str) -> str:
    matched = COHORT.match(name)
    return matched.group(1) if matched else "초기"


def _collect(show_all: bool) -> dict:
    _, current_name, remaining, is_active = get_current_session_info()
    now = tz_now()

    with get_connection() as connection:
        submitters = {
            row["session_name"]: row["total"]
            for row in connection.execute(
                """
                SELECT session_name, COUNT(DISTINCT user_id) AS total
                  FROM retrospectives
                 GROUP BY session_name
                """
            )
        }

    current_cohort = _cohort(current_name) if current_name else _cohort(SESSION_NAMES[-1])
    items = []
    upcoming = 0
    for name, due in zip(SESSION_NAMES, DUE_DATES):
        if due > now:
            upcoming += 1
        if not show_all and _cohort(name) != current_cohort:
            continue
        items.append(
            {
                "name": name,
                "due": due,
                "past": due <= now,
                "current": name == current_name and is_active,
                "submitters": submitters.get(name, 0),
            }
        )
    return {
        "items": items,
        "upcoming": upcoming,
        "current_name": current_name or "진행 중인 회차 없음",
        "current_cohort": current_cohort,
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "is_active": is_active,
        "last_due": DUE_DATES[-1],
        "last_name": SESSION_NAMES[-1],
        "show_all": show_all,
    }


def _schedule_rows(data: dict) -> str:
    items = []
    for row in data["items"]:
        if row["current"]:
            state = '<span class="pill now">진행 중</span>'
        elif row["past"]:
            state = '<span class="pill">마감</span>'
        else:
            state = '<span class="pill pending">예정</span>'
        items.append(
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{row['due'].strftime('%Y-%m-%d %H:%M')} ({'월화수목금토일'[row['due'].weekday()]})</td>"
            f"<td>{state}</td>"
            f"<td>{row['submitters']}명</td>"
            "</tr>"
        )
    return rows(items, 4, "표시할 회차가 없습니다.")


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
            "constants.py의 DUE_DATES와 SESSION_NAMES에 다음 기수를 추가해야 "
            "새 회차로 넘어갑니다.</div>"
        )
    elif data["upcoming"] <= LOW_REMAINING:
        warning = (
            f'<div class="warn">남은 회차가 {data["upcoming"]}개뿐입니다. '
            f'{data["last_due"].strftime("%Y-%m-%d")} 이후 일정을 미리 채워 두세요.</div>'
        )

    toggle = (
        '<a href="/admin/schedule">현재 기수만 보기</a>'
        if show_all
        else '<a href="/admin/schedule?all=1">전체 회차 보기</a>'
    )
    body = f"""
<section class="cards">
<div class="card">현재 회차<div class="number">{escape(data['current_name'])}</div><small>{'진행 중' if data['is_active'] else '마감'} · 마감까지 {data['remaining']}</small></div>
<div class="{'card alert' if data['upcoming'] <= LOW_REMAINING else 'card'}">남은 회차<div class="number">{data['upcoming']}개</div><small>마지막 마감 {data['last_due'].strftime('%Y-%m-%d')}</small></div>
</section>
{warning}
<div class="filters"><small>{escape(data['current_cohort'])} 일정</small>{toggle}</div>
<table><thead><tr><th>회차</th><th>마감 (KST)</th><th>상태</th><th>제출자</th></tr></thead>
<tbody>{_schedule_rows(data)}</tbody></table>
<small>일정은 constants.py의 DUE_DATES / SESSION_NAMES에 있습니다. 이 화면은 읽기 전용입니다.</small>
"""
    return web.Response(
        text=layout.render(
            title="회차 일정",
            active="/admin/schedule",
            heading="회차 일정",
            subtitle="마감일과 남은 회차를 확인합니다.",
            body=body,
        ),
        content_type="text/html",
    )
