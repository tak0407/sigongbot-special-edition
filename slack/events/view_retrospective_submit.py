import json

from loguru import logger
from slack.types import ViewBodyType, ViewType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.models.blocks import SectionBlock, DividerBlock, ContextBlock

from config import settings
from database.retrospective import create_retrospective
from database.ai_review import enqueue_ai_review
from utils import save_temp_retrospective, cleanup_temp_files


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
        calendar_type = (
            values.get("calendar_type", {})
            .get("calendar_type_input", {})
            .get("selected_option", {})
            .get("value", "auto")
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

        # command_retrospective에서 호출된 채널 ID 가져오기
        original_channel_id = metadata.get("channel_id") or body["user"]["id"]

        await ack()
        acknowledged = True

        # 원래의 채널에 회고 내용 게시
        response = await client.chat_postMessage(
            channel=original_channel_id,
            blocks=blocks,
            text=f"*<@{user_id}>님이 `{session_name}` 회고를 공유했어요! 🤗*",
        )

        # 메시지 타임스탬프 가져오기
        slack_ts = response["ts"]
        # 로컬 SQLite에 데이터 저장
        await create_retrospective(
            user_id=user_id,
            session_name=session_name,
            slack_channel=original_channel_id,
            slack_ts=slack_ts,
            good_points=good_points,
            improvements=improvements,
            learnings=learnings,
            action_item=action_item,
            emotion_score=int(emotion_score) if emotion_score else None,
            emotion_reason=emotion_reason if emotion_reason else None,
        )

        if calendar_file_id:
            retrospective_text = "\n".join(
                [
                    f"잘한 점: {good_points}",
                    f"개선할 점: {improvements}",
                    f"배운 점: {learnings}",
                    f"다음 액션: {action_item}",
                ]
            )
            await enqueue_ai_review(
                user_id=user_id,
                slack_channel=original_channel_id,
                slack_ts=slack_ts,
                file_id=calendar_file_id,
                calendar_type=calendar_type,
                retrospective_text=retrospective_text,
            )
            await client.chat_postMessage(
                channel=original_channel_id,
                thread_ts=slack_ts,
                text="이미지를 확인했어요. AI 시간 리뷰를 준비하고 있습니다. 잠시만 기다려주세요. ⏳",
            )

        # 성공적으로 저장되면 임시 파일 삭제
        cleanup_temp_files(user_id)

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
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="회고 게시 중 오류가 발생했어요. 작성 내용은 임시 저장했습니다.",
                )
