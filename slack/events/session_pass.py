"""회차 회고를 건너뛰는 패스 버튼과 확인 모달."""

import json

from loguru import logger
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from constants import MAX_PASS_COUNT
from database.passes import (
    PassAlreadyUsed,
    attach_message,
    check_eligibility_async,
    record_pass,
)
from slack.ephemeral import post_ephemeral
from slack.events.submission_entry import submission_preflight

PASS_ACTION_ID = "use_session_pass"
PASS_CALLBACK_ID = "session_pass_confirm"


def build_pass_view(*, session_name: str, channel_id: str, eligibility) -> dict:
    """확인 모달. 쓸 수 없으면 사유만 보여 주고 확인 버튼을 주지 않는다."""
    if eligibility.allowed:
        body = (
            f"`{session_name}` 회고를 건너뜁니다.\n\n"
            f"이번 패스를 쓰면 남은 패스는 *{eligibility.remaining - 1}회*입니다.\n"
            "확인을 누르면 팀 채널에 패스 사실이 공유됩니다."
        )
    else:
        body = eligibility.reason

    view = {
        "type": "modal",
        "callback_id": PASS_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "회고 패스"},
        "close": {"type": "plain_text", "text": "닫기"},
        "private_metadata": json.dumps(
            {"channel_id": channel_id, "session_name": session_name},
            ensure_ascii=False,
        ),
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"패스는 기수당 {MAX_PASS_COUNT}회까지, "
                            "직전 회차에 쓰지 않았을 때만 사용할 수 있어요."
                        ),
                    }
                ],
            },
        ],
    }
    # 확인 버튼이 없으면 모달을 닫는 것 말고는 아무 일도 일어나지 않는다.
    if eligibility.allowed:
        view["submit"] = {"type": "plain_text", "text": "패스 사용하기"}
    return view


async def handle_open_pass_modal(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    user_id = body["user"]["id"]
    channel_id = (body.get("channel") or {}).get("id", "")

    metadata = {}
    actions = body.get("actions") or [{}]
    try:
        metadata = json.loads(actions[0].get("value") or "{}")
    except ValueError:
        metadata = {}
    session_name = str(metadata.get("session_name") or "")
    channel_id = channel_id or str(metadata.get("channel_id") or "")

    # 제출과 같은 전제(기간·팀 배정·중복)를 먼저 통과해야 한다.
    error = await submission_preflight(user_id, session_name)
    if error:
        await post_ephemeral(client, channel=channel_id, user=user_id, text=error)
        return

    eligibility = await check_eligibility_async(user_id, session_name)
    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_pass_view(
            session_name=session_name, channel_id=channel_id, eligibility=eligibility
        ),
    )


async def handle_pass_confirm(
    ack: AsyncAck, body: dict, client: AsyncWebClient, view: dict
) -> None:
    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata") or "{}")
    session_name = str(metadata.get("session_name") or "")
    channel_id = str(metadata.get("channel_id") or "")
    team_channel = settings.SUBMISSION_DESTINATIONS.get(user_id, "") or channel_id

    # 모달을 열어 둔 사이에 제출하거나 마감됐을 수 있으므로 다시 확인한다.
    error = await submission_preflight(user_id, session_name)
    if error is None:
        eligibility = await check_eligibility_async(user_id, session_name)
        if not eligibility.allowed:
            error = eligibility.reason
    if error:
        await ack()
        await post_ephemeral(client, channel=team_channel, user=user_id, text=error)
        return

    try:
        pass_id = record_pass(
            user_id=user_id, session_name=session_name, team_channel=team_channel
        )
    except PassAlreadyUsed:
        await ack()
        await post_ephemeral(
            client,
            channel=team_channel,
            user=user_id,
            text=f"이미 `{session_name}` 패스를 사용했어요.",
        )
        return
    except Exception:
        logger.exception("패스 기록 실패 - user_id={}, session={}", user_id, session_name)
        await ack()
        await post_ephemeral(
            client,
            channel=team_channel,
            user=user_id,
            text="패스를 기록하지 못했어요. 잠시 후 다시 시도해 주세요.",
        )
        return

    await ack()
    logger.info("회고 패스 사용 - user_id={}, session={}", user_id, session_name)

    # 패스는 팀에 공개한다. 게시가 실패해도 기록은 이미 남았으므로 되돌리지 않는다.
    try:
        response = await client.chat_postMessage(
            channel=team_channel,
            text=f"<@{user_id}>님은 `{session_name}` 회고를 패스했어요. 이번 주는 패스입니다~ 🌿",
        )
        attach_message(pass_id, response["ts"])
    except Exception:
        logger.bind(alert=False).warning(
            "패스 공개 안내 실패 - user_id={}, session={}", user_id, session_name
        )
