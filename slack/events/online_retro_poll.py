"""팀별 온라인 회고 시간 투표 UI와 처리기."""

import datetime
import json

from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database.online_retro_poll import (
    create_poll,
    get_poll,
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
UNAVAILABLE = "이번 회차 참여 어려움"
ALL_OPTIONS = TIME_SLOTS + [UNAVAILABLE]


def build_poll_blocks(poll: dict, counts: dict[str, int], voters: int) -> list[dict]:
    lines = "\n".join(f"• {slot} · *{counts.get(slot, 0)}명*" for slot in ALL_OPTIONS)
    prefix = "*[테스트]* 🧪\n\n" if poll["is_test"] else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{prefix}*{poll['team_name']}은 고개를 들어주세요!* 🙌\n"
                    f"{poll['meeting_date']} 온라인 회고에 참여 가능한 "
                    "1시간 구간을 모두 골라주세요."
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
                    "text": {"type": "plain_text", "text": "가능한 시간 선택"},
                    "style": "primary",
                    "value": str(poll["id"]),
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"현재 {voters}명 투표 · 다시 선택하면 이전 응답이 바뀝니다.",
                }
            ],
        },
    ]


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
    if not poll["is_test"] and settings.SUBMISSION_DESTINATIONS.get(user_id) != poll["team_channel"]:
        await post_ephemeral(
            client,
            channel=body["channel"]["id"],
            user=user_id,
            text="자신이 배정된 팀의 시간 투표에만 참여할 수 있어요.",
        )
        return
    await client.views_open(
        trigger_id=body["trigger_id"],
        view={
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
                    "label": {"type": "plain_text", "text": "가능한 시간을 모두 선택해 주세요"},
                    "element": {
                        "type": "multi_static_select",
                        "action_id": "available_slots_input",
                        "placeholder": {"type": "plain_text", "text": "가능한 시간 선택"},
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": slot},
                                "value": slot,
                            }
                            for slot in ALL_OPTIONS
                        ],
                    },
                }
            ],
        },
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
    slots = [option["value"] for option in selected]
    if not slots:
        await ack(
            response_action="errors",
            errors={"available_slots": "가능한 시간이나 참여 어려움을 선택해 주세요."},
        )
        return
    if UNAVAILABLE in slots and len(slots) > 1:
        await ack(
            response_action="errors",
            errors={"available_slots": "참여 어려움은 다른 시간과 함께 선택할 수 없어요."},
        )
        return
    user_id = body["user"]["id"]
    if not poll["is_test"] and settings.SUBMISSION_DESTINATIONS.get(user_id) != poll["team_channel"]:
        raise ValueError("자신이 배정된 팀의 시간 투표에만 참여할 수 있어요.")
    await save_vote(poll_id=poll["id"], user_id=user_id, slots=slots)
    await ack()
    counts, voters = await vote_counts(poll["id"], poll["slots"])
    if poll["slack_ts"]:
        await client.chat_update(
            channel=poll["team_channel"],
            ts=poll["slack_ts"],
            text=f"{poll['team_name']} 온라인 회고 시간 투표",
            blocks=build_poll_blocks(poll, counts, voters),
        )
    await post_ephemeral(
        client,
        channel=poll["team_channel"],
        user=user_id,
        text=f"투표를 저장했어요: {', '.join(slots)}",
    )
