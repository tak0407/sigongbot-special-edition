import json

from slack.types import CommandBodyType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database import check_user_submitted_this_session
from utils import (
    format_remaining_time,
    get_current_session_info,
    get_latest_temp_retrospective,
)


def _text_input(
    *,
    block_id: str,
    action_id: str,
    label: str,
    initial_value: str = "",
    optional: bool = False,
) -> dict:
    element = {
        "type": "plain_text_input",
        "action_id": action_id,
        "multiline": True,
        "min_length": 1,
        "max_length": 500,
    }
    if initial_value:
        element["initial_value"] = initial_value
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def build_retrospective_view(
    *,
    channel_id: str,
    session_name: str,
    initial_values: dict | None = None,
    test_mode: bool = False,
    guided_flow_id: str | None = None,
) -> dict:
    initial_values = initial_values or {}
    if test_mode:
        notice = f"현재 `{session_name}`로 테스트 중입니다. 반복해서 제출할 수 있어요."
    else:
        current_session_info = get_current_session_info()
        remaining = format_remaining_time(current_session_info[2])
        notice = (
            f"이번 회고 공유 회차는 `{session_name}` 입니다.\n"
            f"공유 마감까지 남은 시간은 `{remaining}`입니다."
        )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": notice}},
        _text_input(
            block_id="good_points",
            action_id="good_points_input",
            label="잘했고 좋았던 점을 알려주세요",
            initial_value=initial_values.get("good_points", ""),
        ),
        _text_input(
            block_id="improvements",
            action_id="improvements_input",
            label="아쉽고 개선하고 싶은 점을 알려주세요",
            initial_value=initial_values.get("improvements", ""),
        ),
        _text_input(
            block_id="learnings",
            action_id="learnings_input",
            label="새롭게 배운 점을 알려주세요",
            initial_value=initial_values.get("learnings", ""),
        ),
        _text_input(
            block_id="action_item",
            action_id="action_item_input",
            label="해볼만한 액션 아이템을 알려주세요",
            initial_value=initial_values.get("action_item", ""),
        ),
        {
            "type": "input",
            "block_id": "emotion_score",
            "optional": True,
            "label": {"type": "plain_text", "text": "오늘의 감정점수 (1-10)"},
            "element": {
                "type": "number_input",
                "action_id": "emotion_score_input",
                "is_decimal_allowed": False,
                "min_value": "1",
                "max_value": "10",
            },
        },
        _text_input(
            block_id="emotion_reason",
            action_id="emotion_reason_input",
            label="감정점수 이유를 알려주세요",
            initial_value=initial_values.get("emotion_reason", ""),
            optional=True,
        ),
        {
            "type": "input",
            "block_id": "calendar_type",
            "optional": True,
            "label": {"type": "plain_text", "text": "시간 기록 이미지 유형"},
            "element": {
                "type": "static_select",
                "action_id": "calendar_type_input",
                "initial_option": {
                    "text": {"type": "plain_text", "text": "자동 판별"},
                    "value": "auto",
                },
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "자동 판별"},
                        "value": "auto",
                    },
                    {
                        "text": {
                            "type": "plain_text",
                            "text": "수기 다이어리·캘린더",
                        },
                        "value": "handwritten_calendar",
                    },
                    {
                        "text": {"type": "plain_text", "text": "수기 계획표"},
                        "value": "handwritten_plan",
                    },
                    {
                        "text": {"type": "plain_text", "text": "디지털 캘린더"},
                        "value": "digital_calendar",
                    },
                ],
            },
        },
        {
            "type": "input",
            "block_id": "calendar_image",
            "optional": True,
            "label": {
                "type": "plain_text",
                "text": "캘린더·시간 기록 이미지 (선택)",
            },
            "hint": {
                "type": "plain_text",
                "text": "이미지를 첨부하면 게시 후 AI 피드백이 스레드에 달립니다.",
            },
            "element": {
                "type": "file_input",
                "action_id": "calendar_image_input",
                "filetypes": ["jpg", "jpeg", "png", "webp"],
                "max_files": 1,
            },
        },
    ]

    if initial_values.get("from_ai"):
        blocks.insert(
            1,
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "AI가 답변을 정리했어요. 내용을 확인하고 자유롭게 수정한 뒤 공유하세요.",
                    }
                ],
            },
        )

    return {
        "type": "modal",
        "callback_id": "retrospective_submit",
        "title": {"type": "plain_text", "text": "회고 공유"},
        "submit": {"type": "plain_text", "text": "공유하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps(
            {
                "channel_id": channel_id,
                "session_name": session_name,
                "guided_flow_id": guided_flow_id,
            },
            ensure_ascii=False,
        ),
        "blocks": blocks,
    }


async def handle_command_retrospective(
    ack: AsyncAck, body: CommandBodyType, client: AsyncWebClient
):
    await ack()
    user_id = body["user_id"]
    session_name = get_current_session_info()[1]
    test_mode = bool(settings.SESSION_NAME_OVERRIDE)

    if not test_mode and await check_user_submitted_this_session(
        user_id=user_id, session_name=session_name
    ):
        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "회고 공유"},
                "close": {"type": "plain_text", "text": "확인"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"<@{user_id}>님은 이미 `{session_name}` 회고를 공유했어요! 🤗",
                        },
                    }
                ],
            },
        )
        return

    initial_values = get_latest_temp_retrospective(user_id) or {}
    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_retrospective_view(
            channel_id=body["channel_id"],
            session_name=session_name,
            initial_values=initial_values,
            test_mode=test_mode,
        ),
    )
