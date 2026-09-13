"""진행 중 회고: 질문형 회고 플로우가 어디서 멈췄는지 본다.

플로우는 완료되면 삭제되므로, 여기 남아 있는 행은 아직 끝내지 않은 것이다.
몇 번째 질문에서 멈췄는지 알 수 있어 미제출자 명단보다 이탈 지점이 정확하다.
"""

import asyncio
import json
from html import escape

from aiohttp import web

from dashboard import directory as slack_directory
from dashboard import layout
from dashboard.auth import require_admin
from dashboard.common import rows, since, to_kst, to_kst_datetime, truncate
from database.sqlite import get_connection
from utils import tz_now

STALE_HOURS = 24
ANSWER_PREVIEW = 60


def _collect() -> dict:
    with get_connection() as connection:
        found = connection.execute(
            """
            SELECT flow_id, user_id, slack_channel, session_name, questions_json,
                   answers_json, formatted_json, current_index, created_at, updated_at
              FROM guided_reflections
             ORDER BY updated_at DESC
            """
        ).fetchall()

    now = tz_now()
    items = []
    stale = 0
    for row in found:
        record = dict(row)
        try:
            questions = json.loads(record["questions_json"])
            answers = json.loads(record["answers_json"])
        except (TypeError, ValueError):
            questions, answers = [], []
        updated = to_kst_datetime(record["updated_at"])
        idle_hours = (now - updated).total_seconds() / 3600 if updated else 0
        is_stale = idle_hours >= STALE_HOURS
        if is_stale:
            stale += 1
        last_answer = ""
        if answers:
            latest = answers[-1]
            last_answer = latest.get("answer", "") if isinstance(latest, dict) else str(latest)
        items.append(
            {
                **record,
                "total": len(questions),
                "answered": len(answers),
                "position": min(record["current_index"] + 1, max(len(questions), 1)),
                "formatted": bool(record["formatted_json"]),
                "stale": is_stale,
                "last_answer": last_answer,
            }
        )
    return {"items": items, "stale": stale}


def _item_rows(data: dict, directory: dict) -> str:
    items = []
    for row in data["items"]:
        total = row["total"] or 0
        width = round(row["answered"] / total * 100) if total else 0
        flag = ' <span class="pill failed">정체</span>' if row["stale"] else ""
        items.append(
            "<tr>"
            f"<td>{slack_directory.user_cell(directory, row['user_id'])}</td>"
            f"<td>{escape(row['session_name'])}</td>"
            f"<td>{slack_directory.channel_cell(directory, row['slack_channel'])}</td>"
            f"<td>{row['position']} / {total or '?'}"
            f'<div class="bar"><span style="width:{width}%"></span></div></td>'
            f"<td>{escape(truncate(row['last_answer'], ANSWER_PREVIEW))}</td>"
            f"<td>{escape(to_kst(row['updated_at']))}<div class=\"sub\">{escape(since(row['updated_at']))}{flag}</div></td>"
            "</tr>"
        )
    return rows(items, 6, "진행 중인 질문형 회고가 없습니다.")


@require_admin
async def handle(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(_collect)
    directory = await slack_directory.get_directory(
        request, {row["slack_channel"] for row in data["items"]}
    )
    warning = ""
    if data["stale"]:
        warning = (
            f'<div class="warn">{data["stale"]}건이 {STALE_HOURS}시간 넘게 멈춰 있습니다. '
            "중간에 창을 닫았을 가능성이 큽니다.</div>"
        )
    body = f"""
<section class="cards">
<div class="card">진행 중<div class="number">{len(data['items'])}건</div><small>아직 끝내지 않은 플로우</small></div>
<div class="{'card alert' if data['stale'] else 'card'}">정체<div class="number">{data['stale']}건</div><small>{STALE_HOURS}시간 이상 갱신 없음</small></div>
</section>
{warning}
<table><thead><tr><th>사용자</th><th>회차</th><th>채널</th><th>진행</th>
<th>마지막 답변</th><th>마지막 갱신 (KST)</th></tr></thead>
<tbody>{_item_rows(data, directory)}</tbody></table>
<small>완료된 플로우는 저장 시 삭제되므로 이 목록에 남지 않습니다.</small>
"""
    return web.Response(
        text=layout.render(
            title="진행 중 회고",
            active="/admin/guided",
            heading="진행 중 회고",
            subtitle="질문형 회고를 시작했지만 아직 끝내지 않은 사람들입니다.",
            body=body,
        ),
        content_type="text/html",
    )
