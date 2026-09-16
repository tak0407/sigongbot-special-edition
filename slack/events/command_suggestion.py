"""Slack `/제안` 명령과 개선 제안 작성 모달."""

import json

from loguru import logger
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database.suggestions import create_suggestion
from slack.ephemeral import post_ephemeral
from slack.types import CommandBodyType, ViewBodyType, ViewType

CATEGORIES = {
    "feature": "새 기능",
    "usability": "사용성 개선",
    "bug": "오류·불편",
    "other": "기타",
}


def build_suggestion_view(channel_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "bot_improvement_suggestion_submit",
        "title": {"type": "plain_text", "text": "시공봇 개선 제안"},
        "submit": {"type": "plain_text", "text": "제출하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps({"channel_id": channel_id}),
        "blocks": [
            {
                "type": "input",
                "block_id": "category",
                "label": {"type": "plain_text", "text": "분류"},
                "element": {
                    "type": "static_select",
                    "action_id": "category_input",
                    "placeholder": {"type": "plain_text", "text": "분류를 선택해 주세요"},
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": label},
                            "value": value,
                        }
                        for value, label in CATEGORIES.items()
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "content",
                "label": {"type": "plain_text", "text": "제안 내용"},
                "hint": {
                    "type": "plain_text",
                    "text": "불편했던 상황이나 기대하는 동작을 구체적으로 알려 주세요.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "content_input",
                    "multiline": True,
                    "min_length": 5,
                    "max_length": 2000,
                },
            },
        ],
    }


async def handle_command_suggestion(
    ack: AsyncAck, body: CommandBodyType, client: AsyncWebClient
) -> None:
    await ack()
    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_suggestion_view(body["channel_id"]),
    )


async def _notify_user(
    client: AsyncWebClient, *, channel: str, user_id: str, text: str
) -> None:
    try:
        await post_ephemeral(client, channel=channel, user=user_id, text=text)
    except Exception as error:
        logger.bind(alert=False).warning(
            "제안 제출 결과 안내 실패 - user_id={}, channel={}, error_type={}",
            user_id,
            channel,
            type(error).__name__,
        )


async def handle_view_suggestion_submit(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, view: ViewType
) -> None:
    user_id = body["user"]["id"]
    values = view["state"]["values"]
    selected = values["category"]["category_input"].get("selected_option") or {}
    category = str(selected.get("value") or "")
    content = str(values["content"]["content_input"].get("value") or "").strip()
    try:
        metadata = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    submission_channel = str(metadata.get("channel_id") or "")

    errors = {}
    if category not in CATEGORIES:
        errors["category"] = "분류를 선택해 주세요."
    if len(content) < 5:
        errors["content"] = "제안 내용을 5자 이상 작성해 주세요."
    if not submission_channel:
        errors["content"] = "제출 채널을 확인할 수 없습니다. 명령을 다시 실행해 주세요."
    if errors:
        await ack(response_action="errors", errors=errors)
        return

    try:
        suggestion = await create_suggestion(
            category=category,
            content=content,
            user_id=user_id,
            submission_channel=submission_channel,
        )
    except Exception as error:
        logger.bind(alert=False).error(
            "봇 개선 제안 DB 저장 실패 - user_id={}, channel={}, error_type={}",
            user_id,
            submission_channel,
            type(error).__name__,
        )
        await ack(
            response_action="errors",
            errors={
                "content": "제안을 저장하지 못했어요. 잠시 후 다시 제출해 주세요. 작성 내용은 유지됩니다."
            },
        )
        return

    await ack()
    admin_notified = True
    try:
        await client.chat_postMessage(
            channel=settings.ADMIN_CHANNEL,
            text=(
                f"새 시공봇 개선 제안 #{suggestion['id']} · "
                f"{CATEGORIES[category]} · 작성자 {user_id}"
            ),
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*새 시공봇 개선 제안 #{suggestion['id']}*\n"
                            f"분류: {CATEGORIES[category]} · 작성자: <@{user_id}> · "
                            f"제출 채널: <#{submission_channel}>"
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": content},
                },
            ],
        )
    except Exception as error:
        admin_notified = False
        logger.bind(alert=False).warning(
            "저장된 제안 관리자 알림 실패 - suggestion_id={}, error_type={}",
            suggestion["id"],
            type(error).__name__,
        )

    if admin_notified:
        message = f"제안 #{suggestion['id']}이 접수됐어요. 소중한 의견 감사합니다!"
    else:
        message = (
            f"제안 #{suggestion['id']}은 안전하게 저장됐어요. "
            "다만 관리자 알림 전송이 지연되어 대시보드에서 확인할 예정입니다."
        )
    await _notify_user(
        client,
        channel=submission_channel,
        user_id=user_id,
        text=message,
    )
