"""Shared preflight for slash commands and announcement buttons."""

from config import settings
from database import check_user_submitted_this_session
from database.sessions import get_session
from slack.events.view_retrospective_submit import _is_test_session
from slack.ephemeral import post_ephemeral
from utils import get_current_session_info, tz_now


async def submission_preflight(user_id: str, session_name: str) -> str | None:
    if not isinstance(session_name, str) or not session_name:
        return "회고 회차를 찾지 못했어요. 최신 공지나 /공유로 다시 시작해 주세요."
    if _is_test_session(session_name):
        return None
    session = get_session(session_name)
    if session is None:
        return "회고 회차를 찾지 못했어요. 최신 공지나 /공유로 다시 시작해 주세요."
    current = get_current_session_info()
    if not current[3] or current[1] != session_name or tz_now() >= session["due_at"]:
        return f"`{session_name}`은 현재 회고 제출 기간이 아니에요. 최신 공지를 확인해 주세요."
    if user_id not in settings.SUBMISSION_DESTINATIONS:
        return "팀 배정이 등록되지 않았어요. 관리자에게 문의해주세요."
    if await check_user_submitted_this_session(user_id=user_id, session_name=session_name):
        return f"<@{user_id}>님은 이미 `{session_name}` 회고를 공유했어요! 다시 작성하지 않아도 됩니다. 🤗"
    return None


async def open_submission_entry(
    *, client, trigger_id: str, user_id: str, channel_id: str,
    session_name: str | None = None,
) -> None:
    # None means the slash command; empty/invalid button metadata must not
    # silently select a different session.
    if session_name is None:
        session_name = get_current_session_info()[1]
    error = await submission_preflight(user_id, session_name)
    if error:
        await post_ephemeral(client, channel=channel_id, user=user_id, text=error)
        return

    from slack.events.test_announcement import build_method_selection_view, _team_channels

    channel_options = None
    if user_id in settings.SUBMISSION_CHANNEL_CHOOSER_IDS:
        channel_options = []
        for team_channel in _team_channels():
            response = await client.conversations_info(channel=team_channel)
            channel_options.append({
                "text": {"type": "plain_text", "text": f"#{response['channel']['name']}"},
                "value": team_channel,
            })
    await client.views_open(
        trigger_id=trigger_id,
        view=build_method_selection_view({
            "channel_id": channel_id,
            "session_name": session_name,
            "test_mode": _is_test_session(session_name),
        }, channel_options),
    )
