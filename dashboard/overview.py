"""대시보드: 이번 회차 제출 현황."""

import asyncio
from html import escape

from aiohttp import web

from config import settings
from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import (
    REFRESH_SECONDS, rows, to_kst, separate_submission_badge, separate_submission_count,
)
from database.sqlite import get_connection
from database.submission_stats import separate_submitter_ids, session_submission_counts
from utils import format_remaining_time, get_current_session_info, tz_now

RECENT_LIMIT = 10
TREND_LIMIT = 8


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


def _collect() -> dict:
    _, session_name, remaining, is_active = get_current_session_info()
    destinations = dict(settings.SUBMISSION_DESTINATIONS)
    expected = set(destinations)
    with get_connection() as connection:
        submitted = {
            row["user_id"]
            for row in connection.execute(
                "SELECT DISTINCT user_id FROM retrospectives WHERE session_name = ? AND is_test_submission = 0",
                (session_name,),
            )
        }
        # Slack에는 게시됐지만 게시 상태 기록이 끝나지 않은 회고. 남아 있으면
        # 그 회고의 slack_ts가 비어 있어 관리자 화면에서 수정·삭제할 수 없다.
        pending_posts = connection.execute(
            "SELECT COUNT(*) FROM retrospectives WHERE slack_post_status = 'pending'"
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
        trend = session_submission_counts(connection)[:TREND_LIMIT]
    separate = submitted & separate_submitter_ids()
    submitted -= separate
    return {
        "session_name": session_name or "진행 중인 회차 없음",
        "status": "진행 중" if is_active else "마감",
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "submitted": len(submitted),
        "separate": sorted(separate),
        "expected": len(expected),
        "missing": max(0, len(expected - submitted)),
        # 별도 제출 계정을 제외한 팀 미배정 제출은 배정 누락 점검용으로 센다.
        "unassigned": len(submitted - expected),
        "teams": _team_breakdown(destinations, submitted),
        "pending_posts": pending_posts,
        "recent": [dict(row) for row in recent],
        "trend": list(reversed(trend)),
        "generated_at": tz_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _team_rows(data: dict, directory: dict) -> str:
    items = []
    for team in data["teams"]:
        if team["missing"]:
            names = ", ".join(
                slack_directory.user_label(directory, user_id)
                for user_id in team["missing"]
            )
            mentions = " ".join(f"<@{user_id}>" for user_id in team["missing"])
            missing_cell = (
                f"{escape(names)}<div class=\"mentions\">{escape(mentions)}</div>"
            )
        else:
            missing_cell = '<span class="done">전원 제출</span>'
        items.append(
            "<tr>"
            f"<td>{slack_directory.channel_cell(directory, team['channel'])}</td>"
            f"<td>{team['submitted']} / {team['members']}</td>"
            f"<td>{len(team['missing'])}</td>"
            f"<td>{missing_cell}</td>"
            "</tr>"
        )
    return rows(items, 4, "SUBMISSION_TEAMS가 비어 있어 팀별 집계를 만들 수 없습니다.")


def _trend_rows(data: dict) -> str:
    peak = max((row["submitters"] for row in data["trend"]), default=0)
    items = []
    for row in data["trend"]:
        width = round(row["submitters"] / peak * 100) if peak else 0
        items.append(
            "<tr>"
            f"<td>{escape(row['session_name'])}</td>"
            f"<td>{row['submitters']}명{separate_submission_count(row['separate_submitters'])}</td>"
            f'<td class="bar"><span style="width:{width}%"></span></td>'
            "</tr>"
        )
    return rows(items, 3, "집계할 회고가 없습니다.")


def _recent_rows(data: dict, directory: dict) -> str:
    items = [
        "<tr>"
        f"<td>{slack_directory.user_cell(directory, row['user_id'])}{separate_submission_badge(row['user_id'])}</td>"
        f"<td>{escape(row['session_name'])}</td>"
        f"<td>{slack_directory.message_cell(directory, row['slack_channel'], row['slack_ts'], to_kst(row['created_at']))}</td>"
        f"<td>{slack_directory.channel_cell(directory, row['slack_channel'])}</td>"
        "</tr>"
        for row in data["recent"]
    ]
    return rows(items, 4, "아직 제출된 회고가 없습니다.")


@require_admin
async def handle(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(_collect)
    channel_ids = {team["channel"] for team in data["teams"]}
    channel_ids.update(row["slack_channel"] for row in data["recent"])
    directory = await slack_directory.get_directory(request, channel_ids)

    unassigned = (
        f" · 팀 미배정 제출 {data['unassigned']}건" if data["unassigned"] else ""
    )
    separate_card = ""
    if data["separate"]:
        names = ", ".join(slack_directory.user_label(directory, uid) for uid in data["separate"])
        separate_card = (
            '<div class="card">별도 제출'
            f'<div class="number">{len(data["separate"])}명</div>'
            f'<small>{escape(names)} · 제출 수 집계 제외</small></div>'
        )
    pending_posts_card = "card alert" if data["pending_posts"] else "card"
    body = f"""
<section class="cards">
<div class="card">현재 회차<div class="number">{escape(data['session_name'])}</div><small>{data['status']} · 마감까지 {data['remaining']}</small></div>
<div class="card">제출 현황<div class="number">{data['submitted']} / {data['expected']}</div><small>미제출 {data['missing']}명{unassigned}</small></div>
<div class="{pending_posts_card}">게시 기록 미확인<div class="number">{data['pending_posts']}건</div><small>Slack 게시 후 상태 기록이 끝나지 않은 회고</small></div>
{separate_card}
</section>
<h2>팀별 제출 현황</h2><small>미제출자는 Slack에 그대로 붙여 넣으면 멘션으로 바뀝니다.</small>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>채널</th><th>제출</th><th>미제출</th><th>미제출자</th></tr></thead><tbody>{_team_rows(data, directory)}</tbody></table></div>
<h2>회차별 제출 추이</h2><small>최근 {TREND_LIMIT}개 회차의 제출자 수입니다. 기수마다 인원이 달라 비율이 아닌 인원으로 표시합니다.</small>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>회차</th><th>제출자</th><th></th></tr></thead><tbody>{_trend_rows(data)}</tbody></table></div>
<h2>최근 제출</h2><small>제출 시각을 누르면 Slack 메시지로 이동합니다.</small>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>사용자</th><th>회차</th><th>제출 시각 (KST)</th><th>게시 채널</th></tr></thead><tbody>{_recent_rows(data, directory)}</tbody></table></div>
"""
    return web.Response(
        text=layout.render(
            title="시공삶 관리자",
            active="/",
            heading="대시보드",
            subtitle=f"{REFRESH_SECONDS}초마다 자동 새로고침 · 기준 {escape(data['generated_at'])} (KST)",
            body=body,
            refresh=True,
            request=request,
        ),
        content_type="text/html",
    )
