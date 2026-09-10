import asyncio
import json

from loguru import logger
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from ai_review.formatter import format_guided_answers
from config import settings
from database.guided_reflection import (
    create_guided_reflection,
    get_guided_reflection,
    go_to_previous_question,
    save_guided_format,
    save_guided_answer,
)
from database.retrospective import get_submitted_user_ids
from database.scheduled_announcements import announcement_sent, mark_announcement_sent
from reflection_questions import select_reflection_questions
from slack.events.command_retrospective import build_retrospective_view
from constants import DUE_DATES, SIXTH_FIRST_SESSION_START
from utils import tz_now


TEST_SESSION_NAME = "테스트 회차"
SIXTH_FIRST_SESSION_NAME = "6기 1회차"
SIXTH_FIRST_REMINDER_AT = SIXTH_FIRST_SESSION_START.replace(day=13, hour=21)
SIXTH_FIRST_SESSION_DUE = DUE_DATES[-1]

RETROSPECTIVE_METHODS = [
    {
        "value": "direct",
        "title": "⚡ 포맷에 바로 작성",
        "description": "이미 정리된 생각을 네 가지 회고 항목에 빠르게 입력합니다.",
    },
    {
        "value": "guided",
        "title": "💬 질문으로 회고 (베타)",
        "description": "4개 항목의 질문에 답하면 AI가 정리해요. 답변은 건너뛸 수 있어요.",
    },
]


def build_method_selection_view(metadata: dict) -> dict:
    options = [
        {
            "text": {"type": "plain_text", "text": method["title"]},
            "description": {"type": "plain_text", "text": method["description"]},
            "value": method["value"],
        }
        for method in RETROSPECTIVE_METHODS
    ]
    return {
        "type": "modal",
        "callback_id": "select_retrospective_method",
        "title": {"type": "plain_text", "text": "회고 방식 선택"},
        "submit": {"type": "plain_text", "text": "시작하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps(metadata, ensure_ascii=False),
        "blocks": [
            {
                "type": "input",
                "block_id": "retrospective_method",
                "label": {"type": "plain_text", "text": "회고 방식을 선택해 주세요"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "method_input",
                    "options": options,
                    "initial_option": options[0],
                },
            }
        ],
    }


def build_guided_question_view(flow: dict) -> dict:
    index = int(flow["current_index"])
    questions = flow["questions"]
    question = questions[index]
    is_last = index == len(questions) - 1
    grouped = "questions" in question
    prompts = question["questions"] if grouped else [question["question"]]
    saved = flow["answers"][index] if index < len(flow["answers"]) else []
    saved = [saved] if isinstance(saved, str) else saved
    blocks = [
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
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": "답할 수 있는 질문만 적어주세요. 빈 질문은 건너뛰고, 작성한 답변은 이동할 때 저장돼요.",
            },
        },
    ]
    for number, prompt in enumerate(prompts):
        element = {
            "type": "plain_text_input",
            "action_id": "guided_answer_input",
            "multiline": True,
            "max_length": 1200,
            "placeholder": {
                "type": "plain_text",
                "text": "떠오르는 생각을 편하게 적어주세요. (선택)",
            },
        }
        if number < len(saved) and saved[number]:
            element["initial_value"] = saved[number]
        blocks.append(
            {
                "type": "input",
                "block_id": f"guided_answer_{index}_{number}"
                if grouped
                else f"guided_answer_{index}",
                "optional": True,
                "label": {"type": "plain_text", "text": f"{number + 1}. {prompt}"},
                "element": element,
            }
        )
    navigation = [
        {
            "type": "button",
            "action_id": "guided_skip_section",
            "text": {"type": "plain_text", "text": "이 항목 건너뛰기"},
            "value": flow["flow_id"],
        }
    ]
    if index > 0:
        navigation.insert(
            0,
            {
                "type": "button",
                "action_id": "guided_previous_question",
                "text": {"type": "plain_text", "text": "← 이전 항목"},
                "value": flow["flow_id"],
            },
        )
    blocks.append({"type": "actions", "elements": navigation})

    return {
        "type": "modal",
        "callback_id": "guided_retrospective_submit",
        "external_id": f"guided-reflection-{flow['flow_id']}",
        "title": {"type": "plain_text", "text": "질문으로 회고 (베타)"},
        "submit": {
            "type": "plain_text",
            "text": "답변 정리하기" if is_last else "다음 항목",
        },
        "close": {"type": "plain_text", "text": "닫기"},
        "private_metadata": flow["flow_id"],
        "blocks": blocks,
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
        {
            "channel_id": channel_id,
            "session_name": TEST_SESSION_NAME,
        },
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
                        + (
                            f"제출 결과는 테스트 채널 <#{settings.TEST_SUBMISSION_CHANNEL}>에 게시됩니다."
                            if settings.TEST_SUBMISSION_CHANNEL
                            else "제출 결과는 작성자에게 배정된 팀 채널에 게시됩니다."
                        )
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


def _announcement_blocks(channel_id: str) -> list[dict]:
    metadata = json.dumps(
        {
            "channel_id": channel_id,
            "session_name": SIXTH_FIRST_SESSION_NAME,
            "test_mode": False,
        },
        ensure_ascii=False,
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*6기 1회차 회고를 제출해 주세요* 🌱\n"
                    "이번 기수부터 회고를 더 편하게 남길 수 있도록 제출 방식을 바꿨어요.\n"
                    "아래 버튼에서 회고를 작성하면 이 채널에 자동으로 공유됩니다.\n\n"
                    "6기의 첫 회고인 만큼, 이번 주를 살아낸 나에게 다음 한 주를 위한 작은 힌트를 남겨주세요."
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
    ]


def _team_channels() -> dict[str, set[str]]:
    channels: dict[str, set[str]] = {}
    for user_id, channel_id in settings.SUBMISSION_DESTINATIONS.items():
        channels.setdefault(channel_id, set()).add(user_id)
    return channels


async def post_sixth_first_announcement(client: AsyncWebClient) -> None:
    for channel_id in _team_channels():
        key = f"6-1-announcement:{channel_id}"
        if await announcement_sent(key):
            continue
        await client.chat_postMessage(
            channel=channel_id,
            text="6기 1회차 회고를 제출해 주세요.",
            blocks=_announcement_blocks(channel_id),
        )
        await mark_announcement_sent(key)


async def post_sixth_first_reminder(client: AsyncWebClient) -> None:
    submitted = await get_submitted_user_ids(SIXTH_FIRST_SESSION_NAME)
    for channel_id, members in _team_channels().items():
        key = f"6-1-reminder:{channel_id}"
        if members <= submitted or await announcement_sent(key):
            continue
        await client.chat_postMessage(
            channel=channel_id,
            text="6기 1회차 회고 마감까지 약 하루 남았어요.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*6기 1회차 회고 마감까지 약 하루 남았어요* ⏰\n"
                            "아직 회고를 남기지 않았다면 위 공지의 `회고 제출하기` 버튼에서 작성해 주세요.\n"
                            "마감은 화요일 오전 5시입니다."
                        ),
                    },
                }
            ],
        )
        await mark_announcement_sent(key)


async def run_sixth_first_announcement_scheduler(client: AsyncWebClient) -> None:
    logger.info("6기 1회차 공지 스케줄러가 시작되었습니다.")
    while True:
        now = tz_now()
        try:
            if SIXTH_FIRST_SESSION_START <= now < SIXTH_FIRST_SESSION_DUE:
                await post_sixth_first_announcement(client)
            if SIXTH_FIRST_REMINDER_AT <= now < SIXTH_FIRST_SESSION_DUE:
                await post_sixth_first_reminder(client)
        except Exception:
            logger.exception("6기 1회차 공지 발송에 실패했습니다.")
        await asyncio.sleep(60)


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
    legacy_action = bool(body.get("actions"))
    if legacy_action:
        await ack()
        metadata = json.loads(body["actions"][0]["value"])
        method = metadata.pop("method")
    else:
        metadata = json.loads(body["view"]["private_metadata"])
        method = body["view"]["state"]["values"]["retrospective_method"][
            "method_input"
        ]["selected_option"]["value"]
    if method not in {"direct", "guided"}:
        raise ValueError("회고 방식을 다시 선택해 주세요.")
    if method == "guided":
        flow = await create_guided_reflection(
            user_id=body["user"]["id"],
            slack_channel=metadata["channel_id"],
            session_name=metadata["session_name"],
            questions=select_reflection_questions(),
        )
        next_view = build_guided_question_view(flow)
    else:
        next_view = build_retrospective_view(
            channel_id=metadata["channel_id"],
            session_name=metadata["session_name"],
            test_mode=metadata.get("test_mode", True),
        )
    if legacy_action:
        await client.views_update(view_id=body["view"]["id"], view=next_view)
    else:
        await ack(response_action="update", view=next_view)


def _fallback_format(flow: dict) -> dict[str, str | bool]:
    answers = flow["answers"]
    if "questions" in flow["questions"][0]:
        return {
            **{
                group["stage"]: "\n\n".join(
                    answer for answer in group_answers if answer
                )[:500]
                for group, group_answers in zip(flow["questions"], answers)
            },
            "from_ai": False,
        }
    return {
        "good_points": "\n\n".join(answers[:2])[:500],
        "improvements": answers[2][:500],
        "learnings": "\n\n".join(answers[3:5])[:500],
        "action_item": answers[5][:500],
        "from_ai": False,
    }


async def _finish_guided_formatting(*, client: AsyncWebClient, flow_id: str) -> None:
    flow = await get_guided_reflection(flow_id)
    if flow is None:
        logger.error(f"질문형 회고 흐름을 찾지 못했습니다 - Flow: {flow_id}")
        return

    responses = []
    for group, answers in zip(flow["questions"], flow["answers"]):
        grouped = "questions" in group
        prompts = group["questions"] if grouped else [group["question"]]
        for question, answer in zip(prompts, answers if grouped else [answers]):
            responses.append(
                {
                    "stage": group["stage"],
                    "label": group["label"],
                    "question": question,
                    "answer": answer,
                }
            )
    try:
        if any(item["answer"] for item in responses):
            formatted = await format_guided_answers(responses)
            formatted["from_ai"] = True
        else:
            formatted = _fallback_format(flow)
    except Exception:
        logger.exception("질문형 회고 AI 정리에 실패해 기본 매핑을 사용합니다.")
        formatted = _fallback_format(flow)

    notice = (
        "AI가 질문형 회고를 정리했어요."
        if formatted["from_ai"]
        else "AI 정리 없이 입력한 답변을 모았어요. 항목별 최대 500자까지 표시돼요."
    )

    try:
        await save_guided_format(flow_id=flow_id, formatted=formatted)
        await client.chat_postEphemeral(
            channel=flow["slack_channel"],
            user=flow["user_id"],
            text=notice,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{notice}*\n내용을 확인하고 수정한 뒤 공유해 주세요.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "open_guided_result",
                            "text": {
                                "type": "plain_text",
                                "text": "정리된 회고 확인",
                            },
                            "style": "primary",
                            "value": flow_id,
                        }
                    ],
                },
            ],
        )
    except Exception:
        logger.exception(f"질문형 회고 결과 알림 실패 - Flow: {flow_id}")


def _read_guided_answers(view: dict) -> str | list[str]:
    inputs = [block for block in view["blocks"] if block.get("type") == "input"]
    values = view.get("state", {}).get("values", {})
    answers = [
        (
            values.get(block["block_id"], {})
            .get("guided_answer_input", {})
            .get("value")
            or ""
        ).strip()
        for block in inputs
    ]
    # 기존에 열린 1문항 모달의 문자열 저장 형식도 유지한다.
    return answers[0] if len(inputs) == 1 else answers


def _formatting_view() -> dict:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "질문으로 회고 (베타)"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": "답변을 저장했어요. 이 창을 닫아도 괜찮아요. 정리가 끝나면 제출 채널에 나에게만 보이는 확인 버튼이 도착해요.",
                },
            }
        ],
    }


async def handle_guided_skip(ack: AsyncAck, body: dict, client: AsyncWebClient) -> None:
    await ack()
    flow = await save_guided_answer(
        flow_id=body["view"]["private_metadata"],
        user_id=body["user"]["id"],
        answer=_read_guided_answers(body["view"]),
    )
    finished = flow["current_index"] == len(flow["questions"])
    await client.views_update(
        view_id=body["view"]["id"],
        view=_formatting_view() if finished else build_guided_question_view(flow),
    )
    if finished:
        asyncio.create_task(
            _finish_guided_formatting(client=client, flow_id=flow["flow_id"])
        )


async def handle_guided_submit(
    ack: AsyncAck, body: dict, client: AsyncWebClient, view: dict
) -> None:
    flow_id = view["private_metadata"]
    flow = await save_guided_answer(
        flow_id=flow_id,
        user_id=body["user"]["id"],
        answer=_read_guided_answers(view),
    )

    if flow["current_index"] < len(flow["questions"]):
        await ack(response_action="update", view=build_guided_question_view(flow))
        return

    await ack(response_action="update", view=_formatting_view())
    asyncio.create_task(_finish_guided_formatting(client=client, flow_id=flow_id))


async def handle_guided_previous(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    flow = await go_to_previous_question(
        flow_id=body["actions"][0]["value"],
        user_id=body["user"]["id"],
        current_answer=_read_guided_answers(body["view"]),
    )
    await client.views_update(
        view_id=body["view"]["id"],
        view=build_guided_question_view(flow),
    )


async def handle_open_guided_result(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    flow_id = body["actions"][0]["value"]
    flow = await get_guided_reflection(flow_id)
    if (
        flow is None
        or flow["user_id"] != body["user"]["id"]
        or flow["formatted"] is None
    ):
        await client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=body["user"]["id"],
            text="정리된 회고를 찾지 못했어요. 다시 질문형 회고를 시작해 주세요.",
        )
        return

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=build_retrospective_view(
            channel_id=flow["slack_channel"],
            session_name=flow["session_name"],
            initial_values=flow["formatted"],
            test_mode=True,
            guided_flow_id=flow_id,
        ),
    )
