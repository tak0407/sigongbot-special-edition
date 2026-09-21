import json

from loguru import logger
from slack.types import ViewBodyType, ViewType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.models.blocks import SectionBlock, DividerBlock, ContextBlock

from config import settings
from database.retrospective import (
    discard_pending_retrospective,
    mark_retrospective_posted,
    start_retrospective_submission,
)
from database.guided_reflection import delete_guided_reflection
from exception import RetrospectiveAlreadySubmitted
from slack.ephemeral import post_ephemeral
from slack.events.command_suggestion import OPEN_SUGGESTION_ACTION_ID
from utils import save_temp_retrospective, cleanup_temp_files


def _is_test_session(session_name: str) -> bool:
    return session_name == "테스트 회차" or (
        bool(settings.SESSION_NAME_OVERRIDE)
        and session_name == settings.SESSION_NAME_OVERRIDE
    )


def _get_submission_channel(
    *, user_id: str, session_name: str, requested_channel: str = ""
) -> str:
    test_mode = _is_test_session(session_name)
    if test_mode and settings.TEST_SUBMISSION_CHANNEL:
        return settings.TEST_SUBMISSION_CHANNEL
    if (
        user_id in settings.SUBMISSION_CHANNEL_CHOOSER_IDS
        and requested_channel in settings.SUBMISSION_DESTINATIONS.values()
    ):
        return requested_channel
    return settings.SUBMISSION_DESTINATIONS.get(user_id, "")


async def _offer_suggestion(
    client: AsyncWebClient, *, channel: str, user_id: str
) -> None:
    """제출 직후 작성자에게만 개선 제안 입구를 보여 준다.

    안내가 실패해도 회고는 이미 게시됐으므로 예외를 올리지 않는다. 올리면
    바깥 except가 회고 제출을 실패로 처리해 임시 저장까지 남긴다.
    """
    try:
        await post_ephemeral(
            client,
            channel=channel,
            user=user_id,
            text="회고가 공유됐어요! 🤗 시공봇에 불편하거나 바라는 점이 있으면 알려주세요.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "회고가 공유됐어요! 🤗\n"
                            "시공봇을 쓰면서 불편했거나 바라는 점이 있으면 알려주세요."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": OPEN_SUGGESTION_ACTION_ID,
                            "text": {"type": "plain_text", "text": "개선 제안하기"},
                            "value": "from_submit",
                        }
                    ],
                },
            ],
        )
    except Exception as error:
        logger.bind(alert=False).warning(
            "제출 후 개선 제안 안내 실패 - user_id={}, channel={}, error_type={}",
            user_id,
            channel,
            type(error).__name__,
        )


async def handle_view_retrospective_submit(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, view: ViewType
):
    """모달 제출 처리"""
    user_id = body["user"]["id"]
    acknowledged = False

    try:
        # 모달에서 입력된 값 추출
        values = view["state"]["values"]

        # 각 필드의 입력값 추출
        good_points = (
            values["good_points"]["good_points_input"]["value"] or "작성되지 않음"
        )
        improvements = (
            values["improvements"]["improvements_input"]["value"] or "작성되지 않음"
        )
        learnings = values["learnings"]["learnings_input"]["value"] or "작성되지 않음"
        action_item = (
            values["action_item"]["action_item_input"]["value"] or "작성되지 않음"
        )

        # 선택적 필드 처리
        emotion_score = (
            values.get("emotion_score", {})
            .get("emotion_score_input", {})
            .get("value", "")
        )
        emotion_reason = (
            values.get("emotion_reason", {})
            .get("emotion_reason_input", {})
            .get("value", "")
        )
        uploaded_files = (
            values.get("calendar_image", {})
            .get("calendar_image_input", {})
            .get("files", [])
        )
        calendar_file_id = None
        if uploaded_files:
            first_file = uploaded_files[0]
            calendar_file_id = (
                first_file.get("id") if isinstance(first_file, dict) else first_file
            )

        metadata_raw = body["view"].get("private_metadata") or ""
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {"channel_id": metadata_raw, "session_name": "테스트 회차"}
        session_name = metadata.get("session_name") or "테스트 회차"
        if metadata.get("guided_flow_id") and not any(
            (values[key][f"{key}_input"].get("value") or "").strip()
            for key in ("good_points", "improvements", "learnings", "action_item")
        ):
            await ack(
                response_action="errors",
                errors={
                    "good_points": "공유할 회고 내용을 한 항목 이상 작성해 주세요."
                },
            )
            return

        original_channel_id = _get_submission_channel(
            user_id=user_id,
            session_name=session_name,
            requested_channel=metadata.get("channel_id", ""),
        )
        if not original_channel_id:
            await ack(
                response_action="errors",
                errors={"good_points": "팀 배정이 등록되지 않았어요. 관리자에게 문의해주세요. 작성 내용은 이 창에 유지됩니다."},
            )
            return

        # 메시지 블록 생성
        blocks = [
            SectionBlock(
                text=f"*<@{user_id}>님이 `{session_name}` 회고를 공유했어요! 🤗*"
            ),
            DividerBlock(),
            ContextBlock(
                elements=[{"type": "mrkdwn", "text": "*잘했고 좋았던 점* 🌟"}]
            ),
            SectionBlock(text=good_points),
            DividerBlock(),
            ContextBlock(
                elements=[{"type": "mrkdwn", "text": "*아쉽고 개선하고 싶은 점* 🔧"}]
            ),
            SectionBlock(text=improvements),
            DividerBlock(),
            ContextBlock(elements=[{"type": "mrkdwn", "text": "*새롭게 배운 점* 💡"}]),
            SectionBlock(text=learnings),
            DividerBlock(),
            ContextBlock(
                elements=[{"type": "mrkdwn", "text": "*해볼만한 액션 아이템* 🚀"}]
            ),
            SectionBlock(text=action_item),
        ]

        # 감정 점수가 입력되었다면 추가
        if emotion_score:
            blocks.extend(
                [
                    DividerBlock(),
                    SectionBlock(
                        text=f"*오늘의 감정점수* :bar_chart: {emotion_score}/10"
                    ),
                ]
            )

            # 감정 이유가 입력되었다면 추가
            if emotion_reason:
                blocks.append(SectionBlock(text=emotion_reason))

        if calendar_file_id:
            blocks.extend(
                [
                    DividerBlock(),
                    {
                        "type": "image",
                        "slack_file": {"id": calendar_file_id},
                        "alt_text": "캘린더 또는 시간 기록 이미지",
                        "title": {"type": "plain_text", "text": "시간 기록"},
                    },
                ]
            )

        # Footer 블록 생성
        footer_blocks = [
            DividerBlock(),
            ContextBlock(
                elements=[
                    {
                        "type": "mrkdwn",
                        "text": f"회고에 문제가 있다면 <#{settings.SUPPORT_CHANNEL}>에 문의를 남겨 주세요.",
                    }
                ]
            ),
        ]

        blocks.extend(footer_blocks)

        # Slack 게시보다 먼저 로컬 SQLite에 기록한다.
        # 저장이 실패하면 Slack에도 게시되지 않으므로 중복 게시가 생기지 않는다.
        try:
            record = await start_retrospective_submission(
                user_id=user_id,
                session_name=session_name,
                slack_channel=original_channel_id,
                good_points=good_points,
                improvements=improvements,
                learnings=learnings,
                action_item=action_item,
                emotion_score=int(emotion_score) if emotion_score else None,
                emotion_reason=emotion_reason if emotion_reason else None,
                is_test=_is_test_session(session_name),
            )
        except RetrospectiveAlreadySubmitted:
            logger.warning(f"중복 회고 제출 차단 - User: {user_id}, Session: {session_name}")
            await ack(
                response_action="errors",
                errors={
                    "good_points": "이번 회차 회고는 이미 제출됐어요. 수정이 필요하면 관리자에게 문의해주세요."
                },
            )
            return

        await ack()
        acknowledged = True

        # 작성자의 배정된 팀 채널에 회고 내용 게시
        try:
            response = await client.chat_postMessage(
                channel=original_channel_id,
                blocks=blocks,
                text=f"*<@{user_id}>님이 `{session_name}` 회고를 공유했어요! 🤗*",
            )
        except Exception:
            # 게시되지 않은 회고는 되돌려 재제출을 막지 않는다.
            await discard_pending_retrospective(record["id"])
            raise

        # 게시 성공을 기록한다. 이 단계가 실패해도 Slack에는 이미 게시됐으므로
        # 회고를 삭제하거나 다시 게시하지 않고 pending 상태로 남겨 둔다.
        try:
            await mark_retrospective_posted(record["id"], response["ts"])
        except Exception as error:
            logger.error(
                f"게시 상태 기록 실패 - ID: {record['id']}, User: {user_id}, Error: {str(error)}"
            )

        # 방금 봇을 써 본 직후가 불편했던 점이 가장 생생할 때다. 공개 게시물이
        # 아니라 작성자에게만 보이는 안내로 제안 입구를 열어 준다.
        await _offer_suggestion(
            client, channel=original_channel_id, user_id=user_id
        )

        # 성공적으로 저장되면 임시 파일 삭제
        cleanup_temp_files(user_id)
        if guided_flow_id := metadata.get("guided_flow_id"):
            await delete_guided_reflection(guided_flow_id)

        # 로깅 추가
        logger.info(f"회고 제출 완료 - User: {user_id}")

    except Exception as e:
        logger.exception(f"회고 제출 실패 - User: {user_id}, Error: {str(e)}")

        # 에러 발생 시 임시 저장
        try:
            save_temp_retrospective(
                user_id,
                {
                    "good_points": locals().get("good_points", ""),
                    "improvements": locals().get("improvements", ""),
                    "learnings": locals().get("learnings", ""),
                    "action_item": locals().get("action_item", ""),
                    "emotion_score": locals().get("emotion_score", ""),
                    "emotion_reason": locals().get("emotion_reason", ""),
                },
            )
        except Exception as save_error:
            logger.error(f"임시 저장 실패 - User: {user_id}, Error: {str(save_error)}")

        if not acknowledged:
            await ack(
                response_action="errors",
                errors={
                    "good_points": "데이터 저장 중 오류가 발생했습니다. 다시 시도해주세요. (작성한 내용은 임시 저장되었습니다)"
                },
            )
        else:
            channel_id = locals().get("original_channel_id")
            if channel_id:
                await post_ephemeral(
                    client,
                    channel=channel_id,
                    user=user_id,
                    text="회고 게시 중 오류가 발생했어요. 작성 내용은 임시 저장했습니다.",
                )
