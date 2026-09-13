import asyncio
import base64
import datetime
import hmac
from html import escape
from zoneinfo import ZoneInfo

from aiohttp import web
from loguru import logger

from config import settings
from database.sqlite import get_connection
from utils import format_remaining_time, get_current_session_info

KST = ZoneInfo("Asia/Seoul")
REFRESH_SECONDS = 60
RECENT_LIMIT = 10
FAILED_LIMIT = 10
TREND_LIMIT = 8
ERROR_PREVIEW_LENGTH = 160
DIRECTORY_TTL = datetime.timedelta(minutes=10)
USER_PAGE_LIMIT = 200
USER_PAGE_MAX = 10

# Slack 이름/링크 정보는 프로세스 안에서만 캐시한다. 페이지가 60초마다 자동
# 새로고침되므로 매 요청마다 Slack API를 부르면 rate limit에 걸린다.
_directory_cache: dict = {"expires_at": None, "value": None}

SLACK_CLIENT = web.AppKey("slack_client")


def _empty_directory() -> dict:
    return {"url": "", "users": {}, "channels": {}}


async def _load_directory(client) -> dict:
    """Slack에서 워크스페이스 URL과 사용자/채널 이름표를 받아 온다."""
    directory = _empty_directory()

    try:
        auth = await client.auth_test()
        directory["url"] = str(auth["url"]).rstrip("/")
    except Exception as error:
        logger.warning("Slack auth.test 실패 - 대시보드 링크를 비활성화합니다 - {}", error)

    # users:read 스코프가 없으면 여기서 실패한다. 이름 없이 ID로 표시하면 되므로
    # 대시보드 전체를 실패시키지 않는다.
    cursor = ""
    for _ in range(USER_PAGE_MAX):
        try:
            page = await client.users_list(limit=USER_PAGE_LIMIT, cursor=cursor)
        except Exception as error:
            logger.warning("Slack users.list 실패 - 사용자는 ID로 표시합니다 - {}", error)
            break
        for member in page.get("members", []):
            profile = member.get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or member.get("name")
                or ""
            ).strip()
            if name:
                directory["users"][member["id"]] = name
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    return directory


async def _resolve_channels(client, directory: dict, channel_ids: set[str]) -> None:
    """아직 이름을 모르는 채널만 조회해 캐시에 채운다."""
    for channel_id in sorted(channel_ids - set(directory["channels"])):
        try:
            info = await client.conversations_info(channel=channel_id)
            directory["channels"][channel_id] = info["channel"]["name"]
        except Exception as error:
            # 비공개 채널은 groups:read가 없으면 조회되지 않는다. ID로 표시한다.
            logger.warning(
                "Slack conversations.info 실패 - channel={} {}", channel_id, error
            )


async def _directory(client, channel_ids: set[str]) -> dict:
    if client is None:
        return _empty_directory()

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    expires_at = _directory_cache["expires_at"]
    if _directory_cache["value"] is None or expires_at is None or now >= expires_at:
        _directory_cache["value"] = await _load_directory(client)
        _directory_cache["expires_at"] = now + DIRECTORY_TTL

    directory = _directory_cache["value"]
    await _resolve_channels(client, directory, channel_ids)
    return directory


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
            SELECT user_id, session_name, created_at, slack_channel, slack_ts
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
.mentions{font-family:ui-monospace,monospace;font-size:12px;color:#b42318;word-break:break-all;margin-top:4px}
.sub{font-family:ui-monospace,monospace;font-size:11px;color:#98a2b3;margin-top:2px}
a{color:#2b5ce6}
.done{color:#087443}
.bar{width:180px}
.bar span{display:block;height:10px;border-radius:5px;background:#4c6ef5;min-width:2px}
.error{font-family:ui-monospace,monospace;font-size:12px;color:#b42318;white-space:pre-wrap;word-break:break-all}
"""


def _user_label(directory: dict, user_id: str) -> str:
    """이름을 알면 이름을, 모르면 ID를 그대로 보여 준다."""
    return directory["users"].get(user_id) or user_id


def _user_cell(directory: dict, user_id: str) -> str:
    name = directory["users"].get(user_id)
    if not name:
        return escape(user_id)
    return f'{escape(name)}<div class="sub">{escape(user_id)}</div>'


def _channel_cell(directory: dict, channel_id: str) -> str:
    name = directory["channels"].get(channel_id)
    label = f"#{name}" if name else channel_id
    url = directory["url"]
    if not url:
        return escape(label)
    href = f"{url}/archives/{channel_id}"
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(label)}</a>'


def _message_cell(directory: dict, channel_id: str, slack_ts: str, label: str) -> str:
    """제출 메시지로 바로 가는 Slack 딥링크를 만든다."""
    url = directory["url"]
    if not url or not slack_ts:
        return escape(label)
    href = f"{url}/archives/{channel_id}/p{slack_ts.replace('.', '')}"
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(label)}</a>'


def _rows(rows: list[str], columns: int, empty: str) -> str:
    if rows:
        return "".join(rows)
    return f'<tr><td colspan="{columns}">{escape(empty)}</td></tr>'


def _team_rows(data: dict) -> str:
    directory = data["directory"]
    rows = []
    for team in data["teams"]:
        if team["missing"]:
            names = ", ".join(
                _user_label(directory, user_id) for user_id in team["missing"]
            )
            mentions = " ".join(f"<@{user_id}>" for user_id in team["missing"])
            missing_cell = (
                f"{escape(names)}"
                f'<div class="mentions">{escape(mentions)}</div>'
            )
        else:
            missing_cell = '<span class="done">전원 제출</span>'
        rows.append(
            "<tr>"
            f"<td>{_channel_cell(directory, team['channel'])}</td>"
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
    directory = data["directory"]
    rows = [
        "<tr>"
        f"<td>{_user_cell(directory, row['user_id'])}</td>"
        f"<td>{escape(row['session_name'])}</td>"
        f"<td>{_message_cell(directory, row['slack_channel'], row['slack_ts'], _to_kst(row['created_at']))}</td>"
        f"<td>{_channel_cell(directory, row['slack_channel'])}</td>"
        "</tr>"
        for row in data["recent"]
    ]
    return _rows(rows, 4, "아직 제출된 회고가 없습니다.")


def _failure_rows(data: dict) -> str:
    directory = data["directory"]
    rows = []
    for row in data["failures"]:
        message = (row["last_error"] or "기록된 오류 메시지가 없습니다.").strip()
        if len(message) > ERROR_PREVIEW_LENGTH:
            message = message[:ERROR_PREVIEW_LENGTH] + "…"
        rows.append(
            "<tr>"
            f"<td>{_user_cell(directory, row['user_id'])}</td>"
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
    channel_ids = {team["channel"] for team in data["teams"]}
    channel_ids.update(row["slack_channel"] for row in data["recent"])
    data["directory"] = await _directory(request.app.get(SLACK_CLIENT), channel_ids)
    return web.Response(text=_page(data), content_type="text/html")


def register_dashboard_routes(app: web.Application, slack_client=None) -> None:
    """slack_client를 넘기지 않으면 사용자/채널을 ID 그대로 표시한다."""
    app[SLACK_CLIENT] = slack_client
    app.router.add_get("/admin", admin_dashboard)
