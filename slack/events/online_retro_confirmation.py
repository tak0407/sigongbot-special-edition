"""팀별 회고 시간 확정, Google Meet 생성, Slack 확정 공지.

순서를 지킨다: DB에 확정 요청을 먼저 남기고(pending) → Google Calendar/Meet을
만들거나 갱신하고 → 결과를 DB에 기록한 다음 → Slack에 공지한다. 회고 흐름과
같은 "DB 기록 후 Slack 게시" 규칙을 그대로 따른다.
"""

import datetime
import json
from zoneinfo import ZoneInfo

from loguru import logger
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from database.online_retro_confirmation import (
    get_confirmation,
    mark_announced,
    mark_cancelled,
    mark_confirmed,
    mark_failed,
    reserve_confirmation,
)
from google_workspace.calendar_meet import (
    GoogleApiError,
    apply_meet_access_type,
    cancel_meet_event,
    deterministic_event_id,
    deterministic_request_id,
    ensure_meet_event,
)
from google_workspace.oauth import GoogleAuthError
from slack.events.online_retro_poll import TIME_SLOTS, WEEKDAYS

# 이 주소들만 Google 계정이 확실하다. 다른 도메인은 Google 계정인지 알 수 없어
# ONLINE_RETRO_TEAM_ATTENDEES로만 받는다.
GOOGLE_MAIL_DOMAINS = ("@gmail.com", "@googlemail.com")
NO_ATTENDEES_WARNING = (
    "Calendar에 초대할 팀원 Gmail을 찾지 못했어요. Slack 앱에 users:read.email 권한이 "
    "있는지 확인하거나, 캘린더에서 Meet 액세스를 '열림'으로 바꿔 주세요. "
    "초대받지 않은 팀원은 주최자가 승인해야 입장할 수 있어요."
)


class ConfirmationError(RuntimeError):
    """운영자에게 그대로 보여 줄 수 있는 확정 실패 사유."""


async def _team_gmail_addresses(client: AsyncWebClient, team_channel: str) -> list[str]:
    """팀원 Slack 프로필의 Gmail 주소. 초대받은 사람만 노크 없이 Meet에 들어온다."""
    members = sorted(
        user_id
        for user_id, channel in settings.SUBMISSION_DESTINATIONS.items()
        if channel == team_channel
    )
    addresses = []
    for user_id in members:
        try:
            response = await client.users_info(user=user_id)
        except SlackApiError as error:
            logger.warning(
                "팀원 프로필을 읽지 못해 초대에서 뺍니다 - user={} error={}",
                user_id,
                error.response.get("error"),
            )
            continue
        profile = (response.get("user") or {}).get("profile") or {}
        email = str(profile.get("email") or "").strip().lower()
        if email.endswith(GOOGLE_MAIL_DOMAINS):
            addresses.append(email)
    return addresses


async def _meeting_attendees(client: AsyncWebClient, team_channel: str) -> list[str]:
    configured = settings.ONLINE_RETRO_TEAM_ATTENDEES.get(team_channel, [])
    found = await _team_gmail_addresses(client, team_channel)
    return sorted({email.strip().lower() for email in [*configured, *found]})


def _timezone() -> ZoneInfo:
    return ZoneInfo(settings.GOOGLE_CALENDAR_TIMEZONE)


def slot_to_range(
    meeting_date: str, slot: str
) -> tuple[datetime.datetime, datetime.datetime]:
    """'2026-10-11' + '20:00~21:00' → 시간대가 붙은 시작·종료 시각."""
    if slot not in TIME_SLOTS:
        raise ConfirmationError("확정할 수 있는 시간 구간이 아니에요.")
    start_text, end_text = slot.split("~")
    day = datetime.date.fromisoformat(meeting_date)
    zone = _timezone()
    starts_at = datetime.datetime.combine(
        day, datetime.time.fromisoformat(start_text), tzinfo=zone
    )
    ends_at = datetime.datetime.combine(
        day, datetime.time.fromisoformat(end_text), tzinfo=zone
    )
    if ends_at <= starts_at:
        ends_at += datetime.timedelta(days=1)
    return starts_at, ends_at


def format_meeting_time(starts_at: datetime.datetime, ends_at: datetime.datetime) -> str:
    weekday = WEEKDAYS[starts_at.weekday()]
    return (
        f"{starts_at.month}월 {starts_at.day}일 {weekday}요일 "
        f"{starts_at:%H:%M}~{ends_at:%H:%M}"
    )


def _attendance_value(confirmation: dict) -> str:
    return json.dumps(
        {
            "session_name": confirmation["session_name"],
            "team_channel": confirmation["team_channel"],
        },
        ensure_ascii=False,
    )


def build_confirmation_blocks(confirmation: dict) -> list[dict]:
    starts_at = datetime.datetime.fromisoformat(confirmation["starts_at"])
    ends_at = datetime.datetime.fromisoformat(confirmation["ends_at"])
    prefix = "*[테스트]* 🧪\n\n" if confirmation["is_test"] else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{prefix}*{confirmation['team_name']} 온라인 회고 시간이 확정됐어요!* 🙌\n"
                    f"{format_meeting_time(starts_at, ends_at)}\n"
                    "아래 버튼으로 Google Meet에 입장해 주세요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_online_retro_meeting",
                    "text": {"type": "plain_text", "text": "Google Meet 입장"},
                    "style": "primary",
                    "url": confirmation["meet_url"],
                },
                {
                    "type": "button",
                    "action_id": "attend_online_retro_meeting",
                    "text": {"type": "plain_text", "text": "출석 체크"},
                    "value": _attendance_value(confirmation),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"<!date^{int(starts_at.timestamp())}^{{date_long_pretty}} {{time}}"
                        f"|{starts_at.isoformat()}>에 시작해요. "
                        f"시작 {settings.ONLINE_RETRO_NOTIFY_MINUTES_BEFORE}분 전 공지와 당일 진행은 "
                        f"<#{confirmation['team_channel']}>에서 해요."
                    ),
                }
            ],
        },
    ]


async def post_confirmation_announcement(
    client: AsyncWebClient, confirmation: dict, *, channel: str
) -> None:
    """확정 공지를 투표가 올라간 채널에 올린다. 시간이 바뀌면 기존 공지를 갱신한다.

    투표를 공지 채널에 모으므로 결과도 같은 곳에서 본다. 시작 직전 공지와 당일
    진행은 `@here`가 팀에만 가도록 팀 채널에서 한다.
    """
    text = f"{confirmation['team_name']} 온라인 회고 시간이 확정됐어요."
    blocks = build_confirmation_blocks(confirmation)
    if confirmation.get("announced_slack_ts"):
        await client.chat_update(
            channel=channel,
            ts=confirmation["announced_slack_ts"],
            text=text,
            blocks=blocks,
        )
        return
    response = await client.chat_postMessage(channel=channel, text=text, blocks=blocks)
    await mark_announced(
        confirmation_id=confirmation["id"], slack_ts=response["ts"]
    )


def _credentials():
    credentials = settings.google_credentials()
    if credentials is None:
        raise ConfirmationError(
            "Google 인증정보가 없어요. 운영 .env의 GOOGLE_OAUTH_* 값을 먼저 설정해 주세요."
        )
    return credentials


async def _apply_access_type(credentials, meet_url: str) -> tuple[str | None, str]:
    """입장 정책 설정은 실패해도 모임을 막지 않는다. (설정값, 경고문)."""
    if not settings.GOOGLE_MEET_ACCESS_TYPE:
        return None, ""
    try:
        applied = await apply_meet_access_type(
            credentials=credentials,
            meet_url=meet_url,
            access_type=settings.GOOGLE_MEET_ACCESS_TYPE,
        )
        return applied, ""
    except (GoogleApiError, GoogleAuthError) as error:
        logger.warning("Meet 입장 정책 설정에 실패했습니다 - {}", error)
        return None, (
            "Meet 입장 정책을 자동으로 바꾸지 못했어요. "
            "운영자가 Meet 설정에서 입장 방식을 직접 확인해 주세요."
        )


async def confirm_meeting_time(
    client: AsyncWebClient, *, poll: dict, slot: str
) -> tuple[dict, str]:
    """시간을 확정하고 Calendar 이벤트·Meet 링크를 만들거나 갱신한다.

    같은 팀·같은 날짜에는 항상 같은 이벤트 ID를 쓰므로 몇 번을 눌러도 이벤트와
    Meet 링크는 하나만 유지된다. 반환값은 (확정 행, 경고문).
    """
    starts_at, ends_at = slot_to_range(poll["meeting_date"], slot)
    credentials = _credentials()
    attendees = await _meeting_attendees(client, poll["team_channel"])

    identity = {
        "meeting_date": poll["meeting_date"],
        "team_channel": poll["team_channel"],
        "is_test": bool(poll["is_test"]),
    }
    existing = await get_confirmation(poll["id"])
    # Google 호출 전에 상태를 먼저 남긴다. 도중에 죽어도 pending으로 남아 추적된다.
    confirmation = await reserve_confirmation(
        poll=poll,
        slot=slot,
        starts_at=starts_at.isoformat(),
        ends_at=ends_at.isoformat(),
        calendar_event_id=(existing or {}).get("calendar_event_id")
        or deterministic_event_id(**identity),
        conference_request_id=(existing or {}).get("conference_request_id")
        or deterministic_request_id(**identity),
    )

    try:
        event = await ensure_meet_event(
            credentials=credentials,
            calendar_id=settings.GOOGLE_CALENDAR_ID,
            event_id=confirmation["calendar_event_id"],
            request_id=confirmation["conference_request_id"],
            summary=f"{confirmation['team_name']} 온라인 회고 ({confirmation['session_name']})",
            description=(
                "시공봇이 만든 팀 온라인 회고 모임입니다.\n"
                f"회차: {confirmation['session_name']}\n"
                f"팀: {confirmation['team_name']}"
            ),
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=settings.GOOGLE_CALENDAR_TIMEZONE,
            attendees=attendees,
        )
    except (GoogleApiError, GoogleAuthError) as error:
        await mark_failed(confirmation_id=confirmation["id"], error=str(error))
        logger.error(
            "Google Meet 생성에 실패했습니다 - team_channel={} error={}",
            confirmation["team_channel"],
            error,
        )
        raise ConfirmationError(f"Google 일정 생성에 실패했어요: {error}") from error

    access_type, warning = await _apply_access_type(credentials, event.meet_url)
    # OPEN이면 링크만으로 들어오므로 초대가 없어도 된다.
    if not attendees and access_type != "OPEN":
        warning = f"{warning} {NO_ATTENDEES_WARNING}".strip()
    confirmation = await mark_confirmed(
        confirmation_id=confirmation["id"],
        calendar_event_id=event.calendar_event_id,
        meet_url=event.meet_url,
        meet_access_type=access_type,
    )
    logger.info(
        "온라인 회고 시간을 확정했습니다 - team_channel={} slot={} created={} invited={}",
        confirmation["team_channel"],
        slot,
        event.created,
        len(attendees),
    )

    # DB 기록이 끝난 뒤에만 Slack에 올린다. 예전 투표는 팀 채널에 있다.
    await post_confirmation_announcement(
        client, confirmation, channel=poll.get("message_channel") or poll["team_channel"]
    )
    return confirmation, warning


async def cancel_meeting(*, poll_id: int) -> dict | None:
    """확정을 취소하고 Calendar 이벤트도 취소 상태로 바꾼다."""
    confirmation = await get_confirmation(poll_id)
    if confirmation is None:
        return None
    if confirmation["calendar_event_id"]:
        try:
            await cancel_meet_event(
                credentials=_credentials(),
                calendar_id=settings.GOOGLE_CALENDAR_ID,
                event_id=confirmation["calendar_event_id"],
            )
        except (GoogleApiError, GoogleAuthError) as error:
            raise ConfirmationError(f"Google 일정 취소에 실패했어요: {error}") from error
    await mark_cancelled(confirmation["id"])
    return await get_confirmation(poll_id)
