"""팀별 온라인 회고 시간 투표 UI와 처리기."""

import datetime

from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database.online_retro_poll import (
    create_poll,
    get_poll,
    get_vote,
    mark_poll_posted,
    save_vote,
    vote_counts,
)
from slack.ephemeral import post_ephemeral

TIME_SLOTS = [
    "18:00~19:00",
    "19:00~20:00",
    "20:00~21:00",
    "21:00~22:00",
    "22:00~23:00",
]
# 투표 기록에 저장되는 값이라 바꾸지 않는다. 화면에는 UNAVAILABLE_LABEL로 보인다.
UNAVAILABLE = "이번 회차 참여 어려움"
UNAVAILABLE_LABEL = "이번엔 어려워요"
ALL_OPTIONS = TIME_SLOTS + [UNAVAILABLE]
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
NOT_ON_TEAM = "자신이 배정된 팀의 시간 투표에만 참여할 수 있어요."


def meeting_day(meeting_date: str) -> str:
    """'2026-10-11' → '10월 11일(일)'."""
    day = datetime.date.fromisoformat(meeting_date)
    return f"{day.month}월 {day.day}일({WEEKDAYS[day.weekday()]})"


def _can_vote(poll: dict, user_id: str) -> bool:
    return bool(poll["is_test"]) or (
        settings.SUBMISSION_DESTINATIONS.get(user_id) == poll["team_channel"]
    )


def build_poll_blocks(poll: dict, counts: dict[str, int], voters: int) -> list[dict]:
    top = max(counts.get(slot, 0) for slot in TIME_SLOTS)
    lines = "\n".join(
        f"• {UNAVAILABLE_LABEL if slot == UNAVAILABLE else slot} · *{counts.get(slot, 0)}명*"
        + (" ⭐" if slot != UNAVAILABLE and top and counts.get(slot, 0) == top else "")
        for slot in ALL_OPTIONS
    )
    prefix = "*[테스트]* 🧪\n\n" if poll["is_test"] else ""
    team_size = 0 if poll["is_test"] else sum(
        1
        for channel in settings.SUBMISSION_DESTINATIONS.values()
        if channel == poll["team_channel"]
    )
    responded = f"팀원 {team_size}명 중 {voters}명 응답" if team_size else f"{voters}명 응답"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{prefix}*{poll['team_name']}은 고개를 들어주세요!* 🙌\n"
                    f"{meeting_day(poll['meeting_date'])} 온라인 회고, "
                    "참여할 수 있는 시간을 모두 골라 주세요."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": lines}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_online_retro_time_poll",
                    "text": {"type": "plain_text", "text": "가능한 시간 고르기"},
                    "style": "primary",
                    "value": str(poll["id"]),
                },
                {
                    "type": "button",
                    "action_id": "mark_online_retro_unavailable",
                    "text": {"type": "plain_text", "text": UNAVAILABLE_LABEL},
                    "value": str(poll["id"]),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{responded} · 다시 고르면 이전 응답이 바뀝니다.",
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
                    "text": f"{meeting_day(poll['meeting_date'])} 참여할 수 있는 시간을 모두 골라 주세요",
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


async def _update_poll_message(client: AsyncWebClient, poll: dict) -> None:
    if not poll["slack_ts"]:
        return
    counts, voters = await vote_counts(poll["id"], ALL_OPTIONS)
    await client.chat_update(
        channel=poll["team_channel"],
        ts=poll["slack_ts"],
        text=f"{poll['team_name']} 온라인 회고 시간 투표",
        blocks=build_poll_blocks(poll, counts, voters),
    )


async def post_team_time_poll(
    client: AsyncWebClient,
    *,
    channel: str,
    team_name: str,
    meeting_date: datetime.date,
    session_name: str,
    is_test: bool = False,
) -> dict:
    poll = await create_poll(
        meeting_date=meeting_date.isoformat(),
        session_name=session_name,
        team_channel=channel,
        team_name=team_name,
        slots=ALL_OPTIONS,
        is_test=is_test,
    )
    if poll["slack_ts"]:
        return poll
    counts, voters = await vote_counts(poll["id"], ALL_OPTIONS)
    response = await client.chat_postMessage(
        channel=channel,
        text=f"{team_name} 온라인 회고 시간 투표",
        blocks=build_poll_blocks(poll, counts, voters),
    )
    await mark_poll_posted(poll["id"], response["ts"])
    poll["slack_ts"] = response["ts"]
    return poll


async def handle_open_time_poll(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    poll = await get_poll(int(body["actions"][0]["value"]))
    if poll is None:
        raise ValueError("시간 투표를 찾을 수 없어요.")
    user_id = body["user"]["id"]
    if not _can_vote(poll, user_id):
        await post_ephemeral(
            client, channel=body["channel"]["id"], user=user_id, text=NOT_ON_TEAM
        )
        return
    previous = await get_vote(poll["id"], user_id)
    await client.views_open(
        trigger_id=body["trigger_id"], view=build_poll_modal(poll, previous)
    )


async def handle_mark_unavailable(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    """모달 없이 한 번에 '이번엔 어려워요'로 응답한다. 이전 선택은 덮어쓴다."""
    await ack()
    poll = await get_poll(int(body["actions"][0]["value"]))
    if poll is None:
        raise ValueError("시간 투표를 찾을 수 없어요.")
    user_id = body["user"]["id"]
    if not _can_vote(poll, user_id):
        await post_ephemeral(
            client, channel=body["channel"]["id"], user=user_id, text=NOT_ON_TEAM
        )
        return
    await save_vote(poll_id=poll["id"], user_id=user_id, slots=[UNAVAILABLE])
    await _update_poll_message(client, poll)
    await post_ephemeral(
        client,
        channel=poll["team_channel"],
        user=user_id,
        text="이번엔 어렵다고 남겼어요. 시간이 되면 `가능한 시간 고르기`로 바꿀 수 있어요.",
    )


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
    await ack()
    await _update_poll_message(client, poll)
    await post_ephemeral(
        client,
        channel=poll["team_channel"],
        user=user_id,
        text=f"투표를 저장했어요: {', '.join(slots)}",
    )
