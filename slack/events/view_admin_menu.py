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
    NumberInputElement,
)
from config import settings
from database.retrospective import get_retrospective_by_id
from retrospective_content import (
    ESSAY_MIN_LENGTH,
    ESSAY_MAX_LENGTH,
    ESSAY_TITLE_MAX_LENGTH,
    STRUCTURED_TEXT_MAX_LENGTH,
    validate_essay,
)
from slack.retrospective_blocks import build_retrospective_blocks


STRUCTURED_EDIT_FIELDS = (
    ("good_points", "잘했고 좋았던 점을 알려주세요"),
    ("improvements", "아쉽고 개선하고 싶은 점을 알려주세요"),
    ("learnings", "새롭게 배운 점을 알려주세요"),
    ("action_item", "해볼만한 액션 아이템을 알려주세요"),
)


def _edit_text_input(record, field, label, *, optional=False, min_length=1,
                     max_length=STRUCTURED_TEXT_MAX_LENGTH, multiline=True):
    return InputBlock(
        block_id=field,
        label=label,
        optional=optional,
        element=PlainTextInputElement(
            action_id=f"{field}_input",
            initial_value=record.get(field) or None,
            multiline=multiline,
            min_length=min_length,
            max_length=max_length,
        ),
    )


def _admin_edit_blocks(record):
    if record.get("content_format", "structured") == "essay":
        blocks = [
            _edit_text_input(
                record, "essay_title", "에세이 제목 (선택)", optional=True,
                max_length=ESSAY_TITLE_MAX_LENGTH, multiline=False,
            ),
            _edit_text_input(
                record, "essay_body", "에세이 본문", min_length=ESSAY_MIN_LENGTH,
                max_length=ESSAY_MAX_LENGTH,
            ),
        ]
    else:
        blocks = [_edit_text_input(record, field, label) for field, label in STRUCTURED_EDIT_FIELDS]
    blocks.extend([
        InputBlock(
            optional=True,
            block_id="emotion_score",
            label="오늘의 감정점수를 알려주세요 (1-10)",
            element=NumberInputElement(
                action_id="emotion_score_input", is_decimal_allowed=False,
                min_value="1", max_value="10",
                initial_value=str(record["emotion_score"]) if record.get("emotion_score") else None,
            ),
        ),
        _edit_text_input(record, "emotion_reason", "감정점수 이유를 알려주세요", optional=True),
    ])
    return blocks


def _existing_input(values, preferred=None):
    """검증 실패 때 현재 모달에 있는 입력에 오류를 연결한다."""
    for field in (preferred, "essay_body", "good_points"):
        if field and field in values:
            return field
    return next(iter(values), "good_points")


def _input_value(values, field):
    return values.get(field, {}).get(f"{field}_input", {}).get("value")


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
            *build_retrospective_blocks(retrospective, heading="*회고 내용*"),
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

        blocks = _admin_edit_blocks(retrospective)

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
    """기존 회고 형식을 유지하고 DB 저장 후 Slack 본문을 수정한다."""
    from database.retrospective import update_retrospective

    values = view.get("state", {}).get("values", {})
    error_field = _existing_input(values)
    if body["user"]["id"] not in settings.ADMIN_IDS:
        await ack(response_action="errors", errors={error_field: "관리자 권한이 없습니다."})
        return

    acknowledged = False
    try:
        retrospective_id = int(view["private_metadata"].split("|")[0])
        record = await get_retrospective_by_id(retrospective_id)
        if not record:
            raise ValueError("회고를 찾을 수 없습니다.")
        is_essay = record.get("content_format", "structured") == "essay"
        error_field = _existing_input(values, "essay_body" if is_essay else "good_points")
        errors = {}
        if is_essay:
            title = _input_value(values, "essay_title")
            if title is not None and len(title) > ESSAY_TITLE_MAX_LENGTH:
                errors[_existing_input(values, "essay_title")] = f"제목은 {ESSAY_TITLE_MAX_LENGTH}자 이하로 작성해주세요."
            try:
                title, essay_body = validate_essay(title, _input_value(values, "essay_body"))
            except ValueError as error:
                errors[error_field] = str(error)
            if not errors:
                update_data = {"essay_title": title, "essay_body": essay_body}
        else:
            update_data = {}
            for field, _ in STRUCTURED_EDIT_FIELDS:
                if field not in values:
                    errors[error_field] = "원래 회고 형식의 수정 창을 다시 열어주세요."
                    continue
                value = _input_value(values, field) or ""
                if len(value) > STRUCTURED_TEXT_MAX_LENGTH:
                    errors[field] = f"{STRUCTURED_TEXT_MAX_LENGTH:,}자 이하로 작성해주세요."
                update_data[field] = value
        if "emotion_score" in values:
            raw_score = _input_value(values, "emotion_score")
            try:
                score = int(raw_score) if raw_score not in (None, "") else None
                if score is not None and not 1 <= score <= 10:
                    raise ValueError
                if not errors:
                    update_data["emotion_score"] = score
            except (TypeError, ValueError):
                errors["emotion_score"] = "감정점수는 1~10 사이의 정수로 입력해주세요."
        if "emotion_reason" in values:
            reason = _input_value(values, "emotion_reason")
            if reason is not None and len(reason) > STRUCTURED_TEXT_MAX_LENGTH:
                errors["emotion_reason"] = f"{STRUCTURED_TEXT_MAX_LENGTH:,}자 이하로 작성해주세요."
            elif not errors:
                update_data["emotion_reason"] = reason or None
        if errors:
            await ack(response_action="errors", errors=errors)
            return

        updated = await update_retrospective(retrospective_id, update_data)
        try:
            # 기존 이미지가 사라지지 않도록 저장된 메시지 주소에서 첨부를 읽는다.
            history = await client.conversations_history(
                channel=updated["slack_channel"], latest=updated["slack_ts"],
                inclusive=True, limit=1,
            )
            original = next((message for message in history.get("messages", [])
                             if message.get("ts") == updated["slack_ts"]), None)
            if original is None:
                raise ValueError("기존 Slack 메시지를 확인하지 못해 본문 갱신을 보류했습니다.")
            blocks = build_retrospective_blocks(
                updated,
                heading=f"*<@{updated['user_id']}>님이 `{updated['session_name']}` 회고를 공유했어요! 🤗*",
            )
            images = [block for block in original.get("blocks", []) if block.get("type") == "image"]
            if images:
                blocks.extend([DividerBlock(), *images])
            await client.chat_update(
                channel=updated["slack_channel"], ts=updated["slack_ts"],
                blocks=blocks, text=f"*<@{updated['user_id']}>님의 회고 (수정됨)*",
            )
            message = "회고가 성공적으로 수정되었습니다.\n슬랙 메시지도 함께 업데이트되었습니다."
        except Exception as error:
            message = "회고는 저장되었습니다.\n슬랙 메시지 업데이트 중 오류가 발생했습니다. 다시 수정해주세요."
            logger.bind(alert=False).warning(
                "회고 수정 후 Slack 갱신 실패 - id={}, error_type={}",
                retrospective_id, type(error).__name__,
            )
        await ack()
        acknowledged = True
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=View(type="modal", title="회고 수정 결과", close="확인", blocks=[SectionBlock(text=message)]),
        )
    except Exception as error:
        logger.bind(alert=False).warning("회고 수정 실패 - error_type={}", type(error).__name__)
        if not acknowledged:
            await ack(response_action="errors", errors={error_field: "회고를 저장하지 못했습니다. 작성 내용을 확인하고 다시 시도해주세요."})
