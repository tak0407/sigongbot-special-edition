"""회고 열람: 제출된 회고 본문을 목록과 상세로 본다.

Slack `/관리자` 모달은 회고 ID를 눈으로 찾아 손으로 입력해야 한다. 여기서는
목록에서 바로 열어 본다. 수정과 삭제는 Slack 쪽에 그대로 둔다.
"""

import asyncio
from html import escape

from aiohttp import web

from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import PAGE_SIZE, page_numbers, rows, to_kst, truncate
from database.sqlite import get_connection

PREVIEW_LENGTH = 70

FIELDS = [
    ("good_points", "잘했고 좋았던 점"),
    ("improvements", "아쉽고 개선하고 싶은 점"),
    ("learnings", "새롭게 배운 점"),
    ("action_item", "해볼만한 액션 아이템"),
]


def _collect_list(session: str, page: int) -> dict:
    with get_connection() as connection:
        sessions = [
            row["session_name"]
            for row in connection.execute(
                """
                SELECT session_name, MAX(created_at) AS last_at
                  FROM retrospectives
                 GROUP BY session_name
                 ORDER BY last_at DESC
                """
            )
        ]
        where, params = "", []
        if session:
            where, params = "WHERE session_name = ?", [session]

        total = connection.execute(
            f"SELECT COUNT(*) FROM retrospectives {where}", params
        ).fetchone()[0]
        last_page, page = page_numbers(total, page)
        items = connection.execute(
            f"""
            SELECT id, user_id, session_name, slack_channel, slack_ts, created_at,
                   emotion_score, good_points, improvements, learnings, action_item
              FROM retrospectives
              {where}
             ORDER BY created_at DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
    return {
        "sessions": sessions,
        "session": session,
        "total": total,
        "page": page,
        "last_page": last_page,
        "items": [dict(row) for row in items],
    }


def _collect_one(retrospective_id: int) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM retrospectives WHERE id = ?", (retrospective_id,)
        ).fetchone()
    return dict(row) if row else None


def _filter_form(data: dict) -> str:
    options = ['<option value="">전체 회차</option>']
    for name in data["sessions"]:
        selected = " selected" if name == data["session"] else ""
        options.append(f'<option value="{escape(name)}"{selected}>{escape(name)}</option>')
    return (
        '<form class="filters" method="get">'
        f'<select name="session">{"".join(options)}</select>'
        "<button type=\"submit\">필터</button>"
        f'<small>{data["total"]}건</small>'
        "</form>"
    )


def _pager(data: dict) -> str:
    if data["last_page"] <= 1:
        return ""
    session = f"&session={escape(data['session'])}" if data["session"] else ""
    parts = []
    if data["page"] > 1:
        parts.append(f'<a href="?page={data["page"] - 1}{session}">← 이전</a>')
    parts.append(f'<span>{data["page"]} / {data["last_page"]}</span>')
    if data["page"] < data["last_page"]:
        parts.append(f'<a href="?page={data["page"] + 1}{session}">다음 →</a>')
    return f'<div class="pager">{"".join(parts)}</div>'


def _list_rows(data: dict, directory: dict) -> str:
    items = []
    for row in data["items"]:
        score = row["emotion_score"]
        items.append(
            "<tr>"
            f"<td>{slack_directory.message_cell(directory, row['slack_channel'], row['slack_ts'], to_kst(row['created_at']))}</td>"
            f"<td>{slack_directory.user_cell(directory, row['user_id'])}</td>"
            f"<td>{escape(row['session_name'])}</td>"
            f"<td>{slack_directory.channel_cell(directory, row['slack_channel'])}</td>"
            f"<td>{score if score is not None else '-'}</td>"
            f"<td>{escape(truncate(row['good_points'], PREVIEW_LENGTH))}</td>"
            f'<td><a href="/admin/retrospectives/{row["id"]}">열기</a></td>'
            "</tr>"
        )
    return rows(items, 7, "조건에 맞는 회고가 없습니다.")


@require_admin
async def handle_list(request: web.Request) -> web.Response:
    session = request.query.get("session", "").strip()
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1

    data = await asyncio.to_thread(_collect_list, session, page)
    directory = await slack_directory.get_directory(
        request, {row["slack_channel"] for row in data["items"]}
    )
    body = (
        f"{_filter_form(data)}"
        "<table><thead><tr><th>제출 시각 (KST)</th><th>작성자</th><th>회차</th>"
        "<th>채널</th><th>감정</th><th>잘했던 점</th><th></th></tr></thead>"
        f"<tbody>{_list_rows(data, directory)}</tbody></table>"
        f"{_pager(data)}"
    )
    return web.Response(
        text=layout.render(
            title="회고 열람",
            active="/admin/retrospectives",
            heading="회고 열람",
            subtitle="제출 시각을 누르면 Slack 메시지로, 열기를 누르면 본문 전체로 이동합니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


@require_admin
async def handle_detail(request: web.Request) -> web.Response:
    try:
        retrospective_id = int(request.match_info["retrospective_id"])
    except ValueError:
        raise web.HTTPNotFound(text="회고 ID가 올바르지 않습니다.")

    row = await asyncio.to_thread(_collect_one, retrospective_id)
    if row is None:
        raise web.HTTPNotFound(text=f"ID {retrospective_id} 회고를 찾을 수 없습니다.")

    directory = await slack_directory.get_directory(request, {row["slack_channel"]})
    fields = "".join(
        f'<div class="body-field"><div class="label">{escape(label)}</div>'
        f'<div class="text">{escape(row[key] or "-")}</div></div>'
        for key, label in FIELDS
    )
    emotion = ""
    if row["emotion_score"] is not None:
        emotion = (
            '<div class="body-field"><div class="label">감정 점수</div>'
            f'<div class="text">{row["emotion_score"]} / 10\n'
            f'{escape(row["emotion_reason"] or "")}</div></div>'
        )
    body = f"""
<p><a href="/admin/retrospectives">← 목록으로</a></p>
<table><tbody>
<tr><th>ID</th><td>{row['id']}</td></tr>
<tr><th>작성자</th><td>{slack_directory.user_cell(directory, row['user_id'])}</td></tr>
<tr><th>회차</th><td>{escape(row['session_name'])}</td></tr>
<tr><th>제출 시각</th><td>{slack_directory.message_cell(directory, row['slack_channel'], row['slack_ts'], to_kst(row['created_at']))} (KST)</td></tr>
<tr><th>채널</th><td>{slack_directory.channel_cell(directory, row['slack_channel'])}</td></tr>
<tr><th>수정 시각</th><td>{escape(to_kst(row['updated_at']))} (KST)</td></tr>
</tbody></table>
{fields}{emotion}
<small>수정과 삭제는 Slack의 <code>/관리자</code> 메뉴에서 합니다. 이 화면은 읽기 전용입니다.</small>
"""
    return web.Response(
        text=layout.render(
            title=f"회고 #{row['id']}",
            active="/admin/retrospectives",
            heading=f"회고 #{row['id']}",
            subtitle=escape(row["session_name"]),
            body=body,
            request=request,
        ),
        content_type="text/html",
    )
