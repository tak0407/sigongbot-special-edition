import asyncio
import json

from loguru import logger
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from ai_review.formatter import format_guided_answers
from config import settings
from database.guided_reflection import (
    create_guided_reflection,
    delete_guided_reflection,
    get_guided_reflection,
    save_guided_answer,
)
from reflection_questions import select_reflection_questions
from slack.events.command_retrospective import build_retrospective_view


TEST_SESSION_NAME = "테스트 회차"

RETROSPECTIVE_METHODS = [
    {
        "value": "direct",
        "title": "⚡ 포맷에 바로 작성",
        "description": "이미 정리된 생각을 네 가지 회고 항목에 빠르게 입력합니다.",
        "button": "⚡ 바로 작성",
    },
    {
        "value": "guided",
        "title": "💬 질문으로 회고",
        "description": "다양한 질문에 한 문항씩 답하면 AI가 마지막에 종합합니다.",
        "button": "💬 질문으로 회고",
    },
]


def build_method_selection_view(metadata: dict) -> dict:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "원하는 회고 방식을 선택하세요. 버튼을 누르면 바로 시작합니다.",
            },
        },
        {"type": "divider"},
    ]
    for start in range(0, len(RETROSPECTIVE_METHODS), 2):
        row = RETROSPECTIVE_METHODS[start : start + 2]
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{method['title']}*\n{method['description']}",
                    }
                    for method in row
                ],
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": (
                            f"select_retrospective_method_{method['value']}"
                        ),
                        "text": {"type": "plain_text", "text": method["button"]},
                        "style": "primary" if method["value"] == "guided" else None,
                        "value": json.dumps(
                            {**metadata, "method": method["value"]},
                            ensure_ascii=False,
                        ),
                    }
                    for method in row
                ],
            }
        )

    for block in blocks:
        if block.get("type") == "actions":
            for element in block["elements"]:
                if element.get("style") is None:
                    element.pop("style")

    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "회고 방식 선택"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }


def build_guided_question_view(flow: dict) -> dict:
    index = int(flow["current_index"])
    questions = flow["questions"]
    question = questions[index]
    is_last = index == len(questions) - 1
    return {
        "type": "modal",
        "callback_id": "guided_retrospective_submit",
        "title": {"type": "plain_text", "text": "질문으로 회고"},
        "submit": {
            "type": "plain_text",
            "text": "AI로 정리하기" if is_last else "다음 질문",
        },
        "close": {"type": "plain_text", "text": "나중에"},
        "private_metadata": flow["flow_id"],
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{index + 1}/{len(questions)} · {question['label']}*",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "guided_answer",
                "label": {"type": "plain_text", "text": question["question"]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "guided_answer_input",
                    "multiline": True,
                    "min_length": 1,
                    "max_length": 1200,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "정답을 찾기보다 지금 떠오르는 생각을 편하게 적어주세요.",
                    },
                },
            },
        ],
    }


async def handle_post_test_announcement(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    user_id = body["user"]["id"]
    if user_id not in settings.ADMIN_IDS:
        return

    channel_id = body.get("view", {}).get("private_metadata") or settings.ADMIN_CHANNEL
    metadata = json.dumps(
        {"channel_id": channel_id, "session_name": TEST_SESSION_NAME},
        ensure_ascii=False,
    )
    await client.chat_postMessage(
        channel=channel_id,
        text=f"{TEST_SESSION_NAME} 회고 제출 안내",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*테스트 회차 회고 제출 안내* 🧪\n"
                        "공지 버튼에서 시작해 회고 작성과 이미지 AI 피드백 흐름을 시험합니다.\n"
                        "제출 결과는 테스트를 위해 이 채널에 게시됩니다."
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "start_retrospective_from_announcement",
                        "text": {"type": "plain_text", "text": "회고 제출하기"},
                        "style": "primary",
                        "value": metadata,
                    }
                ],
            },
        ],
    )
    await client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text="테스트 회차 공지를 보냈어요.",
    )


async def handle_start_from_announcement(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    metadata = json.loads(body["actions"][0]["value"])
    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_method_selection_view(metadata),
    )


async def handle_method_select(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    metadata = json.loads(body["actions"][0]["value"])
    method = metadata.pop("method")
    if method == "guided":
        flow = await create_guided_reflection(
            user_id=body["user"]["id"],
            slack_channel=metadata["channel_id"],
            session_name=metadata["session_name"],
            questions=select_reflection_questions(),
        )
        await client.views_update(
            view_id=body["view"]["id"], view=build_guided_question_view(flow)
        )
        return

    await client.views_update(
        view_id=body["view"]["id"],
        view=build_retrospective_view(
            channel_id=metadata["channel_id"],
            session_name=metadata["session_name"],
            test_mode=True,
        ),
    )


def _fallback_format(flow: dict) -> dict[str, str | bool]:
    answers = flow["answers"]
    return {
        "good_points": "\n\n".join(answers[:2])[:500],
        "improvements": answers[2][:500],
        "learnings": "\n\n".join(answers[3:5])[:500],
        "action_item": answers[5][:500],
        "from_ai": True,
    }


async def _finish_guided_formatting(
    *, client: AsyncWebClient, view_id: str, flow_id: str
) -> None:
    flow = await get_guided_reflection(flow_id)
    if flow is None:
        logger.error(f"질문형 회고 흐름을 찾지 못했습니다 - Flow: {flow_id}")
        return

    responses = [
        {
            "label": question["label"],
            "question": question["question"],
            "answer": answer,
        }
        for question, answer in zip(flow["questions"], flow["answers"])
    ]
    try:
        formatted = await format_guided_answers(responses)
        formatted["from_ai"] = True
    except Exception:
        logger.exception("질문형 회고 AI 정리에 실패해 기본 매핑을 사용합니다.")
        formatted = _fallback_format(flow)

    await client.views_update(
        view_id=view_id,
        view=build_retrospective_view(
            channel_id=flow["slack_channel"],
            session_name=flow["session_name"],
            initial_values=formatted,
            test_mode=True,
        ),
    )
    await delete_guided_reflection(flow_id)


async def handle_guided_submit(
    ack: AsyncAck, body: dict, client: AsyncWebClient, view: dict
) -> None:
    flow_id = view["private_metadata"]
    answer = view["state"]["values"]["guided_answer"]["guided_answer_input"][
        "value"
    ].strip()
    flow = await save_guided_answer(
        flow_id=flow_id,
        user_id=body["user"]["id"],
        answer=answer,
    )

    if flow["current_index"] < len(flow["questions"]):
        await ack(response_action="update", view=build_guided_question_view(flow))
        return

    view_id = view["id"]
    await ack(
        response_action="update",
        view={
            "type": "modal",
            "callback_id": "guided_formatting",
            "title": {"type": "plain_text", "text": "회고 정리 중"},
            "close": {"type": "plain_text", "text": "닫기"},
            "private_metadata": flow_id,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "여섯 답변의 연결과 반복되는 패턴을 Antigravity가 정리하고 있어요. 잠시만 기다려주세요. ⏳",
                    },
                }
            ],
        },
    )
    asyncio.create_task(
        _finish_guided_formatting(
            client=client,
            view_id=view_id,
            flow_id=flow_id,
        )
    )
