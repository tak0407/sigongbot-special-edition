from loguru import logger
from exception import BotException
from slack.types import ViewBodyType, ViewType
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.models.views import View
from slack_sdk.models.blocks import (
    SectionBlock,
    DividerBlock,
    ActionsBlock,
    ButtonElement,
    InputBlock,
    PlainTextInputElement,
    ContextBlock,
    NumberInputElement,
)
from config import settings
from database.retrospective import get_retrospective_by_id


async def handle_view_admin_menu(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, view: ViewType
):
    """관리자 메뉴 모달 제출 처리"""
    user_id = body["user"]["id"]

    # 관리자 권한 확인
    if user_id not in settings.ADMIN_IDS:
        await ack(
            response_action="errors",
            errors={"retrospective_id": "관리자 권한이 없습니다."},
        )
        return

    try:
        # 모달에서 입력된 값 추출
        values = view["state"]["values"]
        retrospective_id = int(
            values["retrospective_id"]["retrospective_id_input"]["value"]
        )

        # 회고 데이터 가져오기
        retrospective = await get_retrospective_by_id(retrospective_id)

        # 회고 데이터 확인
        if not retrospective:
            await ack(
                response_action="errors",
                errors={
                    "retrospective_id": f"ID {retrospective_id}에 해당하는 회고가 없습니다."
                },
            )
            return

        # 회고 데이터 표시
        retro_user = retrospective["user_id"]
        session_name = retrospective["session_name"]
        created_at = (
            retrospective["created_at"].split("T")[0]
            if "T" in retrospective["created_at"]
            else retrospective["created_at"]
        )
        slack_channel = retrospective["slack_channel"]
        slack_ts = retrospective["slack_ts"]

        # 관리 작업 선택 모달 생성
        blocks = [
            SectionBlock(
                text=f"*회고 상세 정보*\n\n"
                f"*ID:* {retrospective_id}\n"
                f"*작성자:* <@{retro_user}>\n"
                f"*회차:* {session_name}\n"
                f"*작성일:* {created_at}\n"
                f"*채널:* <#{slack_channel}>\n"
            ),
            DividerBlock(),
            SectionBlock(
                text="*회고 내용*\n\n"
                f"*잘했던 점:*\n{retrospective['good_points']}\n\n"
                f"*개선할 점:*\n{retrospective['improvements']}\n\n"
                f"*배운 점:*\n{retrospective['learnings']}\n\n"
                f"*액션 아이템:*\n{retrospective['action_item']}\n\n"
                f"*감정 점수:* {retrospective.get('emotion_score', '없음')}\n"
                f"*감정 이유:* {retrospective.get('emotion_reason', '없음')}"
            ),
            DividerBlock(),
            SectionBlock(text="수행할 작업을 선택하세요:"),
            ActionsBlock(
                block_id="admin_actions",
                elements=[
                    ButtonElement(
                        text="삭제하기",
                        value=f"{retrospective_id}|{slack_channel}|{slack_ts}",
                        action_id="delete_retrospective",
                        style="danger",
                    ),
                    ButtonElement(
                        text="수정하기",
                        value=f"{retrospective_id}",
                        action_id="edit_retrospective",
                    ),
                ],
            ),
        ]

        await ack()

        # 관리 작업 선택 모달 열기
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="회고 관리",
                callback_id="admin_action",
                close="닫기",
                blocks=blocks,
                private_metadata=f"{retrospective_id}|{slack_channel}|{slack_ts}",
            ),
        )

    except BotException:
        # 숫자가 아닌 ID가 입력된 경우
        await ack(
            response_action="errors",
            errors={"retrospective_id": "유효한 회고 ID를 입력하세요"},
        )
    except Exception as e:
        logger.error(f"회고 관리 실패 - User: {user_id}, Error: {str(e)}")
        await ack(
            response_action="errors",
            errors={"retrospective_id": f"오류 발생: {str(e)}"},
        )


async def handle_admin_action_delete(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, action: dict
):
    """회고 삭제 액션 처리"""
    await ack()

    user_id = body["user"]["id"]

    # 관리자 권한 확인
    if user_id not in settings.ADMIN_IDS:
        return

    try:
        # 액션 값에서 정보 추출 (retrospective_id|slack_channel|slack_ts)
        value_parts = action["value"].split("|")
        retrospective_id = int(value_parts[0])
        slack_channel = value_parts[1]
        slack_ts = value_parts[2]

        # 삭제 확인 모달 생성
        blocks = [
            SectionBlock(
                text=f"*회고 ID {retrospective_id}를 삭제하시겠습니까?*\n\n"
                f"이 작업은 되돌릴 수 없으며, 회고 데이터와 슬랙 메시지가 모두 삭제됩니다."
            ),
        ]

        # 삭제 확인 모달 표시
        await client.views_update(
            view_id=body["view"]["id"],
            view=View(
                type="modal",
                title="회고 삭제",
                callback_id="admin_delete_retrospective",
                submit="삭제",
                close="취소",
                blocks=blocks,
                private_metadata=f"{retrospective_id}|{slack_channel}|{slack_ts}",
            ),
        )

    except Exception as e:
        logger.error(f"회고 삭제 액션 처리 실패 - User: {user_id}, Error: {str(e)}")
        await client.views_push(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="오류",
                close="닫기",
                blocks=[
                    SectionBlock(
                        text=f"회고 삭제 처리 중 오류가 발생했습니다: {str(e)}"
                    )
                ],
            ),
        )


async def handle_admin_action_edit(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, action: dict
):
    """회고 수정 액션 처리"""
    user_id = body["user"]["id"]

    # 관리자 권한 확인
    if user_id not in settings.ADMIN_IDS:
        await ack()
        return

    await ack()

    try:
        # 액션 값에서 정보 추출
        retrospective_id = int(action["value"])

        # 회고 데이터 가져오기
        retrospective = await get_retrospective_by_id(retrospective_id)

        # 회고 수정 모달 생성
        blocks = [
            InputBlock(
                block_id="good_points",
                label="잘했고 좋았던 점을 알려주세요",
                element=PlainTextInputElement(
                    action_id="good_points_input",
                    initial_value=retrospective["good_points"],
                    multiline=True,
                    min_length=1,
                    max_length=500,
                ),
            ),
            InputBlock(
                block_id="improvements",
                label="아쉽고 개선하고 싶은 점을 알려주세요",
                element=PlainTextInputElement(
                    action_id="improvements_input",
                    initial_value=retrospective["improvements"],
                    multiline=True,
                    min_length=1,
                    max_length=500,
                ),
            ),
            InputBlock(
                block_id="learnings",
                label="새롭게 배운 점을 알려주세요",
                element=PlainTextInputElement(
                    action_id="learnings_input",
                    initial_value=retrospective["learnings"],
                    multiline=True,
                    min_length=1,
                    max_length=500,
                ),
            ),
            InputBlock(
                block_id="action_item",
                label="해볼만한 액션 아이템을 알려주세요",
                element=PlainTextInputElement(
                    action_id="action_item_input",
                    initial_value=retrospective["action_item"],
                    multiline=True,
                    min_length=1,
                    max_length=500,
                ),
            ),
            InputBlock(
                optional=True,
                block_id="emotion_score",
                label="오늘의 감정점수를 알려주세요 (1-10)",
                element=NumberInputElement(
                    action_id="emotion_score_input",
                    is_decimal_allowed=False,
                    min_value="1",
                    max_value="10",
                    initial_value=(
                        str(retrospective.get("emotion_score", ""))
                        if retrospective.get("emotion_score")
                        else None
                    ),
                ),
            ),
            InputBlock(
                optional=True,
                block_id="emotion_reason",
                label="감정점수 이유를 알려주세요",
                element=PlainTextInputElement(
                    action_id="emotion_reason_input",
                    initial_value=retrospective.get("emotion_reason", ""),
                    multiline=True,
                    min_length=1,
                    max_length=500,
                ),
            ),
        ]

        # 수정 모달 열기
        await client.views_update(
            view_id=body["view"]["id"],
            view=View(
                type="modal",
                title="회고 수정",
                callback_id="admin_edit_retrospective",
                submit="저장",
                close="취소",
                blocks=blocks,
                private_metadata=f"{retrospective_id}|{retrospective['slack_channel']}|{retrospective['slack_ts']}",
            ),
        )

    except Exception as e:
        logger.error(f"회고 수정 액션 처리 실패 - User: {user_id}, Error: {str(e)}")
        await client.views_push(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="오류",
                close="닫기",
                blocks=[
                    SectionBlock(
                        text=f"회고 수정 처리 중 오류가 발생했습니다: {str(e)}"
                    )
                ],
            ),
        )


async def handle_view_admin_delete_retrospective(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, view: ViewType
):
    """회고 삭제 모달 처리"""
    from database.retrospective import delete_retrospective

    await ack()

    user_id = body["user"]["id"]

    # 관리자 권한 확인
    if user_id not in settings.ADMIN_IDS:
        return

    try:
        # 액션 값에서 정보 추출
        # value_parts = action["value"].split("|")

        metadata_parts = view["private_metadata"].split("|")
        retrospective_id = int(metadata_parts[0])
        slack_channel = metadata_parts[1]
        slack_ts = metadata_parts[2]

        # 회고 데이터 삭제
        deleted = await delete_retrospective(retrospective_id)

        if deleted:
            # 슬랙 메시지 삭제
            try:
                await client.chat_delete(channel=slack_channel, ts=slack_ts)
                message = f"회고 ID {retrospective_id}가 성공적으로 삭제되었습니다.\n슬랙 메시지도 함께 삭제되었습니다."
            except Exception as e:
                message = f"회고 ID {retrospective_id}가 성공적으로 삭제되었습니다.\n슬랙 메시지 삭제 중 오류 발생: {str(e)}"
                logger.error(
                    f"슬랙 메시지 삭제 실패 - Channel: {slack_channel}, TS: {slack_ts}, Error: {str(e)}"
                )
        else:
            message = f"회고 ID {retrospective_id} 삭제 실패"

        # 결과 알림
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="회고 삭제 결과",
                close="닫기",
                blocks=[SectionBlock(text=message)],
            ),
        )

    except Exception as e:
        logger.error(f"회고 삭제 확인 처리 실패 - User: {user_id}, Error: {str(e)}")
        await client.views_push(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="오류",
                close="닫기",
                blocks=[
                    SectionBlock(
                        text=f"회고 삭제 처리 중 오류가 발생했습니다: {str(e)}"
                    )
                ],
            ),
        )


async def handle_view_admin_edit_retrospective(
    ack: AsyncAck, body: ViewBodyType, client: AsyncWebClient, view: ViewType
):
    """회고 수정 모달 제출 처리"""
    from database.retrospective import update_retrospective

    user_id = body["user"]["id"]

    # 관리자 권한 확인
    if user_id not in settings.ADMIN_IDS:
        await ack(
            response_action="errors",
            errors={"good_points": "관리자 권한이 없습니다."},
        )
        return

    try:
        # 모달에서 입력된 값 추출
        values = view["state"]["values"]

        # 메타데이터에서 정보 추출
        metadata_parts = view["private_metadata"].split("|")
        retrospective_id = int(metadata_parts[0])
        slack_channel = metadata_parts[1]
        slack_ts = metadata_parts[2]

        # 수정할 데이터 준비
        update_data = {
            "good_points": values["good_points"]["good_points_input"]["value"],
            "improvements": values["improvements"]["improvements_input"]["value"],
            "learnings": values["learnings"]["learnings_input"]["value"],
            "action_item": values["action_item"]["action_item_input"]["value"],
        }

        # 감정 점수 처리
        if "emotion_score" in values and values["emotion_score"][
            "emotion_score_input"
        ].get("value"):
            update_data["emotion_score"] = int(
                values["emotion_score"]["emotion_score_input"]["value"]
            )

        # 감정 이유 처리
        if (
            "emotion_reason" in values
            and values["emotion_reason"]["emotion_reason_input"]["value"]
        ):
            update_data["emotion_reason"] = values["emotion_reason"][
                "emotion_reason_input"
            ]["value"]

        # 회고 데이터 업데이트
        updated_retro = await update_retrospective(retrospective_id, update_data)

        # 슬랙 메시지 업데이트
        try:
            # 회고 작성자 정보 및 감정 점수 가져오기
            retro_user = updated_retro["user_id"]
            session_name = updated_retro["session_name"]

            # 메시지 블록 생성
            blocks = [
                SectionBlock(
                    text=f"*<@{retro_user}>님이 `{session_name}` 회고를 공유했어요! 🤗*"
                ),
                DividerBlock(),
                ContextBlock(
                    elements=[{"type": "mrkdwn", "text": "*잘했고 좋았던 점* 🌟"}]
                ),
                SectionBlock(text=update_data["good_points"]),
                DividerBlock(),
                ContextBlock(
                    elements=[
                        {"type": "mrkdwn", "text": "*아쉽고 개선하고 싶은 점* 🔧"}
                    ]
                ),
                SectionBlock(text=update_data["improvements"]),
                DividerBlock(),
                ContextBlock(
                    elements=[{"type": "mrkdwn", "text": "*새롭게 배운 점* 💡"}]
                ),
                SectionBlock(text=update_data["learnings"]),
                DividerBlock(),
                ContextBlock(
                    elements=[{"type": "mrkdwn", "text": "*해볼만한 액션 아이템* 🚀"}]
                ),
                SectionBlock(text=update_data["action_item"]),
            ]

            # 감정 점수가 있다면 추가
            if "emotion_score" in update_data:
                blocks.extend(
                    [
                        DividerBlock(),
                        SectionBlock(
                            text=f"*오늘의 감정점수* :bar_chart: {update_data['emotion_score']}/10"
                        ),
                    ]
                )

                # 감정 이유가 입력되었다면 추가
                if "emotion_reason" in update_data and update_data["emotion_reason"]:
                    blocks.append(SectionBlock(text=update_data["emotion_reason"]))

            # 업데이트된 블록으로 메시지 수정
            await client.chat_update(
                channel=slack_channel,
                ts=slack_ts,
                blocks=blocks,
                text=f"*<@{retro_user}>님의 회고 (수정됨)*",
            )

            message = "회고가 성공적으로 수정되었습니다.\n슬랙 메시지도 함께 업데이트되었습니다."
        except Exception as e:
            message = f"회고가 성공적으로 수정되었습니다.\n슬랙 메시지 업데이트 중 오류 발생: {str(e)}"
            logger.error(
                f"슬랙 메시지 업데이트 실패 - Channel: {slack_channel}, TS: {slack_ts}, Error: {str(e)}"
            )

        await ack()

        # 결과 알림
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=View(
                type="modal",
                title="회고 수정 결과",
                close="닫기",
                blocks=[SectionBlock(text=message)],
            ),
        )

    except Exception as e:
        logger.error(f"회고 수정 제출 처리 실패 - User: {user_id}, Error: {str(e)}")
        await ack(
            response_action="errors",
            errors={"good_points": f"오류 발생: {str(e)}"},
        )
