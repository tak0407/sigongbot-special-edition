"""접수된 봇 개선 제안 목록, 상세, 처리 상태 변경."""

import asyncio
from html import escape
from urllib.parse import urlencode

from aiohttp import web
from loguru import logger

from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import csrf_token, require_admin
from dashboard.common import PAGE_SIZE, page_numbers, rows, to_kst, truncate
from database.sqlite import get_connection
from database.suggestions import STATUSES, update_suggestion_status

CATEGORY_LABELS = {
    "feature": "새 기능",
    "usability": "사용성 개선",
    "bug": "오류·불편",
    "other": "기타",
}
STATUS_LABELS = {
    "pending": "접수",
    "in_progress": "처리 중",
    "completed": "완료",
}


def _collect_list(status: str, page: int) -> dict:
    with get_connection() as connection:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS total "
                "FROM bot_improvement_suggestions GROUP BY status"
            )
        }
        where, params = ("WHERE status = ?", [status]) if status else ("", [])
        total = connection.execute(
            f"SELECT COUNT(*) FROM bot_improvement_suggestions {where}", params
        ).fetchone()[0]
        last_page, page = page_numbers(total, page)
        items = connection.execute(
            f"""
            SELECT id, category, content, user_id, submission_channel,
                   status, created_at, updated_at
              FROM bot_improvement_suggestions
              {where}
             ORDER BY created_at DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
    return {
        "counts": counts,
        "status": status,
        "total": total,
        "page": page,
        "last_page": last_page,
        "items": [dict(row) for row in items],
    }


def _collect_one(suggestion_id: int) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM bot_improvement_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    return dict(row) if row else None


def _status_pill(status: str) -> str:
    return (
        f'<span class="pill {escape(status)}">'
        f'{escape(STATUS_LABELS.get(status, status))}</span>'
    )


def _filter_form(data: dict) -> str:
    options = [f'<option value="">전체 ({sum(data["counts"].values())})</option>']
    for status in STATUSES:
        selected = " selected" if status == data["status"] else ""
        options.append(
            f'<option value="{status}"{selected}>'
            f'{STATUS_LABELS[status]} ({data["counts"].get(status, 0)})</option>'
        )
    return (
        '<form class="filters" method="get">'
        f'<select name="status" aria-label="상태 필터">{"".join(options)}</select>'
        '<button type="submit">필터</button>'
        f'<small>{data["total"]}건</small></form>'
    )


def _pager(data: dict) -> str:
    if data["last_page"] <= 1:
        return ""
    query = f'&status={escape(data["status"])}' if data["status"] else ""
    parts = []
    if data["page"] > 1:
        parts.append(f'<a href="?page={data["page"] - 1}{query}">← 이전</a>')
    parts.append(f'<span>{data["page"]} / {data["last_page"]}</span>')
    if data["page"] < data["last_page"]:
        parts.append(f'<a href="?page={data["page"] + 1}{query}">다음 →</a>')
    return f'<div class="pager">{"".join(parts)}</div>'


def _list_rows(data: dict, directory: dict) -> str:
    items = []
    for item in data["items"]:
        items.append(
            "<tr>"
            f"<td>{item['id']}</td>"
            f"<td>{escape(CATEGORY_LABELS.get(item['category'], item['category']))}</td>"
            f"<td>{escape(truncate(item['content'], 90))}</td>"
            f"<td>{slack_directory.user_cell(directory, item['user_id'])}</td>"
            f"<td>{slack_directory.channel_cell(directory, item['submission_channel'])}</td>"
            f"<td>{_status_pill(item['status'])}</td>"
            f"<td>{escape(to_kst(item['created_at']))}</td>"
            f'<td><a href="/suggestions/{item["id"]}">열기</a></td>'
            "</tr>"
        )
    return rows(items, 8, "접수된 제안이 없습니다.")


@require_admin
async def handle_list(request: web.Request) -> web.Response:
    status = request.query.get("status", "").strip()
    if status not in (*STATUSES, ""):
        status = ""
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1
    data = await asyncio.to_thread(_collect_list, status, page)
    directory = await slack_directory.get_directory(
        request, {item["submission_channel"] for item in data["items"]}
    )
    notice = (
        '<div class="warn">처리 상태를 변경했습니다.</div>'
        if request.query.get("updated") == "1"
        else ""
    )
    body = (
        f"{notice}{_filter_form(data)}"
        '<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>ID</th><th>분류</th><th>제안</th><th>작성자</th>'
        "<th>제출 채널</th><th>상태</th><th>생성 시각 (KST)</th><th></th></tr></thead>"
        f"<tbody>{_list_rows(data, directory)}</tbody></table></div>{_pager(data)}"
    )
    return web.Response(
        text=layout.render(
            title="봇 개선 제안",
            active="/suggestions",
            heading="봇 개선 제안",
            subtitle="Slack에서 접수된 의견을 확인하고 처리 상태를 관리합니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


@require_admin
async def handle_detail(request: web.Request) -> web.Response:
    try:
        suggestion_id = int(request.match_info["suggestion_id"])
    except ValueError:
        raise web.HTTPNotFound(text="제안 ID가 올바르지 않습니다.")
    item = await asyncio.to_thread(_collect_one, suggestion_id)
    if item is None:
        raise web.HTTPNotFound(text=f"ID {suggestion_id} 제안을 찾을 수 없습니다.")
    directory = await slack_directory.get_directory(
        request, {item["submission_channel"]}
    )
    options = "".join(
        f'<option value="{status}"{" selected" if status == item["status"] else ""}>'
        f'{STATUS_LABELS[status]}</option>'
        for status in STATUSES
    )
    body = f"""
<p><a href="/suggestions">← 목록으로</a></p>
<table class="detail-table"><tbody>
<tr><th>ID</th><td>{item['id']}</td></tr>
<tr><th>분류</th><td>{escape(CATEGORY_LABELS.get(item['category'], item['category']))}</td></tr>
<tr><th>작성자</th><td>{slack_directory.user_cell(directory, item['user_id'])}</td></tr>
<tr><th>제출 채널</th><td>{slack_directory.channel_cell(directory, item['submission_channel'])}</td></tr>
<tr><th>생성 시각</th><td>{escape(to_kst(item['created_at']))} (KST)</td></tr>
<tr><th>갱신 시각</th><td>{escape(to_kst(item['updated_at']))} (KST)</td></tr>
</tbody></table>
<div class="body-field"><div class="label">제안 내용</div>
<div class="text">{escape(item['content'])}</div></div>
<form class="inline" method="post" action="/suggestions/{item['id']}/status">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<label for="status">처리 상태</label><select id="status" name="status">{options}</select>
<button type="submit">변경</button></form>
"""
    return web.Response(
        text=layout.render(
            title=f"개선 제안 #{item['id']}",
            active="/suggestions",
            heading=f"개선 제안 #{item['id']}",
            subtitle=STATUS_LABELS.get(item["status"], item["status"]),
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


@require_admin
async def handle_status_update(request: web.Request) -> web.Response:
    try:
        suggestion_id = int(request.match_info["suggestion_id"])
    except ValueError:
        raise web.HTTPNotFound(text="제안 ID가 올바르지 않습니다.")
    form = await request.post()
    status = str(form.get("status", ""))
    if status not in STATUSES:
        raise web.HTTPBadRequest(text="처리 상태가 올바르지 않습니다.")
    updated = await asyncio.to_thread(
        update_suggestion_status, suggestion_id, status
    )
    if not updated:
        raise web.HTTPNotFound(text=f"ID {suggestion_id} 제안을 찾을 수 없습니다.")
    logger.info(
        "관리자 웹에서 제안 상태 변경 - suggestion_id={}, status={}",
        suggestion_id,
        status,
    )
    raise web.HTTPFound("/suggestions?" + urlencode({"status": status, "updated": "1"}))
