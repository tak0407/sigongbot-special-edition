import json

from retrospective_content import (
    ESSAY_MAX_LENGTH,
    ESSAY_MIN_LENGTH,
    ESSAY_TITLE_MAX_LENGTH,
    STRUCTURED_TEXT_MAX_LENGTH,
)
from slack.types import CommandBodyType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from utils import (
    format_remaining_time,
    get_current_session_info,
)


def _text_input(
    *,
    block_id: str,
    action_id: str,
    label: str,
    initial_value: str = "",
    optional: bool = False,
    min_length: int = 1,
    max_length: int = STRUCTURED_TEXT_MAX_LENGTH,
    multiline: bool = True,
) -> dict:
    element = {
        "type": "plain_text_input",
        "action_id": action_id,
        "multiline": multiline,
        "min_length": min_length,
        "max_length": max_length,
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
    content_format: str = "structured",
) -> dict:
    if content_format not in {"structured", "essay"}:
        raise ValueError("회고 작성 방식을 다시 선택해 주세요.")
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
            optional=bool(guided_flow_id),
        ),
        _text_input(
            block_id="improvements",
            action_id="improvements_input",
            label="아쉽고 개선하고 싶은 점을 알려주세요",
            initial_value=initial_values.get("improvements", ""),
            optional=bool(guided_flow_id),
        ),
        _text_input(
            block_id="learnings",
            action_id="learnings_input",
            label="새롭게 배운 점을 알려주세요",
            initial_value=initial_values.get("learnings", ""),
            optional=bool(guided_flow_id),
        ),
        _text_input(
            block_id="action_item",
            action_id="action_item_input",
            label="해볼만한 액션 아이템을 알려주세요",
            initial_value=initial_values.get("action_item", ""),
            optional=bool(guided_flow_id),
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
            "block_id": "calendar_image",
            "optional": True,
            "label": {
                "type": "plain_text",
                "text": "캘린더·시간 기록 이미지 (선택)",
            },
            "hint": {
                "type": "plain_text",
                "text": "이미지는 회고와 함께 게시됩니다.",
            },
            "element": {
                "type": "file_input",
                "action_id": "calendar_image_input",
                "filetypes": ["jpg", "jpeg", "png", "webp"],
                "max_files": 1,
            },
        },
    ]

    if content_format == "essay":
        blocks[1:5] = [
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": (
                        "이번 회고 기간에 겪은 일과, 그 경험을 통해 떠올린 생각이나 "
                        "느낀 점을 자유롭게 적어주세요. 주제와 구성은 자유입니다.\n"
                        f"본문은 앞뒤 공백을 제외하고 {ESSAY_MIN_LENGTH}자 이상, "
                        f"전체 {ESSAY_MAX_LENGTH:,}자 이하로 작성해주세요."
                    ),
                },
            },
            _text_input(
                block_id="essay_title", action_id="essay_title_input",
                label="제목 (선택)", optional=True, multiline=False,
                max_length=ESSAY_TITLE_MAX_LENGTH,
                initial_value=initial_values.get("essay_title", ""),
            ),
            _text_input(
                block_id="essay_body", action_id="essay_body_input",
                label="자유 에세이", min_length=ESSAY_MIN_LENGTH,
                max_length=ESSAY_MAX_LENGTH,
                initial_value=initial_values.get("essay_body", ""),
            ),
        ]

    if initial_values.get("from_ai") and content_format == "structured":
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
                "content_format": content_format,
            },
            ensure_ascii=False,
        ),
        "blocks": blocks,
    }


async def handle_command_retrospective(
    ack: AsyncAck, body: CommandBodyType, client: AsyncWebClient
):
    await ack()
    from slack.events.submission_entry import open_submission_entry

    await open_submission_entry(
        client=client,
        trigger_id=body["trigger_id"],
        user_id=body["user_id"],
        channel_id=body["channel_id"],
    )
