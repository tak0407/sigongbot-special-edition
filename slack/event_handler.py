import re
import traceback
from typing import Callable
from config import settings
from slack_bolt.async_app import AsyncApp as SlackBoltAsyncApp

from loguru import logger
from slack_bolt.request import BoltRequest
from slack_bolt.response import BoltResponse
from slack_sdk.models.blocks import SectionBlock
from slack_sdk.models.views import View

from alerts import build_alert, send_alert
from exception import BotException
from slack.events.channel_created import handle_channel_created
from slack.events.member_joined_channel import handle_member_joined_channel
from slack.events.reaction_added import handle_reaction_added
from slack.events.view_invite_channel import (
    handle_action_view_invite_channel,
    handle_invite_channel,
)
from slack.events.command_retrospective import handle_command_retrospective
from slack.events.command_admin import handle_command_admin
from slack.events.message import handle_message
from slack.events.view_retrospective_submit import handle_view_retrospective_submit
from slack.events.view_admin_menu import (
    handle_view_admin_menu,
    handle_admin_action_delete,
    handle_admin_action_edit,
    handle_view_admin_delete_retrospective,
    handle_view_admin_edit_retrospective,
)
from slack.events.command_my_retrospectives import handle_command_my_retrospectives
from slack.events.command_suggestion import (
    OPEN_SUGGESTION_ACTION_ID,
    handle_command_suggestion,
    handle_open_suggestion_modal,
    handle_view_suggestion_submit,
)
from slack.events.action_view_retrospective_detail import (
    handle_action_view_retrospective_detail,
)
from slack.events.test_announcement import (
    handle_post_test_announcement,
    handle_start_from_announcement,
    handle_method_select,
    handle_guided_previous,
    handle_open_guided_result,
    handle_guided_submit,
    handle_guided_skip,
)
from slack.events.online_retro_meeting import (
    handle_online_retro_attendance,
    handle_open_online_retro_meeting,
    handle_open_online_retro_photo_upload,
    handle_online_retro_photo_submit,
    handle_start_online_retro_photo,
    handle_start_online_retro_sharing,
)
from slack.events.online_retro_poll import handle_open_time_poll, handle_time_poll_submit
from slack.ephemeral import handle_dismiss_ephemeral


# Slack events are received exclusively through Socket Mode, so HTTP request
# signature verification (and therefore SLACK_SIGNING_SECRET) is not required.
app = SlackBoltAsyncApp(request_verification_enabled=False)


@app.middleware
async def log_event_middleware(
    req: BoltRequest,
    resp: BoltResponse,
    next: Callable,
) -> None:
    """이벤트 로깅 미들웨어"""
    body = req.body or {}
    action_ids = [
        action.get("action_id") for action in body.get("actions", []) if action
    ]
    logger.info(
        "Slack 요청 수신 - type={}, command={}, callback_id={}, actions={}",
        body.get("type"),
        body.get("command"),
        body.get("view", {}).get("callback_id"),
        action_ids,
    )
    await next()


@app.error
async def handle_error(error, body):
    """이벤트 핸들러에서 발생한 에러 처리"""
    body = body or {}
    # 아래에서 더 자세한 알림을 직접 보내므로 로그 싱크가 중복으로 알리지 않게 한다.
    logger.bind(alert=False).error(f'"{str(error)}"')
    trace = traceback.format_exc()
    logger.bind(alert=False).debug(
        "Slack 처리 오류 - type={}, command={}, callback_id={}, error={}",
        body.get("type"),
        body.get("command"),
        body.get("view", {}).get("callback_id"),
        trace,
    )

    # 알림 워크스페이스 전송을 가장 먼저 시도합니다.
    # 봇 워크스페이스의 Slack 호출이 실패해도 오류가 묻히지 않아야 합니다.
    await send_alert(
        build_alert(
            "Slack 이벤트 처리 오류",
            {
                "종류": body.get("type"),
                "명령": body.get("command"),
                "콜백": (body.get("view") or {}).get("callback_id"),
                "액션": ",".join(
                    action.get("action_id", "")
                    for action in body.get("actions", [])
                    if action
                ),
                "사용자": (body.get("user") or {}).get("id"),
            },
            error=error,
            detail=trace,
        ),
        dedup_key=(
            f"slack-event:{type(error).__name__}:{body.get('type')}"
            f":{body.get('command')}"
        ),
    )

    # 사용자에게 에러를 알립니다.
    if re.search(r"[\u3131-\uD79D]", str(error)):
        # 한글로 핸들링하는 메시지만 사용자에게 전송합니다.
        message = str(error)
    else:
        message = "예기치 못한 오류가 발생했어요."

    text = f"🥲 {message}\n\n👉🏼 문제가 해결되지 않는다면 <#{settings.SUPPORT_CHANNEL}> 채널로 문의해주세요."
    try:
        if trigger_id := body.get("trigger_id"):
            await app.client.views_open(
                trigger_id=trigger_id,
                view=View(
                    type="modal",
                    title={"type": "plain_text", "text": "잠깐!"},
                    blocks=[SectionBlock(text=text)],
                ),
            )
    except Exception as notify_error:
        # trigger_id는 3초 만에 만료되므로 안내 실패로 관리자 알림까지 막지 않는다.
        logger.bind(alert=False).warning(
            "사용자에게 오류 안내를 전달하지 못했습니다 - {}", notify_error
        )

    # 관리자에게 에러를 알립니다.
    prefix = "🫢" if isinstance(error, BotException) else "⛈️ 핸들링이 필요한 에러입니다. 🫢"
    try:
        await app.client.chat_postMessage(
            channel=settings.ADMIN_CHANNEL,
            # Slack 메시지 길이 제한(4000자)에 걸리면 이 호출 자체가 실패한다.
            text=f"{prefix}: {error=} 🕊️: {trace=}"[:3500],
        )
    except Exception as admin_error:
        logger.bind(alert=False).warning(
            "관리자 채널에 오류를 알리지 못했습니다 - {}", admin_error
        )


# message
app.event("message")(handle_message)

# member_joined_channel
app.event("member_joined_channel")(handle_member_joined_channel)

# channel_created
app.event("channel_created")(handle_channel_created)

# reaction_added
app.event("reaction_added")(handle_reaction_added)

# channel_join
app.action("invite_channel")(handle_invite_channel)
app.view("invite_channel_view")(handle_action_view_invite_channel)

# retrospective
app.command("/공유")(handle_command_retrospective)
app.view("retrospective_submit")(handle_view_retrospective_submit)
app.action("post_test_announcement")(handle_post_test_announcement)
app.action("start_retrospective_from_announcement")(
    handle_start_from_announcement
)
app.action(re.compile(r"^select_retrospective_method_"))(handle_method_select)
app.view("select_retrospective_method")(handle_method_select)
app.action("guided_skip_section")(handle_guided_skip)
app.action("guided_previous_question")(handle_guided_previous)
app.action("open_guided_result")(handle_open_guided_result)
app.view("guided_retrospective_submit")(handle_guided_submit)
app.action("attend_online_retro_meeting")(handle_online_retro_attendance)
app.action("open_online_retro_meeting")(handle_open_online_retro_meeting)
app.action("start_online_retro_sharing")(handle_start_online_retro_sharing)
app.action("start_online_retro_photo")(handle_start_online_retro_photo)
app.action("open_online_retro_photo_upload")(
    handle_open_online_retro_photo_upload
)
app.view("online_retro_photo_submit")(handle_online_retro_photo_submit)
app.action("open_online_retro_time_poll")(handle_open_time_poll)
app.view("online_retro_time_poll_submit")(handle_time_poll_submit)
app.action("dismiss_ephemeral")(handle_dismiss_ephemeral)

# my retrospectives
app.command("/내회고")(handle_command_my_retrospectives)
app.action("view_retrospective_detail")(handle_action_view_retrospective_detail)

# bot improvement suggestions
app.command("/제안")(handle_command_suggestion)
app.action(OPEN_SUGGESTION_ACTION_ID)(handle_open_suggestion_modal)
app.view("bot_improvement_suggestion_submit")(handle_view_suggestion_submit)

# admin
app.command("/관리자")(handle_command_admin)  # 관리자 메뉴 호출
app.view("admin_menu")(handle_view_admin_menu)  # 관리자 메뉴 출력
app.view("admin_edit_retrospective")(
    handle_view_admin_edit_retrospective
)  # 회고 수정 제출 처리
app.view("admin_delete_retrospective")(
    handle_view_admin_delete_retrospective
)  # 회고 삭제 모달 처리

# 회고 관리 액션
app.action("edit_retrospective")(handle_admin_action_edit)  # 회고 수정 버튼
app.action("delete_retrospective")(handle_admin_action_delete)  # 회고 삭제 버튼
