from slack.types import ViewBodyType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.models.views import View
from slack_sdk.models.blocks import SectionBlock
from loguru import logger

from database.retrospective import get_retrospective_by_id
from slack.retrospective_blocks import build_retrospective_blocks


async def handle_action_view_retrospective_detail(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, action: dict
):
    """회고 상세보기 액션 처리"""
    await ack()

    user_id = body["user"]["id"]
    retrospective_id = int(action["value"])

    try:
        # 회고 데이터 가져오기
        retrospective = await get_retrospective_by_id(retrospective_id)

        # 자신의 회고만 볼 수 있도록 체크
        if retrospective["user_id"] != user_id:
            # 권한 없음 메시지
            await client.views_open(
                trigger_id=body["trigger_id"],
                view=View(
                    type="modal",
                    title="접근 거부",
                    close="확인",
                    blocks=[
                        SectionBlock(text="다른 사용자의 회고는 볼 수 없습니다."),
                    ],
                ),
            )
            return

        # 회고 정보 추출
        session_name = retrospective["session_name"]
        created_at = (
            retrospective["created_at"].split("T")[0]
            if "T" in retrospective["created_at"]
            else retrospective["created_at"]
        )

        # 회고 상세 모달 블록 생성
        blocks = build_retrospective_blocks(
            retrospective, heading=f"*{session_name}* ({created_at})"
        )

        # 상세 모달 표시 (views_push 사용)
        await client.views_push(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="회고 상세",
                close="뒤로가기",
                blocks=blocks,
            ),
        )

    except Exception as e:
        logger.error(
            f"회고 상세 조회 실패 - User: {user_id}, ID: {retrospective_id}, Error: {str(e)}"
        )
        # 오류 메시지 모달
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="오류",
                close="확인",
                blocks=[
                    SectionBlock(
                        text=f"회고 상세정보를 불러오는 중 오류가 발생했습니다: {str(e)}"
                    )
                ],
            ),
        )
