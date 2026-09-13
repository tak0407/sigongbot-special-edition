import asyncio
import base64
import datetime
import hmac
from html import escape
from zoneinfo import ZoneInfo

from aiohttp import web

from config import settings
from database.sqlite import get_connection
from utils import format_remaining_time, get_current_session_info

KST = ZoneInfo("Asia/Seoul")
REFRESH_SECONDS = 60
RECENT_LIMIT = 10
FAILED_LIMIT = 10
TREND_LIMIT = 8
ERROR_PREVIEW_LENGTH = 160


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


def _to_kst(value: str) -> str:
    """SQLite가 UTC로 남긴 시각 문자열을 한국 시간 표기로 바꾼다."""
    if not value:
        return "-"
    try:
        parsed = datetime.datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        # 예상 못 한 형식이면 원본을 그대로 보여 준다.
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _team_breakdown(destinations: dict[str, str], submitted: set[str]) -> list[dict]:
    """멤버 -> 채널 매핑을 채널별 제출/미제출 집계로 뒤집는다."""
    teams: dict[str, dict] = {}
    for user_id, channel in sorted(destinations.items()):
        team = teams.setdefault(
            channel, {"channel": channel, "members": 0, "submitted": 0, "missing": []}
        )
        team["members"] += 1
        if user_id in submitted:
            team["submitted"] += 1
        else:
            team["missing"].append(user_id)
    # 미제출이 남은 팀을 위로 올려 리마인드 대상을 먼저 보이게 한다.
    return sorted(teams.values(), key=lambda team: (not team["missing"], team["channel"]))


def _dashboard_data() -> dict:
    _, session_name, remaining, is_active = get_current_session_info()
    destinations = dict(settings.SUBMISSION_DESTINATIONS)
    expected = set(destinations)
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
             LIMIT ?
            """,
            (RECENT_LIMIT,),
        ).fetchall()
        failures = connection.execute(
            """
            SELECT user_id, attempts, last_error, updated_at
              FROM ai_review_jobs
             WHERE status = 'failed'
             ORDER BY updated_at DESC, id DESC
             LIMIT ?
            """,
            (FAILED_LIMIT,),
        ).fetchall()
        trend = connection.execute(
            """
            SELECT session_name,
                   COUNT(DISTINCT user_id) AS submitters,
                   MAX(created_at) AS last_at
              FROM retrospectives
             GROUP BY session_name
             ORDER BY last_at DESC, session_name DESC
             LIMIT ?
            """,
            (TREND_LIMIT,),
        ).fetchall()
    return {
        "session_name": session_name or "진행 중인 회차 없음",
        "status": "진행 중" if is_active else "마감",
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "submitted": len(submitted),
        "expected": len(expected),
        "missing": max(0, len(expected - submitted)),
        # 팀 배정이 없는 제출(테스트 계정 등)은 팀별 표에 잡히지 않으므로 따로 센다.
        "unassigned": len(submitted - expected),
        "teams": _team_breakdown(destinations, submitted),
        "pending_posts": pending_posts,
        "pending_ai": pending_ai,
        "failed_ai": failed_ai,
        "recent": [dict(row) for row in recent],
        "failures": [dict(row) for row in failures],
        "trend": [dict(row) for row in reversed([dict(row) for row in trend])],
        "generated_at": datetime.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
    }


STYLE = """
body{font-family:system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#202124}
header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
h2{margin-top:36px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0}
.card{background:#f5f7fa;border-radius:12px;padding:18px}
.card.alert{background:#fdecea}
.number{font-size:28px;font-weight:700;margin-top:8px}
table{border-collapse:collapse;width:100%}
th,td{padding:12px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}
small{color:#667085}
.mentions{font-family:ui-monospace,monospace;font-size:13px;color:#b42318;word-break:break-all}
.done{color:#087443}
.bar{width:180px}
.bar span{display:block;height:10px;border-radius:5px;background:#4c6ef5;min-width:2px}
.error{font-family:ui-monospace,monospace;font-size:12px;color:#b42318;white-space:pre-wrap;word-break:break-all}
"""


def _rows(rows: list[str], columns: int, empty: str) -> str:
    if rows:
        return "".join(rows)
    return f'<tr><td colspan="{columns}">{escape(empty)}</td></tr>'


def _team_rows(data: dict) -> str:
    rows = []
    for team in data["teams"]:
        if team["missing"]:
            mentions = " ".join(f"<@{user_id}>" for user_id in team["missing"])
            missing_cell = f'<span class="mentions">{escape(mentions)}</span>'
        else:
            missing_cell = '<span class="done">전원 제출</span>'
        rows.append(
            "<tr>"
            f"<td>{escape(team['channel'])}</td>"
            f"<td>{team['submitted']} / {team['members']}</td>"
            f"<td>{len(team['missing'])}</td>"
            f"<td>{missing_cell}</td>"
            "</tr>"
        )
    return _rows(rows, 4, "SUBMISSION_TEAMS가 비어 있어 팀별 집계를 만들 수 없습니다.")


def _trend_rows(data: dict) -> str:
    peak = max((row["submitters"] for row in data["trend"]), default=0)
    rows = []
    for row in data["trend"]:
        width = round(row["submitters"] / peak * 100) if peak else 0
        rows.append(
            "<tr>"
            f"<td>{escape(row['session_name'])}</td>"
            f"<td>{row['submitters']}명</td>"
            f'<td class="bar"><span style="width:{width}%"></span></td>'
            "</tr>"
        )
    return _rows(rows, 3, "집계할 회고가 없습니다.")


def _recent_rows(data: dict) -> str:
    rows = [
        "<tr>"
        f"<td>{escape(row['user_id'])}</td>"
        f"<td>{escape(row['session_name'])}</td>"
        f"<td>{escape(_to_kst(row['created_at']))}</td>"
        f"<td>{escape(row['slack_channel'])}</td>"
        "</tr>"
        for row in data["recent"]
    ]
    return _rows(rows, 4, "아직 제출된 회고가 없습니다.")


def _failure_rows(data: dict) -> str:
    rows = []
    for row in data["failures"]:
        message = (row["last_error"] or "기록된 오류 메시지가 없습니다.").strip()
        if len(message) > ERROR_PREVIEW_LENGTH:
            message = message[:ERROR_PREVIEW_LENGTH] + "…"
        rows.append(
            "<tr>"
            f"<td>{escape(row['user_id'])}</td>"
            f"<td>{escape(_to_kst(row['updated_at']))}</td>"
            f"<td>{row['attempts']}회</td>"
            f'<td class="error">{escape(message)}</td>'
            "</tr>"
        )
    return _rows(rows, 4, "실패한 AI 처리 작업이 없습니다.")


def _page(data: dict) -> str:
    unassigned_note = (
        f" · 팀 미배정 제출 {data['unassigned']}건" if data["unassigned"] else ""
    )
    failed_card_class = "card alert" if data["failed_ai"] else "card"
    pending_posts_card_class = "card alert" if data["pending_posts"] else "card"
    return f"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>시공삶 관리자</title>
<style>{STYLE}</style>
<body><header><h1>시공삶 관리자</h1><small>{REFRESH_SECONDS}초마다 자동 새로고침 · 기준 {escape(data['generated_at'])} (KST)</small></header>
<section class="cards"><div class="card">현재 회차<div class="number">{escape(data['session_name'])}</div><small>{data['status']} · 마감까지 {data['remaining']}</small></div>
<div class="card">제출 현황<div class="number">{data['submitted']} / {data['expected']}</div><small>미제출 {data['missing']}명{unassigned_note}</small></div>
<div class="card">AI 처리 대기<div class="number">{data['pending_ai']}건</div><small>질문형 회고 이미지 처리</small></div>
<div class="{failed_card_class}">처리 오류<div class="number">{data['failed_ai']}건</div><small>아래 실패 목록에서 원인을 확인하세요.</small></div>
<div class="{pending_posts_card_class}">게시 기록 미확인<div class="number">{data['pending_posts']}건</div><small>Slack 게시 후 상태 기록이 끝나지 않은 회고</small></div></section>
<h2>팀별 제출 현황</h2><small>미제출자는 Slack에 그대로 붙여 넣으면 멘션으로 바뀝니다.</small>
<table><thead><tr><th>채널</th><th>제출</th><th>미제출</th><th>미제출자</th></tr></thead><tbody>{_team_rows(data)}</tbody></table>
<h2>회차별 제출 추이</h2><small>최근 {TREND_LIMIT}개 회차의 제출자 수입니다. 기수마다 인원이 달라 비율이 아닌 인원으로 표시합니다.</small>
<table><thead><tr><th>회차</th><th>제출자</th><th></th></tr></thead><tbody>{_trend_rows(data)}</tbody></table>
<h2>최근 제출</h2><table><thead><tr><th>사용자</th><th>회차</th><th>제출 시각 (KST)</th><th>게시 채널</th></tr></thead><tbody>{_recent_rows(data)}</tbody></table>
<h2>AI 처리 실패</h2><table><thead><tr><th>사용자</th><th>마지막 시도 (KST)</th><th>시도</th><th>오류</th></tr></thead><tbody>{_failure_rows(data)}</tbody></table>
</body></html>"""


async def admin_dashboard(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="sigongbot-admin"'})
    data = await asyncio.to_thread(_dashboard_data)
    return web.Response(text=_page(data), content_type="text/html")


def register_dashboard_routes(app: web.Application) -> None:
    app.router.add_get("/admin", admin_dashboard)
