"""온라인 회고 모임 공지, 출석 버튼, 중복 방지 스케줄러."""

import asyncio
import datetime
import json

from loguru import logger
from slack_bolt.async_app import AsyncAck
from slack_sdk.web.async_client import AsyncWebClient

from config import OnlineRetroMeeting, settings
from database.online_retro_attendance import (
    has_attended,
    list_attendees,
    record_attendance,
)
from database.retrospective import get_submitted_user_ids
from database.scheduled_announcements import announcement_sent, mark_announcement_sent
from utils import tz_now

LATE_ANNOUNCEMENT_GRACE = datetime.timedelta(hours=1)
TIME_UP_GRACE = datetime.timedelta(hours=2)
_phase_lock = asyncio.Lock()


def _announcement_key(meeting: OnlineRetroMeeting) -> str:
    return (
        "online-retro:"
        f"{meeting.session_name}:{meeting.channel}:{int(meeting.starts_at.timestamp())}"
    )


def _phase_key(meeting: OnlineRetroMeeting, phase: str) -> str:
    return f"{_announcement_key(meeting)}:{phase}"


def _meeting(session_name: str, team_channel: str) -> OnlineRetroMeeting | None:
    return next(
        (
            meeting
            for meeting in settings.ONLINE_RETRO_MEETINGS
            if meeting.session_name == session_name and meeting.channel == team_channel
        ),
        None,
    )


def _action_value(meeting: OnlineRetroMeeting) -> str:
    return json.dumps(
        {"session_name": meeting.session_name, "team_channel": meeting.channel},
        ensure_ascii=False,
    )


def build_online_retro_blocks(meeting: OnlineRetroMeeting) -> list[dict]:
    starts_at = int(meeting.starts_at.timestamp())
    attendance_value = _action_value(meeting)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "<!here>\n\n"
                    f"*{meeting.session_name} 온라인 회고 모임 안내* 💻\n"
                    f"모임은 <!date^{starts_at}^{{date_long_pretty}} {{time}}|"
                    f"{meeting.starts_at.isoformat()}>에 시작해요.\n"
                    f"모이면 {meeting.writing_minutes}분 동안 각자 주간 회고를 작성한 뒤 돌아가며 공유합니다.\n"
                    "참여한다면 아래에서 출석을 남기고 모임에 입장해 주세요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "attend_online_retro_meeting",
                    "text": {"type": "plain_text", "text": "출석 체크"},
                    "style": "primary",
                    "value": attendance_value,
                },
                {
                    "type": "button",
                    "action_id": "open_online_retro_meeting",
                    "text": {"type": "plain_text", "text": "모임 입장"},
                    "url": meeting.url,
                },
            ],
        },
    ]


async def post_online_retro_announcement(
    client: AsyncWebClient, meeting: OnlineRetroMeeting
) -> None:
    key = _announcement_key(meeting)
    if await announcement_sent(key):
        return
    await client.chat_postMessage(
        channel=meeting.channel,
        text=f"{meeting.session_name} 온라인 회고 모임 안내",
        blocks=build_online_retro_blocks(meeting),
    )
    await mark_announcement_sent(key)


def build_writing_blocks(meeting: OnlineRetroMeeting) -> list[dict]:
    ends_at = meeting.starts_at + datetime.timedelta(minutes=meeting.writing_minutes)
    submission = json.dumps(
        {
            "channel_id": meeting.channel,
            "session_name": meeting.session_name,
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
                    f"*{meeting.session_name} 회고 작성을 시작할게요* ✍️\n"
                    f"{meeting.writing_minutes}분 동안 이번 주를 돌아보고 회고를 작성해 주세요. "
                    f"<!date^{int(ends_at.timestamp())}^{{time}}|작성 종료 시각>에 마무리 알림을 드릴게요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "start_retrospective_from_announcement",
                    "text": {"type": "plain_text", "text": "회고 작성하기"},
                    "style": "primary",
                    "value": submission,
                },
                {
                    "type": "button",
                    "action_id": "open_online_retro_meeting",
                    "text": {"type": "plain_text", "text": "모임 입장"},
                    "url": meeting.url,
                },
            ],
        },
    ]


async def post_writing_phase(client: AsyncWebClient, meeting: OnlineRetroMeeting) -> None:
    key = _phase_key(meeting, "writing")
    if await announcement_sent(key):
        return
    await client.chat_postMessage(
        channel=meeting.channel,
        text=f"{meeting.session_name} 회고 작성 시간입니다.",
        blocks=build_writing_blocks(meeting),
    )
    await mark_announcement_sent(key)


def build_time_up_blocks(meeting: OnlineRetroMeeting) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{meeting.writing_minutes}분 회고 작성 시간이 끝났어요* ⏰\n"
                    "작성 중인 내용을 마무리한 뒤, 모두 준비되면 운영자가 공유를 시작해 주세요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "start_online_retro_sharing",
                    "text": {"type": "plain_text", "text": "회고 공유 시작"},
                    "style": "primary",
                    "value": _action_value(meeting),
                }
            ],
        },
    ]


async def post_writing_time_up(
    client: AsyncWebClient, meeting: OnlineRetroMeeting
) -> None:
    key = _phase_key(meeting, "writing-time-up")
    if await announcement_sent(key):
        return
    await client.chat_postMessage(
        channel=meeting.channel,
        text=f"{meeting.session_name} 회고 작성 시간이 끝났어요.",
        blocks=build_time_up_blocks(meeting),
    )
    await mark_announcement_sent(key)


async def handle_online_retro_attendance(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    metadata = json.loads(body["actions"][0]["value"])
    session_name = str(metadata.get("session_name") or "").strip()
    team_channel = str(metadata.get("team_channel") or "").strip()
    if not session_name or _meeting(session_name, team_channel) is None:
        raise ValueError("현재 설정된 온라인 회고 모임을 찾을 수 없어요.")
    user_id = body["user"]["id"]
    created = await record_attendance(
        session_name=session_name, team_channel=team_channel, user_id=user_id
    )
    await client.chat_postEphemeral(
        channel=body["channel"]["id"],
        user=user_id,
        text=(
            f"`{session_name}` 온라인 회고 모임 출석을 기록했어요."
            if created
            else f"`{session_name}` 온라인 회고 모임 출석은 이미 기록되어 있어요."
        ),
    )


async def handle_open_online_retro_meeting(ack: AsyncAck) -> None:
    """URL 버튼 상호작용을 즉시 확인한다."""
    await ack()


async def _participant_action(
    *, body: dict, client: AsyncWebClient, action_name: str
) -> OnlineRetroMeeting | None:
    user_id = body["user"]["id"]
    metadata = json.loads(body["actions"][0]["value"])
    meeting = _meeting(
        str(metadata.get("session_name") or "").strip(),
        str(metadata.get("team_channel") or "").strip(),
    )
    if meeting is None:
        raise ValueError("현재 설정된 온라인 회고 모임을 찾을 수 없어요.")
    if not await has_attended(
        session_name=meeting.session_name,
        team_channel=meeting.channel,
        user_id=user_id,
    ):
        await client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text=f"먼저 출석 체크를 해야 {action_name}을 진행할 수 있어요.",
        )
        return None
    return meeting


async def handle_start_online_retro_sharing(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    meeting = await _participant_action(
        body=body, client=client, action_name="회고 공유 시작"
    )
    if meeting is None:
        return
    async with _phase_lock:
        key = _phase_key(meeting, "sharing")
        if await announcement_sent(key):
            await client.chat_postEphemeral(
                channel=meeting.channel,
                user=body["user"]["id"],
                text="회고 공유 단계는 이미 시작됐어요.",
            )
            return
        attendees = await list_attendees(meeting.session_name, meeting.channel)
        submitted = await get_submitted_user_ids(meeting.session_name)
        if attendees:
            order = "\n".join(
                f"{number}. <@{row['user_id']}> "
                f"{'✅ 작성 완료' if row['user_id'] in submitted else '✍️ 작성 확인 필요'}"
                for number, row in enumerate(attendees[:50], 1)
            )
        else:
            order = "출석 체크한 참여자가 아직 없습니다."
        await client.chat_postMessage(
            channel=meeting.channel,
            text=f"{meeting.session_name} 회고 공유를 시작합니다.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*이제 한 명씩 돌아가며 회고를 공유할게요* 🎙️\n"
                            "출석 체크 순서이며, 상황에 맞게 순서를 바꿔도 괜찮아요.\n\n"
                            f"{order}"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "start_online_retro_photo",
                            "text": {"type": "plain_text", "text": "공유 완료 · 인증샷"},
                            "style": "primary",
                            "value": _action_value(meeting),
                        }
                    ],
                },
            ],
        )
        await mark_announcement_sent(key)


async def handle_start_online_retro_photo(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    meeting = await _participant_action(
        body=body, client=client, action_name="인증샷 단계 시작"
    )
    if meeting is None:
        return
    async with _phase_lock:
        key = _phase_key(meeting, "photo")
        if await announcement_sent(key):
            await client.chat_postEphemeral(
                channel=meeting.channel,
                user=body["user"]["id"],
                text="인증샷 안내는 이미 보냈어요.",
            )
            return
        await client.chat_postMessage(
            channel=meeting.channel,
            text=f"{meeting.session_name} 온라인 회고 모임 인증샷을 남겨 주세요.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*마치기 전에 오늘의 인증샷을 남겨볼까요?* 📸\n"
                            "화상회의 화면을 촬영한 뒤 아래 버튼에서 올려 주세요. "
                            "참여자의 화면 공개 동의를 먼저 확인해 주세요."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "open_online_retro_photo_upload",
                            "text": {"type": "plain_text", "text": "인증샷 올리기"},
                            "style": "primary",
                            "value": _action_value(meeting),
                        }
                    ],
                },
            ],
        )
        await mark_announcement_sent(key)


async def handle_open_online_retro_photo_upload(
    ack: AsyncAck, body: dict, client: AsyncWebClient
) -> None:
    await ack()
    metadata = json.loads(body["actions"][0]["value"])
    meeting = _meeting(
        str(metadata.get("session_name") or "").strip(),
        str(metadata.get("team_channel") or "").strip(),
    )
    if meeting is None:
        raise ValueError("현재 설정된 온라인 회고 모임을 찾을 수 없어요.")
    await client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "online_retro_photo_submit",
            "title": {"type": "plain_text", "text": "온라인 모임 인증샷"},
            "submit": {"type": "plain_text", "text": "올리기"},
            "close": {"type": "plain_text", "text": "취소"},
            "private_metadata": _action_value(meeting),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "meeting_photo",
                    "label": {"type": "plain_text", "text": "인증샷"},
                    "element": {
                        "type": "file_input",
                        "action_id": "meeting_photo_input",
                        "filetypes": ["jpg", "jpeg", "png", "gif"],
                        "max_files": 1,
                    },
                }
            ],
        },
    )


async def handle_online_retro_photo_submit(
    ack: AsyncAck, body: dict, client: AsyncWebClient, view: dict
) -> None:
    metadata = json.loads(view["private_metadata"])
    meeting = _meeting(
        str(metadata.get("session_name") or "").strip(),
        str(metadata.get("team_channel") or "").strip(),
    )
    if meeting is None:
        raise ValueError("현재 설정된 온라인 회고 모임을 찾을 수 없어요.")
    files = view["state"]["values"]["meeting_photo"]["meeting_photo_input"].get(
        "files", []
    )
    if not files:
        await ack(
            response_action="errors", errors={"meeting_photo": "인증샷을 선택해 주세요."}
        )
        return
    file_id = files[0].get("id") if isinstance(files[0], dict) else files[0]
    await ack()
    await client.chat_postMessage(
        channel=meeting.channel,
        text=f"<@{body['user']['id']}>님이 {meeting.session_name} 인증샷을 올렸어요.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*<@{body['user']['id']}>님이 오늘의 온라인 회고 모임 인증샷을 남겼어요* 📸",
                },
            },
            {
                "type": "image",
                "slack_file": {"id": file_id},
                "alt_text": f"{meeting.session_name} 온라인 회고 모임 인증샷",
            },
        ],
    )


async def run_online_retro_meeting_scheduler(client: AsyncWebClient) -> None:
    logger.info(
        "온라인 회고 모임 공지 스케줄러가 시작되었습니다 - schedules={}",
        len(settings.ONLINE_RETRO_MEETINGS),
    )
    while True:
        now = tz_now()
        for meeting in settings.ONLINE_RETRO_MEETINGS:
            try:
                local_now = now.astimezone(meeting.notify_at.tzinfo)
                if (
                    meeting.notify_at <= local_now
                    < meeting.starts_at + LATE_ANNOUNCEMENT_GRACE
                ):
                    await post_online_retro_announcement(client, meeting)
                if (
                    meeting.starts_at <= local_now
                    < meeting.starts_at + LATE_ANNOUNCEMENT_GRACE
                ):
                    await post_writing_phase(client, meeting)
                writing_ends_at = meeting.starts_at + datetime.timedelta(
                    minutes=meeting.writing_minutes
                )
                if writing_ends_at <= local_now < writing_ends_at + TIME_UP_GRACE:
                    await post_writing_time_up(client, meeting)
            except Exception:
                logger.exception(
                    "온라인 회고 모임 공지 발송에 실패했습니다 - session={}",
                    meeting.session_name,
                )
        await asyncio.sleep(60)
