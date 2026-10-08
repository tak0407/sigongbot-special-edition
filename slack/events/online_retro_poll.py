"""온라인 회고 시간 투표 UI와 처리기.

공지 채널에 모임 날짜마다 메시지 하나를 올리고, 누른 사람의 팀 투표에 기록한다.
팀별 결과를 따로 확정하므로 투표 행은 팀마다 하나씩 두고 같은 메시지를 가리킨다.
"""

import datetime
from typing import NamedTuple

from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database.online_retro_poll import (
    create_poll,
    find_poll,
    get_poll,
    get_vote,
    mark_poll_posted,
    message_polls,
    save_vote,
)

# 일요일에만 모이므로 오후부터 고르게 한다. Slack 체크박스는 10개까지라
# 1시간 단위로는 13시가 가장 이른 시작이다.
TIME_SLOTS = [f"{hour:02d}:00~{hour + 1:02d}:00" for hour in range(13, 23)]
# 투표 기록에 저장되는 값이라 바꾸지 않는다. 화면에는 UNAVAILABLE_LABEL로 보인다.
UNAVAILABLE = "이번 회차 참여 어려움"
UNAVAILABLE_LABEL = "이번엔 어려워요"
ALL_OPTIONS = TIME_SLOTS + [UNAVAILABLE]
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
NO_TEAM = "팀 배정이 없어 투표할 수 없어요. 운영자에게 알려 주세요."
NOT_ON_TEAM = "자신이 배정된 팀의 시간 투표에만 참여할 수 있어요."
DEFAULT_POLL_INTRO = (
    "*{날짜} 온라인 회고, 언제 모일까요?* 💻\n"
    f"팀별로 Google Meet에서 모여 {settings.ONLINE_RETRO_WRITING_MINUTES}분 동안 "
    "이번 주 회고를 쓰고, 돌아가며 나눠요.\n"
    "참여할 수 있는 시간을 모두 골라 주세요. "
    "팀마다 가장 많이 고른 시간으로 정해서 이 채널에 알려 드릴게요."
)
MAX_POLL_INTRO_LENGTH = 1000


class PollPost(NamedTuple):
    added: list[str]  # 이번에 공지에 올라간 팀
    new_message: bool  # 새 메시지를 올렸는지, 이미 있던 공지에 붙였는지


def meeting_day(meeting_date: str) -> str:
    """'2026-10-11' → '10월 11일(일)'."""
    day = datetime.date.fromisoformat(meeting_date)
    return f"{day.month}월 {day.day}일({WEEKDAYS[day.weekday()]})"


def render_poll_intro(template: str | None, *, meeting_date: str) -> str:
    """`{날짜}`를 모임 날짜로 바꾼다. 남은 중괄호에 걸리지 않게 format은 쓰지 않는다."""
    return (template or DEFAULT_POLL_INTRO).replace("{날짜}", meeting_day(meeting_date))


def _can_vote(poll: dict, user_id: str) -> bool:
    return bool(poll["is_test"]) or (
        settings.SUBMISSION_DESTINATIONS.get(user_id) == poll["team_channel"]
    )


def _message_channel(poll: dict) -> str:
    """투표 메시지가 있는 채널. 예전 투표는 팀 채널에 올라가 비어 있다."""
    return poll.get("message_channel") or poll["team_channel"]


def _team_size(team_channel: str) -> int:
    return sum(
        1 for channel in settings.SUBMISSION_DESTINATIONS.values() if channel == team_channel
    )


def poll_actions(poll_id: int) -> dict:
    """투표 버튼. 공지와 미응답 DM에 같이 달고, 누른 사람의 팀 투표로 기록된다."""
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "open_online_retro_time_poll",
                "text": {"type": "plain_text", "text": "가능한 시간 고르기"},
                "style": "primary",
                "value": str(poll_id),
            },
            {
                "type": "button",
                "action_id": "mark_online_retro_unavailable",
                "text": {"type": "plain_text", "text": UNAVAILABLE_LABEL},
                "value": str(poll_id),
            },
        ],
    }


def build_poll_blocks(polls: list[dict]) -> list[dict]:
    """공지 하나에 묶인 팀 투표들. 시간은 팀마다 따로 정하므로 응답 현황만 보여 준다."""
    first = polls[0]
    prefix = "*[테스트]* 🧪\n\n" if first["is_test"] else ""

    def progress(poll: dict) -> str:
        size = _team_size(poll["team_channel"])
        # 테스트 채널처럼 배정된 팀원이 없는 투표는 응답 수만 보인다.
        return (
            f"{poll['team_name']} {poll['voters']}/{size}"
            if size
            else f"{poll['team_name']} {poll['voters']}명"
        )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": prefix
                + render_poll_intro(
                    first.get("intro_template"), meeting_date=first["meeting_date"]
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*응답 현황*  " + " · ".join(progress(poll) for poll in polls),
            },
        },
        poll_actions(first["id"]),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "고른 시간은 자기 팀 투표로 기록돼요. 다시 고르면 이전 응답이 바뀝니다.",
                }
            ],
        },
    ]


def build_poll_modal(poll: dict, previous: list[str]) -> dict:
    """가능한 시간을 체크박스로 한눈에 보여 주고, 이전 선택을 미리 체크한다."""
    options = [
        {"text": {"type": "plain_text", "text": slot}, "value": slot} for slot in TIME_SLOTS
    ]
    element = {
        "type": "checkboxes",
        "action_id": "available_slots_input",
        "options": options,
    }
    checked = [option for option in options if option["value"] in previous]
    if checked:
        element["initial_options"] = checked
    return {
        "type": "modal",
        "callback_id": "online_retro_time_poll_submit",
        "title": {"type": "plain_text", "text": "온라인 회고 시간 투표"},
        "submit": {"type": "plain_text", "text": "투표하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": str(poll["id"]),
        "blocks": [
            {
                "type": "input",
                "block_id": "available_slots",
                "label": {
                    "type": "plain_text",
                    "text": (
                        f"{poll['team_name']} · {meeting_day(poll['meeting_date'])} "
                        "참여할 수 있는 시간을 모두 골라 주세요"
                    ),
                },
                "element": element,
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"참여가 어렵다면 투표 메시지의 `{UNAVAILABLE_LABEL}`를 눌러 주세요.",
                    }
                ],
            },
        ],
    }


async def _shared_polls(poll: dict) -> list[dict]:
    """이 투표와 같은 메시지에 묶인 팀 투표들."""
    return [
        item
        for item in await message_polls(
            meeting_date=poll["meeting_date"], is_test=bool(poll["is_test"])
        )
        if item["slack_ts"] == poll["slack_ts"]
    ]


async def _update_poll_message(client: AsyncWebClient, poll: dict) -> None:
    if not poll["slack_ts"]:
        return
    await client.chat_update(
        channel=_message_channel(poll),
        ts=poll["slack_ts"],
        text="온라인 회고 시간 투표",
        blocks=build_poll_blocks(await _shared_polls(poll)),
    )


async def post_time_poll(
    client: AsyncWebClient,
    *,
    teams: list[tuple[str, str]],
    meeting_date: datetime.date,
    session_name: str,
    message_channel: str,
    is_test: bool = False,
    intro_template: str | None = None,
) -> PollPost:
    """팀 투표를 DB에 먼저 만들고 공지 하나로 올린다.

    `teams`는 (팀 채널, 팀 이름) 목록이다. 그날 공지가 이미 있으면 새로 올리지
    않고, 빠진 팀만 그 공지에 붙여 메시지를 고친다.
    """
    date_text = meeting_date.isoformat()
    polls = [
        await create_poll(
            meeting_date=date_text,
            session_name=session_name,
            team_channel=channel,
            team_name=name,
            slots=ALL_OPTIONS,
            is_test=is_test,
            intro_template=intro_template,
        )
        for channel, name in teams
    ]
    pending = [poll for poll in polls if not poll["slack_ts"]]
    if not pending:
        return PollPost(added=[], new_message=False)
    posted = next((poll for poll in polls if poll["slack_ts"]), None)
    if posted:
        ts, channel = posted["slack_ts"], _message_channel(posted)
    else:
        ids = {poll["id"] for poll in polls}
        shown = [
            poll
            for poll in await message_polls(meeting_date=date_text, is_test=is_test)
            if poll["id"] in ids
        ]
        response = await client.chat_postMessage(
            channel=message_channel,
            text="온라인 회고 시간 투표",
            blocks=build_poll_blocks(shown),
        )
        ts, channel = response["ts"], message_channel
    for poll in pending:
        await mark_poll_posted(poll["id"], ts, channel)
    if posted:
        await _update_poll_message(client, posted)
    return PollPost(added=[poll["team_name"] for poll in pending], new_message=posted is None)


async def _own_poll(anchor: dict, user_id: str) -> dict | None:
    """공지에서 누른 사람의 팀 투표.

    같은 공지에 붙은 팀 투표만 받는다. 아직 공지에 없는 팀이면 운영자가 다시
    올려야 한다. 테스트 공지는 팀이 없는 사람도 첫 팀 투표로 눌러 볼 수 있다.
    """
    team_channel = settings.SUBMISSION_DESTINATIONS.get(user_id)
    if team_channel:
        poll = await find_poll(
            meeting_date=anchor["meeting_date"],
            team_channel=team_channel,
            is_test=bool(anchor["is_test"]),
        )
        if poll and poll["slack_ts"] == anchor["slack_ts"]:
            return poll
    return anchor if anchor["is_test"] else None


def build_notice_view(text: str) -> dict:
    """투표 결과와 안내를 보여 주는 창.

    채널에 남기는 '나에게만 보이는 메시지'는 봇이 나중에 지우거나 고칠 수 없어
    여러 번 누르면 쌓인다. 창은 닫으면 사라지므로 안내는 모두 창으로 띄운다.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "온라인 회고 시간 투표"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


async def handle_open_time_poll(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    anchor = await get_poll(int(body["actions"][0]["value"]))
    if anchor is None:
        raise ValueError("시간 투표를 찾을 수 없어요.")
    user_id = body["user"]["id"]
    poll = await _own_poll(anchor, user_id)
    if poll is None:
        await client.views_open(trigger_id=body["trigger_id"], view=build_notice_view(NO_TEAM))
        return
    previous = await get_vote(poll["id"], user_id)
    await client.views_open(
        trigger_id=body["trigger_id"], view=build_poll_modal(poll, previous)
    )


async def handle_mark_unavailable(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    """시간 고르는 창 없이 한 번에 '이번엔 어려워요'로 응답한다. 이전 선택은 덮어쓴다."""
    await ack()
    anchor = await get_poll(int(body["actions"][0]["value"]))
    if anchor is None:
        raise ValueError("시간 투표를 찾을 수 없어요.")
    user_id = body["user"]["id"]
    poll = await _own_poll(anchor, user_id)
    if poll is None:
        await client.views_open(trigger_id=body["trigger_id"], view=build_notice_view(NO_TEAM))
        return
    await save_vote(poll_id=poll["id"], user_id=user_id, slots=[UNAVAILABLE])
    # trigger_id는 3초 안에만 쓸 수 있어 메시지를 고치기 전에 창부터 띄운다.
    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_notice_view(
            f"*{poll['team_name']} 투표에 이번엔 어렵다고 남겼어요*\n"
            "시간이 되면 투표 메시지의 `가능한 시간 고르기`로 바꿀 수 있어요."
        ),
    )
    await _update_poll_message(client, poll)


async def handle_time_poll_submit(
    ack: AsyncAck, body: dict, client: AsyncWebClient, view: dict
) -> None:
    poll = await get_poll(int(view["private_metadata"]))
    if poll is None:
        raise ValueError("시간 투표를 찾을 수 없어요.")
    selected = view["state"]["values"]["available_slots"][
        "available_slots_input"
    ].get("selected_options", [])
    slots = [option["value"] for option in selected if option["value"] in TIME_SLOTS]
    if not slots:
        await ack(
            response_action="errors",
            errors={
                "available_slots": (
                    "가능한 시간을 하나 이상 골라 주세요. "
                    f"참여가 어렵다면 `{UNAVAILABLE_LABEL}`를 눌러 주세요."
                )
            },
        )
        return
    user_id = body["user"]["id"]
    if not _can_vote(poll, user_id):
        raise ValueError(NOT_ON_TEAM)
    await save_vote(poll_id=poll["id"], user_id=user_id, slots=slots)
    # 같은 창을 저장 결과 화면으로 바꾼다. 채널에는 아무것도 남기지 않는다.
    await ack(
        response_action="update",
        view=build_notice_view(
            f"*{poll['team_name']} 투표에 저장했어요* ✅\n{', '.join(slots)}\n\n"
            "바꾸려면 투표 메시지의 `가능한 시간 고르기`를 다시 누르세요."
        ),
    )
    await _update_poll_message(client, poll)
