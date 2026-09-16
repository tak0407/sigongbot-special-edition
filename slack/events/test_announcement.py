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
from reflection_questions import select_reflection_questions
from utils import get_latest_temp_retrospective
from slack.events.command_retrospective import build_retrospective_view


TEST_SESSION_NAME = "테스트 회차"

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


def build_method_selection_view(
    metadata: dict, channel_options: list[dict] | None = None
) -> dict:
    options = [
        {
            "text": {"type": "plain_text", "text": method["title"]},
            "description": {"type": "plain_text", "text": method["description"]},
            "value": method["value"],
        }
        for method in RETROSPECTIVE_METHODS
    ]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f"이번에 제출할 회고는 {metadata['session_name']}입니다.",
            },
        },
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
    ]
    if channel_options:
        blocks.append(
            {
                "type": "input",
                "block_id": "submission_channel",
                "label": {"type": "plain_text", "text": "회고를 공유할 팀 채널"},
                "element": {
                    "type": "static_select",
                    "action_id": "channel_input",
                    "placeholder": {"type": "plain_text", "text": "팀 채널 선택"},
                    "options": channel_options,
                },
            }
        )
    return {
        "type": "modal",
        "callback_id": "select_retrospective_method",
        "title": {"type": "plain_text", "text": "회고 방식 선택"},
        "submit": {"type": "plain_text", "text": "시작하기"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps(metadata, ensure_ascii=False),
        "blocks": blocks,
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


def _team_channels() -> dict[str, set[str]]:
    channels: dict[str, set[str]] = {}
    for user_id, channel_id in settings.SUBMISSION_DESTINATIONS.items():
        channels.setdefault(channel_id, set()).add(user_id)
    return channels


async def handle_start_from_announcement(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    from slack.events.submission_entry import open_submission_entry

    try:
        metadata = json.loads(body["actions"][0]["value"])
        session_name = (metadata.get("session_name") or "") if isinstance(metadata, dict) else ""
    except (ValueError, TypeError):
        session_name = ""
    await open_submission_entry(
        client=client,
        trigger_id=body["trigger_id"],
        user_id=body["user"]["id"],
        channel_id=body["channel"]["id"],
        session_name=session_name,
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
        values = body["view"]["state"]["values"]
        method = values["retrospective_method"][
            "method_input"
        ]["selected_option"]["value"]
        if body["user"]["id"] in settings.SUBMISSION_CHANNEL_CHOOSER_IDS:
            selected = values.get("submission_channel", {}).get(
                "channel_input", {}
            ).get("selected_option")
            channel_id = selected.get("value") if selected else ""
            if channel_id not in _team_channels():
                raise ValueError("공유할 팀 채널을 다시 선택해 주세요.")
            metadata["channel_id"] = channel_id
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
            initial_values=get_latest_temp_retrospective(body["user"]["id"]),
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
