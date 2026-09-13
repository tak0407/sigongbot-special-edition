import asyncio
import base64
import hmac
from html import escape

from aiohttp import web

from config import settings
from database.sqlite import get_connection
from utils import format_remaining_time, get_current_session_info


def _authorized(request: web.Request) -> bool:
    password = settings.DASHBOARD_PASSWORD
    if not password:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        username, supplied = base64.b64decode(header[6:]).decode().split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return False
    return username == "admin" and hmac.compare_digest(supplied, password)


def _dashboard_data() -> dict:
    _, session_name, remaining, is_active = get_current_session_info()
    expected = set(settings.SUBMISSION_DESTINATIONS)
    with get_connection() as connection:
        submitted = {
            row["user_id"]
            for row in connection.execute(
                "SELECT DISTINCT user_id FROM retrospectives WHERE session_name = ?",
                (session_name,),
            )
        }
        pending_posts = connection.execute(
            "SELECT COUNT(*) FROM retrospectives WHERE slack_post_status = 'pending'"
        ).fetchone()[0]
        failed_ai = connection.execute(
            "SELECT COUNT(*) FROM ai_review_jobs WHERE status = 'failed'"
        ).fetchone()[0]
        pending_ai = connection.execute(
            "SELECT COUNT(*) FROM ai_review_jobs WHERE status IN ('pending', 'processing')"
        ).fetchone()[0]
        recent = connection.execute(
            """
            SELECT user_id, session_name, created_at, slack_channel
              FROM retrospectives
             ORDER BY created_at DESC, id DESC
             LIMIT 10
            """
        ).fetchall()
    return {
        "session_name": session_name or "진행 중인 회차 없음",
        "status": "진행 중" if is_active else "마감",
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "submitted": len(submitted),
        "expected": len(expected),
        "missing": max(0, len(expected - submitted)),
        "pending_posts": pending_posts,
        "pending_ai": pending_ai,
        "failed_ai": failed_ai,
        "recent": [dict(row) for row in recent],
    }


def _page(data: dict) -> str:
    recent_rows = "".join(
        "<tr>"
        f"<td>{escape(row['user_id'])}</td>"
        f"<td>{escape(row['session_name'])}</td>"
        f"<td>{escape(row['created_at'])}</td>"
        f"<td>{escape(row['slack_channel'])}</td>"
        "</tr>"
        for row in data["recent"]
    ) or "<tr><td colspan=\"4\">아직 제출된 회고가 없습니다.</td></tr>"
    return f"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>시공삶 관리자</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#202124}}header{{display:flex;justify-content:space-between;align-items:baseline}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0}}.card{{background:#f5f7fa;border-radius:12px;padding:18px}}.number{{font-size:28px;font-weight:700;margin-top:8px}}table{{border-collapse:collapse;width:100%}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #ddd}}small{{color:#667085}}</style>
<body><header><h1>시공삶 관리자</h1><small>자동 새로고침 없음 · 새로고침해 최신 상태를 확인하세요.</small></header>
<section class="cards"><div class="card">현재 회차<div class="number">{escape(data['session_name'])}</div><small>{data['status']} · 마감까지 {data['remaining']}</small></div>
<div class="card">제출 현황<div class="number">{data['submitted']} / {data['expected']}</div><small>미제출 {data['missing']}명</small></div>
<div class="card">AI 처리 대기<div class="number">{data['pending_ai']}건</div><small>질문형 회고 이미지 처리</small></div>
<div class="card">처리 오류<div class="number">{data['failed_ai']}건</div><small>Slack 로그에서 원인을 확인하세요.</small></div>
<div class="card">게시 기록 미확인<div class="number">{data['pending_posts']}건</div><small>Slack 게시 후 상태 기록이 끝나지 않은 회고</small></div></section>
<h2>최근 제출</h2><table><thead><tr><th>사용자</th><th>회차</th><th>제출 시각 (UTC)</th><th>게시 채널</th></tr></thead><tbody>{recent_rows}</tbody></table>
</body></html>"""


async def admin_dashboard(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="sigongbot-admin"'})
    data = await asyncio.to_thread(_dashboard_data)
    return web.Response(text=_page(data), content_type="text/html")


def register_dashboard_routes(app: web.Application) -> None:
    app.router.add_get("/admin", admin_dashboard)
