"""온라인 회고 모임의 회차별 출석 현황."""

import asyncio
from html import escape
from urllib.parse import quote

from aiohttp import web

from config import settings
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import rows, to_kst
from dashboard.directory import channel_cell, get_directory, user_cell
from database.sqlite import get_connection


def _collect(session_name: str) -> dict:
    with get_connection() as connection:
        recorded_sessions = [
            row[0]
            for row in connection.execute(
                """
                SELECT session_name
                  FROM online_retro_attendance
                 GROUP BY session_name
                 ORDER BY MAX(attended_at) DESC
                """
            )
        ]
        configured_sessions = [
            meeting.session_name
            for meeting in sorted(
                settings.ONLINE_RETRO_MEETINGS,
                key=lambda meeting: meeting.starts_at,
                reverse=True,
            )
        ]
        sessions = list(dict.fromkeys(configured_sessions + recorded_sessions))
        selected = (
            session_name if session_name in sessions else (sessions[0] if sessions else "")
        )
        records = [
            dict(row)
            for row in connection.execute(
                """
                SELECT team_channel, user_id, attended_at
                  FROM online_retro_attendance
                 WHERE session_name = ?
                 ORDER BY attended_at, user_id
                """,
                (selected,),
            )
        ] if selected else []
    return {"sessions": sessions, "selected": selected, "records": records}


def _session_filters(sessions: list[str], selected: str) -> str:
    if not sessions:
        return ""
    links = []
    for session in sessions:
        label = escape(session)
        if session == selected:
            links.append(f"<b>{label}</b>")
        else:
            links.append(f'<a href="/admin/attendance?session={quote(session)}">{label}</a>')
    return '<div class="filters">' + " · ".join(links) + "</div>"


@require_admin
async def handle(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(_collect, request.query.get("session", "").strip())
    directory = await get_directory(
        request, {row["team_channel"] for row in data["records"]}
    )
    table_rows = [
        "<tr>"
        f"<td>{channel_cell(directory, row['team_channel'])}</td>"
        f"<td>{user_cell(directory, row['user_id'])}</td>"
        f"<td>{escape(to_kst(row['attended_at']))}</td>"
        "</tr>"
        for row in data["records"]
    ]
    selected = data["selected"] or "출석 기록 없음"
    body = f"""
<section class="cards">
<div class="card">선택 회차<div class="number">{escape(selected)}</div></div>
<div class="card">출석 인원<div class="number">{len(data['records'])}명</div></div>
</section>
{_session_filters(data['sessions'], data['selected'])}
<table><thead><tr><th>팀 채널</th><th>참여자</th><th>출석 시각 (KST)</th></tr></thead>
<tbody>{rows(table_rows, 3, '아직 기록된 출석이 없습니다.')}</tbody></table>
"""
    return web.Response(
        text=layout.render(
            title="온라인 모임 출석",
            active="/admin/attendance",
            heading="온라인 모임 출석",
            subtitle="회차별 출석 버튼 기록을 확인합니다.",
            body=body,
            refresh=True,
            request=request,
        ),
        content_type="text/html",
    )
