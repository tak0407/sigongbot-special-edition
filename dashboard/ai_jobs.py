"""AI 처리 큐: 질문형 회고 정리 작업의 전체 상태와 재시도."""

import asyncio
from html import escape

from aiohttp import web
from loguru import logger

from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import csrf_token, require_admin
from dashboard.common import PAGE_SIZE, page_numbers, rows, since, to_kst, truncate
from database.sqlite import get_connection

STATUSES = ["pending", "processing", "completed", "failed"]
PREVIEW_LENGTH = 90


def _collect_list(status: str, page: int) -> dict:
    with get_connection() as connection:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS total FROM ai_review_jobs GROUP BY status"
            )
        }
        where, params = "", []
        if status:
            where, params = "WHERE status = ?", [status]
        total = connection.execute(
            f"SELECT COUNT(*) FROM ai_review_jobs {where}", params
        ).fetchone()[0]
        last_page, page = page_numbers(total, page)
        items = connection.execute(
            f"""
            SELECT id, user_id, slack_channel, slack_ts, status, attempts,
                   last_error, feedback, created_at, updated_at
              FROM ai_review_jobs
              {where}
             ORDER BY updated_at DESC, id DESC
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


def _collect_one(job_id: int) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM ai_review_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def _requeue(job_id: int) -> bool:
    """실패한 작업만 다시 대기열에 올린다. 워커가 pending을 집어 간다."""
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE ai_review_jobs
               SET status = 'pending', updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND status = 'failed'
            """,
            (job_id,),
        )
        return cursor.rowcount > 0


def _status_pill(status: str) -> str:
    return f'<span class="pill {escape(status)}">{escape(status)}</span>'


def _filter_form(data: dict) -> str:
    options = [f'<option value="">전체 ({sum(data["counts"].values())})</option>']
    for status in STATUSES:
        selected = " selected" if status == data["status"] else ""
        count = data["counts"].get(status, 0)
        options.append(
            f'<option value="{status}"{selected}>{status} ({count})</option>'
        )
    return (
        '<form class="filters" method="get">'
        f'<select name="status">{"".join(options)}</select>'
        '<button type="submit">필터</button>'
        f'<small>{data["total"]}건</small>'
        "</form>"
    )


def _pager(data: dict) -> str:
    if data["last_page"] <= 1:
        return ""
    status = f"&status={escape(data['status'])}" if data["status"] else ""
    parts = []
    if data["page"] > 1:
        parts.append(f'<a href="?page={data["page"] - 1}{status}">← 이전</a>')
    parts.append(f'<span>{data["page"]} / {data["last_page"]}</span>')
    if data["page"] < data["last_page"]:
        parts.append(f'<a href="?page={data["page"] + 1}{status}">다음 →</a>')
    return f'<div class="pager">{"".join(parts)}</div>'


def _list_rows(data: dict, directory: dict) -> str:
    items = []
    for row in data["items"]:
        note = row["last_error"] if row["status"] == "failed" else row["feedback"]
        note_class = "error" if row["status"] == "failed" else ""
        items.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{slack_directory.user_cell(directory, row['user_id'])}</td>"
            f"<td>{_status_pill(row['status'])}</td>"
            f"<td>{row['attempts']}회</td>"
            f"<td>{escape(to_kst(row['updated_at']))}<div class=\"sub\">{escape(since(row['updated_at']))}</div></td>"
            f'<td class="{note_class}">{escape(truncate(note, PREVIEW_LENGTH))}</td>'
            f'<td><a href="/ai-jobs/{row["id"]}">상세</a></td>'
            "</tr>"
        )
    return rows(items, 7, "조건에 맞는 작업이 없습니다.")


@require_admin
async def handle_list(request: web.Request) -> web.Response:
    status = request.query.get("status", "").strip()
    if status and status not in STATUSES:
        status = ""
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1

    data = await asyncio.to_thread(_collect_list, status, page)
    directory = await slack_directory.get_directory(
        request, {row["slack_channel"] for row in data["items"]}
    )
    notice = ""
    if request.query.get("retried") == "1":
        notice = '<div class="warn">작업을 다시 대기열에 올렸습니다.</div>'
    body = (
        f"{notice}{_filter_form(data)}"
        "<table><thead><tr><th>ID</th><th>사용자</th><th>상태</th><th>시도</th>"
        "<th>마지막 갱신 (KST)</th><th>오류 / 피드백</th><th></th></tr></thead>"
        f"<tbody>{_list_rows(data, directory)}</tbody></table>"
        f"{_pager(data)}"
    )
    return web.Response(
        text=layout.render(
            title="AI 처리 큐",
            active="/ai-jobs",
            heading="AI 처리 큐",
            subtitle="질문형 회고 이미지 정리 작업입니다. 실패한 작업은 상세에서 다시 시도할 수 있습니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


@require_admin
async def handle_detail(request: web.Request) -> web.Response:
    try:
        job_id = int(request.match_info["job_id"])
    except ValueError:
        raise web.HTTPNotFound(text="작업 ID가 올바르지 않습니다.")

    row = await asyncio.to_thread(_collect_one, job_id)
    if row is None:
        raise web.HTTPNotFound(text=f"ID {job_id} 작업을 찾을 수 없습니다.")

    directory = await slack_directory.get_directory(request, {row["slack_channel"]})
    retry = ""
    if row["status"] == "failed":
        retry = (
            f'<form method="post" action="/ai-jobs/{row["id"]}/retry">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">'
            '<button class="retry" type="submit">다시 시도</button></form>'
        )
    blocks = ""
    for key, label in [
        ("last_error", "마지막 오류"),
        ("feedback", "AI 피드백"),
        ("retrospective_text", "회고 본문"),
    ]:
        if row.get(key):
            blocks += (
                f'<div class="body-field"><div class="label">{escape(label)}</div>'
                f'<div class="text">{escape(row[key])}</div></div>'
            )
    body = f"""
<p><a href="/ai-jobs">← 목록으로</a></p>
<table><tbody>
<tr><th>ID</th><td>{row['id']}</td></tr>
<tr><th>사용자</th><td>{slack_directory.user_cell(directory, row['user_id'])}</td></tr>
<tr><th>상태</th><td>{_status_pill(row['status'])}</td></tr>
<tr><th>시도</th><td>{row['attempts']}회</td></tr>
<tr><th>대상 메시지</th><td>{slack_directory.message_cell(directory, row['slack_channel'], row['slack_ts'], 'Slack에서 열기')}</td></tr>
<tr><th>파일 ID</th><td>{escape(row['file_id'])}</td></tr>
<tr><th>등록</th><td>{escape(to_kst(row['created_at']))} (KST)</td></tr>
<tr><th>갱신</th><td>{escape(to_kst(row['updated_at']))} (KST) · {escape(since(row['updated_at']))}</td></tr>
</tbody></table>
{retry}{blocks}
"""
    return web.Response(
        text=layout.render(
            title=f"AI 작업 #{row['id']}",
            active="/ai-jobs",
            heading=f"AI 작업 #{row['id']}",
            subtitle=escape(row["status"]),
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


@require_admin
async def handle_retry(request: web.Request) -> web.Response:
    try:
        job_id = int(request.match_info["job_id"])
    except ValueError:
        raise web.HTTPNotFound(text="작업 ID가 올바르지 않습니다.")

    requeued = await asyncio.to_thread(_requeue, job_id)
    if not requeued:
        # 이미 처리 중이거나 완료된 작업은 건드리지 않는다.
        raise web.HTTPConflict(text="실패 상태인 작업만 다시 시도할 수 있습니다.")
    logger.info("관리자 웹에서 AI 작업을 재시도합니다 - job_id={}", job_id)
    raise web.HTTPFound("/ai-jobs?status=pending&retried=1")
