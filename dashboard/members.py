"""멤버: 회차별 참여 이력과 이탈 징후.

대시보드 탭은 이번 회차만 보여 주므로 "지난주에도 안 냈던 사람"이 보이지 않는다.
여기서는 마감이 지난 회차를 쭉 훑어 연속 미제출을 세고, 이탈 위험이 큰 순서로 올린다.
"""

import asyncio
import re
from html import escape

from aiohttp import web

from config import settings
from constants import MAX_PASS_COUNT
from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import rows, to_kst, separate_submission_badge
from database.sessions import load_schedule
from database.sqlite import get_connection
from database.submission_stats import separate_submitter_ids
from utils import get_current_session_info, tz_now

# 표에 점으로 찍어 줄 최근 회차 수. 너무 길면 한 줄을 넘어간다.
RECENT_MARKS = 10
# 패스를 누가 썼는지 표에 보일 이름.
PASS_LABELS = {"manual": "직접", "auto": "자동"}
# 이 횟수 이상 연속으로 빠지면 이탈 위험으로 본다.
AT_RISK_MISSES = 2
COHORT = re.compile(r"^(\d+기)")


def _cohort(name: str) -> str:
    matched = COHORT.match(name or "")
    return matched.group(1) if matched else "초기"


def _streak(sessions: list[str], submitted: set[str]) -> int:
    """마감이 지난 회차를 최신순으로 훑어 연속 미제출 횟수를 센다."""
    count = 0
    for name in reversed(sessions):
        if name in submitted:
            break
        count += 1
    return count


def _collect(cohort: str = "") -> dict:
    now = tz_now()
    _, current_name, _, is_active = get_current_session_info()
    names, dues = load_schedule()
    current_cohort = _cohort(current_name or (names[-1] if names else ""))
    destinations = dict(settings.SUBMISSION_DESTINATIONS)

    with get_connection() as connection:
        recorded_names = [
            row["session_name"]
            for row in connection.execute(
                "SELECT DISTINCT session_name FROM retrospectives WHERE is_test_submission = 0"
            )
        ]
        cohorts = sorted(
            {_cohort(name) for name in [*names, *recorded_names]} | {current_cohort},
            key=lambda name: int(name[:-1]) if name != "초기" else 0,
            reverse=True,
        )
        if cohort not in cohorts:
            cohort = current_cohort
        selected_names = [name for name in recorded_names if _cohort(name) == cohort]
        # 테스트 제출은 참여 이력에서 제외한다. 최신순으로 받아 두면 사용자별
        # 첫 행이 곧 마지막 제출이라 MAX()의 동점 처리에 기대지 않아도 된다.
        records = []
        if selected_names:
            placeholders = ",".join("?" for _ in selected_names)
            records = connection.execute(
                f"""
                SELECT user_id, session_name, slack_channel, created_at
                  FROM retrospectives
                 WHERE is_test_submission = 0
                   AND session_name IN ({placeholders})
                 ORDER BY created_at DESC, id DESC
                """,
                selected_names,
            ).fetchall()
        pass_rows = connection.execute(
            "SELECT user_id, session_name, source FROM session_passes WHERE cohort = ?",
            (cohort,),
        ).fetchall()

    # 일정 편집 결과를 반영하고, 아직 제출할 수 있는 회차는 분모에서 뺀다.
    sessions = [
        name
        for name, due in zip(names, dues)
        if due <= now and _cohort(name) == cohort
    ]
    scope = set(sessions)
    session_order = {name: index for index, name in enumerate(names)}
    is_current_cohort = cohort == current_cohort
    submitted: dict[str, set[str]] = {}
    last_by_user: dict[str, dict] = {}
    for row in records:
        submitted.setdefault(row["user_id"], set()).add(row["session_name"])
        last_by_user.setdefault(row["user_id"], dict(row))

    passes: dict[str, dict[str, str]] = {}
    for row in pass_rows:
        passes.setdefault(row["user_id"], {})[row["session_name"]] = row["source"]

    # 현재 기수는 미제출자도 볼 수 있도록 운영 명단을 포함한다. 이전 기수의
    # 명단은 보관하지 않으므로 그 기수에 실제 제출한 사람만 보여 준다.
    user_ids = set(submitted)
    if is_current_cohort:
        user_ids |= set(destinations)
    members = []
    for user_id in user_ids:
        mine = submitted.get(user_id, set())
        passed = passes.get(user_id, {})
        done = mine & scope
        streak = _streak(sessions, mine)
        last = last_by_user.get(user_id)
        members.append(
            {
                "user_id": user_id,
                "channel": (
                    destinations.get(user_id, "")
                    if is_current_cohort
                    else last["slack_channel"]
                ),
                "done": len(done),
                "total": len(sessions),
                "rate": len(done) / len(sessions) if sessions else 0.0,
                "streak": streak,
                "marks": [
                    (name, name in mine, passed.get(name))
                    for name in sessions[-RECENT_MARKS:]
                ],
                # 진행 중인 회차에 버튼으로 쓴 패스도 함께 보이도록 회차 순으로 둔다.
                "passes": sorted(
                    passed.items(),
                    key=lambda item: session_order.get(item[0], len(names)),
                ),
                "current_done": bool(current_name and current_name in mine),
                "last_session": last["session_name"] if last else "",
                "last_at": last["created_at"] if last else "",
            }
        )

    separate = separate_submitter_ids()
    counted = [member for member in members if member["user_id"] not in separate]
    # 연속 미제출이 긴 사람부터, 같으면 제출률이 낮은 사람부터 보여 준다.
    members.sort(key=lambda member: (-member["streak"], member["rate"], member["user_id"]))
    at_risk = [member for member in counted if member["streak"] >= AT_RISK_MISSES]
    never = [member for member in counted if not submitted.get(member["user_id"])]
    average = (
        round(sum(member["rate"] for member in counted) / len(counted) * 100)
        if counted
        else 0
    )
    return {
        "members": members,
        "counted": len(counted),
        "sessions": sessions,
        "cohort": cohort,
        "cohorts": cohorts,
        "current_cohort": current_cohort,
        "is_current_cohort": is_current_cohort,
        "current_name": current_name,
        "is_active": is_active and is_current_cohort,
        "roster": len(destinations) if is_current_cohort else 0,
        "at_risk": len(at_risk),
        "never": len(never),
        "average": average,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _filter_form(data: dict) -> str:
    options = []
    for cohort in data["cohorts"]:
        selected = " selected" if cohort == data["cohort"] else ""
        label = f"{cohort} (현재)" if cohort == data["current_cohort"] else cohort
        options.append(
            f'<option value="{escape(cohort)}"{selected}>{escape(label)}</option>'
        )
    return (
        '<form class="filters" method="get" action="/members">'
        f'<select name="cohort" aria-label="기수 선택">{"".join(options)}</select>'
        '<button type="submit">조회</button>'
        '</form>'
    )


def _marks_cell(member: dict) -> str:
    if not member["marks"]:
        return '<span class="sub">마감된 회차 없음</span>'
    dots = []
    for name, done, source in member["marks"]:
        if done:
            css, title = "", name
        elif source:
            css, title = f" pass {source}", f"{name} ({PASS_LABELS[source]} 패스)"
        else:
            css, title = " miss", name
        dots.append(f'<i class="dot{css}" title="{escape(title)}"></i>')
    return f'<span class="dots">{"".join(dots)}</span>'


def _pass_cell(member: dict) -> str:
    if not member["passes"]:
        return '<span class="sub">-</span>'
    items = "".join(
        f'<div>{escape(name)} <span class="pill pass-{source}">{PASS_LABELS[source]}</span></div>'
        for name, source in member["passes"]
    )
    return f'{len(member["passes"])} / {MAX_PASS_COUNT}{items}'


def _member_rows(data: dict, directory: dict) -> str:
    items = []
    for member in data["members"]:
        if member["channel"]:
            team = slack_directory.channel_cell(directory, member["channel"])
        else:
            team = '<span class="sub">팀 미배정</span>'

        if not data["is_active"]:
            current = "-"
        elif member["current_done"]:
            current = '<span class="done">제출</span>'
        else:
            current = '<span class="pill pending">미제출</span>'
        current_cell = f"<td>{current}</td>" if data["is_current_cohort"] else ""

        streak = member["streak"]
        if streak >= AT_RISK_MISSES:
            streak_cell = f'<span class="pill failed">{streak}회 연속</span>'
        elif streak:
            streak_cell = f"{streak}회"
        else:
            streak_cell = '<span class="done">-</span>'

        if member["user_id"] in separate_submitter_ids():
            streak_cell = "집계 제외"

        if member["last_at"]:
            last = (
                f'{escape(member["last_session"])}'
                f'<div class="sub">{escape(to_kst(member["last_at"]))}</div>'
            )
        else:
            last = '<span class="sub">기록 없음</span>'

        items.append(
            "<tr>"
            f"<td>{slack_directory.user_cell(directory, member['user_id'])}{separate_submission_badge(member['user_id'])}</td>"
            f"<td>{team}</td>"
            f"{current_cell}"
            f"<td>{member['done']} / {member['total']}"
            f'<div class="sub">{round(member["rate"] * 100)}%</div></td>'
            f"<td>{streak_cell}</td>"
            f"<td>{_marks_cell(member)}</td>"
            f"<td>{_pass_cell(member)}</td>"
            f"<td>{last}</td>"
            "</tr>"
        )
    return rows(
        items,
        8 if data["is_current_cohort"] else 7,
        "선택한 기수에 집계할 멤버가 없습니다.",
    )


@require_admin
async def handle(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(_collect, request.query.get("cohort", "").strip())
    channel_ids = {
        member["channel"] for member in data["members"] if member["channel"]
    }
    directory = await slack_directory.get_directory(request, channel_ids)

    scope = (
        f"{len(data['sessions'])}개 회차 기준"
        if data["sessions"]
        else "마감된 회차가 아직 없습니다"
    )
    at_risk_card = "card alert" if data["at_risk"] else "card"
    never_card = "card alert" if data["never"] else "card"
    never = ""
    if data["is_current_cohort"]:
        basis = f"운영 명단 {data['roster']}명과 해당 기수 제출자를 집계합니다."
        never = (
            f'<div class="{never_card}">{escape(data["cohort"])} 제출 없음'
            f'<div class="number">{data["never"]}명</div>'
            '<small>해당 기수에 한 번도 제출하지 않음</small></div>'
        )
    else:
        basis = "해당 기수에 제출한 멤버만 집계합니다. 팀은 마지막 제출 당시 채널입니다."
    current_heading = "<th>이번 회차</th>" if data["is_current_cohort"] else ""
    body = f"""
{_filter_form(data)}
<p><small>{escape(basis)}</small></p>
<section class="cards">
<div class="card">집계 대상<div class="number">{data['counted']}명</div><small>{escape(data['cohort'])} · {escape(scope)}</small></div>
<div class="card">평균 제출률<div class="number">{data['average']}%</div><small>마감된 회차만 셉니다</small></div>
<div class="{at_risk_card}">이탈 위험<div class="number">{data['at_risk']}명</div><small>{AT_RISK_MISSES}회 이상 연속 미제출</small></div>
{never}
</section>
<h2>{escape(data['cohort'])} 멤버별 참여 이력</h2>
<small>연속 미제출이 긴 순서입니다. 점은 최근 {RECENT_MARKS}개 회차이고, 왼쪽이 오래된 회차입니다. 점에 마우스를 올리면 회차 이름이 보입니다. 주황 점은 직접 쓴 패스, 회색 테두리 점은 미제출로 자동 처리된 패스입니다.</small>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr>
<th>멤버</th><th>팀</th>{current_heading}<th>제출</th><th>연속 미제출</th><th>최근 회차</th><th>패스</th><th>마지막 제출</th>
</tr></thead><tbody>{_member_rows(data, directory)}</tbody></table></div>
"""
    return web.Response(
        text=layout.render(
            title="시공삶 관리자",
            active="/members",
            heading="멤버",
            subtitle=f"기준 {escape(data['generated_at'])} (KST)",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )
