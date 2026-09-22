"""회차 일정: 마감일과 남은 회차. 일정이 소진되기 전에 알아채기 위한 화면."""

import asyncio
import datetime
import re
from html import escape
from urllib.parse import urlencode

from aiohttp import web
from loguru import logger

from dashboard import layout
from dashboard.announcement import PLACEHOLDER_HELP, announcement_dialog, state
from dashboard.auth import csrf_token, require_admin
from dashboard.common import KST, rows, separate_submission_count
from database.sessions import (
    MAX_ANNOUNCEMENT_LENGTH,
    MAX_POSTPONE_WEEKS,
    ScheduleError,
    add_session,
    get_template,
    list_sessions,
    postpone_from,
    rename_session,
    set_resting,
    withdraw_rest_week,
    update_due_at,
)
from utils import format_remaining_time, get_current_session_info, tz_now

LOW_REMAINING = 2
COHORT = re.compile(r"^(\d+기)")
# 회차는 매주 한 번이라 마감 간격이 두 주에 가까우면 회고 없이 쉬어간 주다.
# 마감 시각을 조금 옮겨 둔 회차도 같이 잡히도록 하루 여유를 둔다.
WEEK = 7
REST_MIN_DAYS = WEEK * 2 - 1
POSTPONE_CHOICES = (1, 2, 3, 4)


def _cohort(name: str) -> str:
    matched = COHORT.match(name)
    return matched.group(1) if matched else "초기"


def _rest_weeks(previous: dict | None, row: dict) -> int:
    """앞 회차와의 간격에서 회고 없이 쉬어가는 주가 몇 주인지 센다.

    기수가 바뀌는 자리는 원래 몇 주씩 비므로 쉬어가는 주로 보지 않는다.
    """
    if previous is None or _cohort(previous["name"]) != _cohort(row["name"]):
        return 0
    gap = (row["due_at"] - previous["due_at"]).days
    if gap < REST_MIN_DAYS:
        return 0
    return max(round(gap / WEEK) - 1, 1)


def _collect(show_all: bool) -> dict:
    _, current_name, remaining, is_active = get_current_session_info()
    now = tz_now()

    schedule = list_sessions()
    # 쉬어가는 주는 회차가 아니다. 남은 회차 수나 마지막 마감처럼 "언제까지
    # 일정이 차 있나"를 보는 값에서는 빼고 센다. 표에는 그대로 보여 준다.
    working = [row for row in schedule if not row["resting"]] or schedule
    names = [row["name"] for row in working]
    dues = [row["due_at"] for row in working]

    current_cohort = _cohort(current_name) if current_name else _cohort(names[-1])
    items = []
    movable = []
    upcoming = 0
    previous = None
    for row in schedule:
        name, due = row["name"], row["due_at"]
        # 쉬어가는 주는 걸러 내기 전에 센다. 기수만 보는 중이어도 앞뒤 회차
        # 간격은 전체 일정에서 나온다.
        rest_weeks = _rest_weeks(previous, row)
        previous = row
        if due > now and not row["resting"]:
            upcoming += 1
            movable.append(row)
        if not show_all and _cohort(name) != current_cohort:
            continue
        items.append(
            {
                **row,
                "name": name,
                "due": due,
                "past": due <= now,
                "current": name == current_name and is_active,
                "submitters": row["submissions"],
                # 마감이 지난 회차의 마감 시각을 옮기면 그 구간에 제출된 회고가
                # 다른 회차에 속한 것처럼 집계되므로 예정 회차만 고칠 수 있다.
                "editable": due > now,
                # 기록이 하나라도 붙으면 회차 이름을 바꿀 수 없다. 여기서는
                # 제출 건수만 보고, 출석 같은 다른 기록은 저장할 때 걸러진다.
                "changeable": (
                    due > now
                    and row["announced_at"] is None
                    and row["submissions"] == 0
                ),
                "announcement_state": state(row, now)[0],
                "rest_weeks": rest_weeks,
            }
        )
    return {
        "items": items,
        "movable": movable,
        "template": get_template(),
        "now": now,
        "upcoming": upcoming,
        "current_name": current_name or "진행 중인 회차 없음",
        "current_cohort": current_cohort,
        "remaining": format_remaining_time(remaining) if is_active else "-",
        "is_active": is_active,
        "last_due": dues[-1],
        "last_name": names[-1],
        "show_all": show_all,
    }


def _due_form(token: str, row: dict) -> str:
    value = row["due"].strftime("%Y-%m-%dT%H:%M")
    return (
        '<div class="field"><small>마감 변경</small>'
        '<form method="post" class="inline" action="/schedule/due">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{escape(row["name"])}">'
        f'<input aria-label="마감 시각 (KST)" type="datetime-local" name="due_at" value="{value}" required>'
        '<button type="submit">변경</button></form></div>'
    )


def _rename_form(token: str, name: str) -> str:
    return (
        '<div class="field"><small>이름 변경</small>'
        '<form method="post" class="inline" action="/schedule/rename">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{name}">'
        f'<input aria-label="새 회차 이름" type="text" name="new_name" value="{name}" required>'
        '<button type="submit">변경</button></form></div>'
    )


def _rest_form(token: str, name: str, row: dict) -> str:
    return (
        '<form method="post" class="inline" action="/schedule/rest">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{name}">'
        f'<input type="hidden" name="resting" value="{0 if row["resting"] else 1}">'
        f'<button type="submit">{"회차로 쓰기" if row["resting"] else "쉬어가기"}</button>'
        "</form>"
    )


def _withdraw_form(token: str, name: str) -> str:
    """쉬어가는 주를 삭제하고 뒤 회차를 당겨 미루기를 되돌린다.

    뒤 회차 전부의 마감이 움직이는 일이라 누르기 전에 한 번 더 알린다. 회차
    이름은 `NAME_PATTERN`을 거쳐 따옴표가 없으므로 확인 문구에 넣어도 안전하다.
    """
    return (
        '<form method="post" class="inline" action="/schedule/withdraw"'
        f' onsubmit="return confirm(\'{name}을(를) 삭제하면 뒤 회차가 한 주씩 당겨집니다. 삭제할까요?\')">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<input type="hidden" name="name" value="{name}">'
        '<button type="submit" class="danger">삭제하기</button></form>'
    )


def _edit_dialog(request, row: dict, dialog_id: str, announce_id: str) -> str:
    """회차 하나를 고치는 모달. 표에는 이 모달을 여는 `편집` 버튼만 둔다.

    예정 회차는 마감을, 아직 기록이 붙지 않은 회차는 이름과 쉬어가기를,
    쉬어가는 주는 삭제까지 고칠 수 있다. 못 고치는 칸은 왜 못 고치는지 적는다.
    """
    token = csrf_token(request)
    name = escape(row["name"])
    parts = [_due_form(token, row)]
    if row["changeable"]:
        parts.append(_rename_form(token, name))
        actions = _rest_form(token, name, row)
        if row["resting"]:
            actions += _withdraw_form(token, name)
        parts.append(
            f'<div class="field"><small>쉬어가기</small><div class="actions">{actions}</div></div>'
        )
    else:
        parts.append(
            '<p><small>제출이나 공지가 이미 있어 이름과 쉬어가기는 바꿀 수 없습니다.'
            " 회차 이름은 회고 기록에 그대로 박히는 값입니다.</small></p>"
        )
    parts.append(
        f'<div class="field"><small>제출 공지</small>{row["announcement_state"]}'
        f' <button type="button" data-dialog="{announce_id}">문구·공지 시각 편집</button></div>'
    )
    due = f"{row['due'].strftime('%Y-%m-%d %H:%M')} ({'월화수목금토일'[row['due'].weekday()]})"
    return (
        f'<dialog id="{dialog_id}" data-session="{name}" aria-labelledby="{dialog_id}-title">'
        f'<div class="dialog-head"><h2 id="{dialog_id}-title">{name}</h2>'
        '<form method="dialog"><button aria-label="닫기">✕</button></form></div>'
        f"<small>마감 {due}</small>"
        f'{"".join(parts)}</dialog>'
    )


def _rest_row(weeks: int) -> str:
    return (
        '<tr class="rest"><td colspan="6">'
        f'쉬어가는 주 · {weeks}주 · 회고 없음'
        "</td></tr>"
    )


def _postpone_form(request, data: dict) -> str:
    """연휴로 쉬어갈 때 고른 회차부터 뒤쪽을 통째로 미루는 폼."""
    if not data["movable"]:
        return "<small>미룰 예정 회차가 없습니다.</small>"
    options = "".join(
        f'<option value="{escape(row["name"])}">'
        f'{escape(row["name"])} · {row["due_at"].strftime("%m-%d")}'
        f'({"월화수목금토일"[row["due_at"].weekday()]}) 마감</option>'
        for row in data["movable"]
    )
    weeks = "".join(
        f'<option value="{count}">{count}주</option>' for count in POSTPONE_CHOICES
    )
    return (
        '<form method="post" action="/schedule/postpone" class="filters">'
        f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">'
        f'<select aria-label="미룰 첫 회차" name="name">{options}</select>'
        f'<select aria-label="미룰 주 수" name="weeks">{weeks}</select>'
        '<button type="submit" class="primary">이 회차부터 미루기</button></form>'
    )


def _schedule_rows(request, data: dict) -> tuple[str, str]:
    """(표 행, 편집 모달)을 돌려준다.

    모달은 표 밖에 둔다. 표 칸 안에 두면 좁은 칸 폭과 가로 스크롤 설정을 물려받는다.
    """
    items = []
    dialogs = []
    for index, row in enumerate(data["items"]):
        if row["rest_weeks"]:
            items.append(_rest_row(row["rest_weeks"]))
        if row["resting"]:
            status = '<span class="pill">쉬어가는 주</span>'
        elif row["current"]:
            status = '<span class="pill now">진행 중</span>'
        elif row["past"]:
            status = '<span class="pill">마감</span>'
        else:
            status = '<span class="pill pending">예정</span>'
        edit = ""
        notice_cell = row["announcement_state"]
        # 마감이 지난 회차는 고칠 게 없어 버튼을 두지 않는다.
        if row["editable"]:
            dialog_id = f"edit-{index}"
            announce_id = f"announce-{index}"
            dialogs.append(_edit_dialog(request, row, dialog_id, announce_id))
            dialogs.append(
                announcement_dialog(
                    request, row, announce_id, template=data["template"], now=data["now"]
                )
            )
            edit = f'<button type="button" data-dialog="{dialog_id}">편집</button>'
            # 공지 상태를 누르면 바로 공지 모달이 열린다.
            notice_cell = (
                f'<button type="button" class="link" data-dialog="{announce_id}"'
                f' aria-label="{escape(row["name"])} 제출 공지 편집">{notice_cell}</button>'
            )
        items.append(
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{row['due'].strftime('%Y-%m-%d %H:%M')} ({'월화수목금토일'[row['due'].weekday()]})</td>"
            f"<td>{status}</td>"
            f"<td>{row['submitters']}명{separate_submission_count(row['separate_submissions'])}</td>"
            f"<td>{notice_cell}</td>"
            f"<td>{edit}</td>"
            "</tr>"
        )
    return rows(items, 6, "표시할 회차가 없습니다."), "".join(dialogs)


@require_admin
async def handle(request: web.Request) -> web.Response:
    show_all = request.query.get("all") == "1"
    data = await asyncio.to_thread(_collect, show_all)
    table_rows, edit_dialogs = _schedule_rows(request, data)

    warning = ""
    if data["upcoming"] == 0:
        warning = (
            '<div class="warn"><b>남은 회차가 없습니다.</b> '
            f'마지막 회차는 {escape(data["last_name"])}'
            f'({data["last_due"].strftime("%Y-%m-%d")})였습니다. '
            "`회차 추가` 버튼으로 다음 회차를 추가해야 새 회차로 넘어갑니다.</div>"
        )
    elif data["upcoming"] <= LOW_REMAINING:
        warning = (
            f'<div class="warn">남은 회차가 {data["upcoming"]}개뿐입니다. '
            f'{data["last_due"].strftime("%Y-%m-%d")} 이후 일정을 미리 채워 두세요.</div>'
        )

    notice = ""
    if request.query.get("added"):
        notice = f'<div class="warn">`{escape(request.query["added"])}` 회차를 추가했습니다.</div>'
    elif request.query.get("updated"):
        notice = f'<div class="warn">`{escape(request.query["updated"])}` 마감 시각을 바꿨습니다.</div>'
    elif request.query.get("postponed"):
        moved = request.query.get("moved", "")
        stale = (
            " 이미 나간 공지에는 예전 마감이 적혀 있으니 채널에 따로 알려 주세요."
            if request.query.get("announced")
            else ""
        )
        filled = request.query.get("filled", "")
        made = (
            f" 비는 주는 `{escape(filled)}`(으)로 세워 뒀습니다. "
            "공지는 꺼져 있으니, 쉬어갈 주면 지우고 쓸 주면 이름과 공지를 정하세요."
            if filled
            else ""
        )
        notice = (
            f'<div class="warn">`{escape(request.query["postponed"])}`부터 '
            f'{escape(moved)}개 회차를 {escape(request.query.get("weeks", ""))}주씩 '
            f"미뤘습니다.{made}{stale}</div>"
        )
    elif request.query.get("renamed"):
        notice = (
            f'<div class="warn">`{escape(request.query["renamed"])}` 회차 이름을 '
            f'`{escape(request.query.get("to", ""))}`(으)로 바꿨습니다.</div>'
        )
    elif request.query.get("withdrawn"):
        pulled = request.query.get("pulled", "0")
        notice = (
            f'<div class="warn">`{escape(request.query["withdrawn"])}`을(를) 삭제하고 '
            f"뒤 회차 {escape(pulled)}개를 한 주씩 당겼습니다.</div>"
        )
    elif request.query.get("resting"):
        notice = (
            f'<div class="warn">`{escape(request.query["resting"])}` 주는 쉬어갑니다. '
            "그 주에는 마감도 공지도 없습니다.</div>"
        )
    elif request.query.get("working"):
        notice = (
            f'<div class="warn">`{escape(request.query["working"])}`을(를) 회차로 씁니다. '
            "공지를 보내려면 그 회차의 `문구`에서 공지 시각을 정하세요.</div>"
        )
    elif request.query.get("announcement"):
        target = escape(request.query["announcement"])
        result = request.query.get("result")
        message = {
            "saved": f"`{target}` 공지 문구를 저장했습니다.",
            "reset": f"`{target}` 공지가 기본 문구를 쓰도록 되돌렸습니다.",
            "scheduled": f"`{target}` 공지 시각을 {escape(request.query.get('at', ''))}로 바꿨습니다.",
            "off": f"`{target}`은(는) 공지를 보내지 않습니다.",
        }.get(result)
        if message:
            notice = f'<div class="warn">{message}</div>'
    elif request.query.get("template"):
        notice = '<div class="warn">제출 공지 기본 문구를 저장했습니다.</div>'
    elif request.query.get("error"):
        notice = f'<div class="warn">{escape(request.query["error"])}</div>'

    toggle = (
        '<a href="/schedule">현재 기수만 보기</a>'
        if show_all
        else '<a href="/schedule?all=1">전체 회차 보기</a>'
    )
    body = f"""
<section class="cards">
<div class="card">현재 회차<div class="number">{escape(data['current_name'])}</div><small>{'진행 중' if data['is_active'] else '마감'} · 마감까지 {data['remaining']}</small></div>
<div class="{'card alert' if data['upcoming'] <= LOW_REMAINING else 'card'}">남은 회차<div class="number">{data['upcoming']}개</div><small>마지막 마감 {data['last_due'].strftime('%Y-%m-%d')}</small></div>
</section>
{warning}
{notice}
<div class="toolbar">
<button type="button" class="primary" data-dialog="postpone-dialog">회차 미루기</button>
<button type="button" data-dialog="add-dialog">회차 추가</button>
<button type="button" data-dialog="template-dialog">공지 기본 문구</button>
</div>
<dialog id="postpone-dialog" aria-labelledby="postpone-title">
<div class="dialog-head"><h2 id="postpone-title">회차 미루기</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<small>고른 회차부터 마지막 회차까지 N주씩 한꺼번에 밉니다. 회차 이름과 순서는 그대로이고, 아직 나가지 않은 공지도 같이 밀립니다.
비는 주에는 `추가 회차`가 `쉬어가는 주`로 생깁니다. 그 주에도 회고를 받으려면 그 회차 `편집`에서 `회차로 쓰기`를, 되돌리려면 `삭제하기`를 누르세요.
한 번에 최대 {MAX_POSTPONE_WEEKS}주까지 미룰 수 있습니다.</small>
{_postpone_form(request, data)}
</dialog>
<dialog id="add-dialog" aria-labelledby="add-title">
<div class="dialog-head"><h2 id="add-title">회차 추가</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<small>마지막 회차({escape(data['last_name'])}, {data['last_due'].strftime('%Y-%m-%d %H:%M')}) 뒤에만 붙일 수 있습니다.</small>
<form method="post" action="/schedule/add" class="filters">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<input aria-label="새 회차 이름" type="text" name="name" placeholder="7기 1회차" required>
<input aria-label="마감 시각 (KST)" type="datetime-local" name="due_at" required>
<button type="submit" class="primary">추가</button></form>
</dialog>
<dialog id="template-dialog" aria-labelledby="template-title">
<div class="dialog-head"><h2 id="template-title">제출 공지 기본 문구</h2><form method="dialog"><button aria-label="닫기">✕</button></form></div>
<small>공지 시각이 되면 이 문구가 회차마다 나갑니다. {PLACEHOLDER_HELP}
특정 회차만 다르게 쓰려면 회차 목록의 공지 칸을 눌러 그 회차만 덮어씁니다.</small>
<form method="post" action="/schedule/announcement/template">
<input type="hidden" name="csrf_token" value="{csrf_token(request)}">
<textarea aria-label="공지 문구" name="body" rows="8" maxlength="{MAX_ANNOUNCEMENT_LENGTH}" required>{escape(data['template'])}</textarea>
<div class="filters"><button type="submit" class="primary">기본 문구 저장</button></div></form>
</dialog>
<h2>회차 목록</h2>
<div class="filters"><small>{escape(data['current_cohort'])} 일정</small>{toggle}</div>
<p class="table-hint">표를 좌우로 밀어 모든 항목을 확인하세요.</p><div class="table-scroll" role="region" aria-label="목록 표" tabindex="0"><table><thead><tr><th>회차</th><th>마감 (KST)</th><th>상태</th><th>제출자</th><th>제출 공지</th><th></th></tr></thead>
<tbody>{table_rows}</tbody></table></div>
{edit_dialogs}
<small>`편집`을 누르면 그 회차의 마감·이름·쉬어가기·공지를 고칠 수 있습니다. 마감이 지난 회차는 그 구간에 제출된 회고가 다른 회차로 집계되지 않도록 고칠 수 없습니다.
`쉬어가는 주`는 회차 판정과 공지에서 빠져 그 주에는 마감이 없습니다.</small>
<script>
(() => {{
 // 버튼이 가리키는 모달을 연다. 편집 버튼은 표 뒤에 그려지므로 버튼마다 걸지 않고
 // 문서에서 한 번에 받는다. 바깥 어두운 곳을 누르면 닫는다.
 document.addEventListener('click', (event) => {{
  const opener = event.target.closest('[data-dialog]');
  if (opener) {{
   const current = opener.closest('dialog');
   if (current) current.close();
   document.getElementById(opener.dataset.dialog).showModal();
   return;
  }}
  if (event.target.tagName === 'DIALOG') event.target.close();
 }});
}})();
</script>
"""
    return web.Response(
        text=layout.render(
            title="회차 일정",
            active="/schedule",
            heading="회차 일정",
            subtitle="마감일과 남은 회차를 확인합니다.",
            body=body,
            request=request,
        ),
        content_type="text/html",
    )


def _parse_due(raw: str) -> datetime.datetime:
    """datetime-local 입력을 KST 기준 시각으로 읽는다."""
    try:
        parsed = datetime.datetime.fromisoformat((raw or "").strip())
    except ValueError:
        raise ScheduleError("마감 시각 형식이 올바르지 않습니다.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _redirect(**query) -> web.HTTPFound:
    return web.HTTPFound("/schedule?" + urlencode(query))


@require_admin
async def handle_add(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        due_at = _parse_due(str(form.get("due_at", "")))
        await asyncio.to_thread(add_session, name, due_at)
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 회차를 추가합니다 - name={} due_at={}", name, due_at)
    raise _redirect(added=name)


def _parse_weeks(raw: str) -> int:
    try:
        return int((raw or "").strip())
    except ValueError:
        raise ScheduleError("미룰 주 수를 고르세요.") from None


@require_admin
async def handle_withdraw(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        pulled = await asyncio.to_thread(withdraw_rest_week, name, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info(
        "관리자 웹에서 쉬어가는 주를 삭제하고 당깁니다 - name={} pulled={}", name, len(pulled)
    )
    raise _redirect(withdrawn=name, pulled=len(pulled))


@require_admin
async def handle_postpone(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        weeks = _parse_weeks(str(form.get("weeks", "")))
        moved = await asyncio.to_thread(postpone_from, name, weeks, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info(
        "관리자 웹에서 회차를 미룹니다 - name={} weeks={} moved={}",
        name,
        weeks,
        len(moved["names"]),
    )
    extra = {"announced": "1"} if moved["announced"] else {}
    if moved["filled"]:
        extra["filled"] = ", ".join(moved["filled"])
    raise _redirect(
        postponed=name, weeks=weeks, moved=len(moved["names"]), **extra
    )


@require_admin
async def handle_rename(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    new_name = str(form.get("new_name", "")).strip()
    try:
        saved = await asyncio.to_thread(rename_session, name, new_name, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 회차 이름을 바꿉니다 - name={} new_name={}", name, saved)
    raise _redirect(renamed=name, to=saved)


@require_admin
async def handle_rest(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    resting = str(form.get("resting", "")) == "1"
    try:
        await asyncio.to_thread(set_resting, name, resting, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 쉬어가기를 바꿉니다 - name={} resting={}", name, resting)
    raise _redirect(**({"resting": name} if resting else {"working": name}))


@require_admin
async def handle_update_due(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    name = str(form.get("name", "")).strip()
    try:
        due_at = _parse_due(str(form.get("due_at", "")))
        await asyncio.to_thread(update_due_at, name, due_at, now=tz_now())
    except ScheduleError as error:
        raise _redirect(error=str(error))
    logger.info("관리자 웹에서 마감을 바꿉니다 - name={} due_at={}", name, due_at)
    raise _redirect(updated=name)
