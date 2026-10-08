"""온라인 회고 시간 투표에 아직 응답하지 않은 팀원에게 1:1 DM으로 알린다.

모임 이틀 전 20:00에 한 번 보낸다. 시간을 이미 확정한 팀과 응답한 사람은 빼고,
테스트 투표는 실제 팀원에게 가지 않도록 보내지 않는다. DM에는 공지와 같은 투표
버튼을 달아 그 자리에서 고를 수 있게 한다.

미제출 리마인더처럼 누가 응답하지 않았는지는 로그와 운영 알림에 남기지 않고,
인원 수와 오류 코드만 남긴다.
"""

import asyncio
import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from alerts import build_alert, send_alert
from config import settings
from database.online_retro_confirmation import get_confirmation
from database.online_retro_poll import list_polls
from database.scheduled_announcements import (
    announcement_sent,
    mark_announcement_sent,
    sent_keys,
)
from slack.events.online_retro_poll import UNAVAILABLE_LABEL, meeting_day, poll_actions
from slack.events.unsubmitted_reminder import (
    FATAL_ERRORS,
    PERMANENT_USER_ERRORS,
    _count,
    _counts_text,
    _error_code,
    _post_dm,
)
from utils import tz_now

KST = ZoneInfo("Asia/Seoul")
# 모임 이틀 전 저녁에 보내 하루 응답할 여유를 두고, 운영자는 그다음 날 확정한다.
REMINDER_DAYS_BEFORE = 2
REMINDER_TIME = datetime.time(20, 0)
# 20시에 봇이 꺼져 있었어도 그날 밤 안에 켜지면 보낸다. 자정을 넘기면 보내지 않는다.
REMINDER_WINDOW = datetime.timedelta(hours=4)
# 팀원 수만큼 한꺼번에 보내므로 chat.postMessage 제한에 걸리지 않게 간격을 둔다.
SEND_INTERVAL_SECONDS = 1.0
SCHEDULER_INTERVAL_SECONDS = 60
KEY_PREFIX = "online-retro-poll-dm:"


def reminder_at(meeting_date: str) -> datetime.datetime:
    """'2026-10-11' → 2026-10-09 20:00 KST."""
    day = datetime.date.fromisoformat(meeting_date) - datetime.timedelta(
        days=REMINDER_DAYS_BEFORE
    )
    return datetime.datetime.combine(day, REMINDER_TIME, tzinfo=KST)


def reminder_key(poll_id: int, user_id: str) -> str:
    """중복 발송 방지 키. 투표마다 1인 1회가 된다."""
    return f"{KEY_PREFIX}{poll_id}:{user_id}"


async def reminded_users() -> dict[int, set[str]]:
    """투표별로 DM을 보낸 사람. 관리자 화면에서 보낸 인원을 센다."""
    reminded: dict[int, set[str]] = {}
    for key in await sent_keys(KEY_PREFIX):
        poll_id, _, user_id = key[len(KEY_PREFIX):].partition(":")
        reminded.setdefault(int(poll_id), set()).add(user_id)
    return reminded


def build_reminder_blocks(poll: dict) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{meeting_day(poll['meeting_date'])} 온라인 회고 시간 투표를 기다리고 있어요* 🗳️\n"
                    f"{poll['team_name']} 모임 시간은 가장 많이 고른 시간으로 정해요. "
                    "참여할 수 있는 시간을 모두 골라 주세요.\n"
                    f"참여가 어렵다면 `{UNAVAILABLE_LABEL}`를 눌러 주세요."
                ),
            },
        },
        poll_actions(poll["id"]),
    ]


async def _due_polls(now: datetime.datetime) -> list[dict]:
    """지금 알림 시간대에 들어 있고 아직 시간을 정하지 않은 실제 팀 투표."""
    due = []
    for poll in await list_polls():
        if poll["is_test"] or not poll["slack_ts"]:
            continue
        start = reminder_at(poll["meeting_date"])
        if not start <= now < start + REMINDER_WINDOW:
            continue
        # 알림 시각이 지나서 올린 투표라면 공지를 막 본 사람을 곧바로 독촉하게 된다.
        if datetime.datetime.fromtimestamp(float(poll["slack_ts"]), KST) > start:
            continue
        # 확정한 팀은 시간이 정해졌으니 묻지 않는다. 확정을 취소했다면 다시 묻는다.
        confirmation = await get_confirmation(poll["id"])
        if confirmation and confirmation["status"] != "cancelled":
            continue
        due.append(poll)
    return due


async def send_poll_reminders(
    client: AsyncWebClient, *, now: datetime.datetime | None = None
) -> dict[str, int]:
    """알림 시간대에 든 투표마다 미응답 팀원에게 DM을 보내고 결과 집계를 돌려준다."""
    now = now or tz_now()
    targets = 0
    delivered = 0
    failures: dict[str, int] = {}
    fatal_code = ""
    sent_any = False

    for poll in await _due_polls(now):
        waiting = sorted(
            user_id
            for user_id, channel in settings.SUBMISSION_DESTINATIONS.items()
            if channel == poll["team_channel"] and user_id not in poll["votes"]
        )
        text = f"{meeting_day(poll['meeting_date'])} 온라인 회고 시간을 골라 주세요."
        blocks = build_reminder_blocks(poll)
        for user_id in waiting:
            key = reminder_key(poll["id"], user_id)
            if await announcement_sent(key):
                continue
            targets += 1
            if sent_any:
                await asyncio.sleep(SEND_INTERVAL_SECONDS)
            sent_any = True

            try:
                await _post_dm(client, user_id=user_id, text=text, blocks=blocks)
            except Exception as error:
                code = _error_code(error)
                # 사람마다 ERROR를 남기면 알림 싱크가 그만큼 울린다. 끝나고 한 건으로 알린다.
                logger.bind(alert=False).warning(
                    "온라인 회고 투표 미응답 DM 실패 - meeting_date={}, error={}",
                    poll["meeting_date"],
                    code,
                )
                _count(failures, code)
                if code in FATAL_ERRORS:
                    # 토큰·스코프 문제는 남은 사람에게도 같으므로 이번 바퀴를 접는다.
                    fatal_code = code
                    break
                # 영구 오류는 다시 보내도 같으므로 끊고, 일시적 오류는 다음 바퀴에 다시 보낸다.
                if code in PERMANENT_USER_ERRORS:
                    await mark_announcement_sent(key)
                continue
            delivered += 1
            await mark_announcement_sent(key)
        if fatal_code:
            break

    failed = sum(failures.values())
    if targets:
        logger.info(
            "온라인 회고 투표 미응답 DM - 대상={}명, DM={}명, 실패={}명",
            targets,
            delivered,
            failed,
        )
    if failures:
        await send_alert(
            build_alert(
                "온라인 회고 투표 미응답 DM 발송 문제",
                {
                    "대상": f"{targets}명",
                    "DM 성공": f"{delivered}명",
                    "발송 실패": _counts_text(failures),
                }
                | (
                    {"중단": f"{fatal_code} - 토큰/스코프 문제로 남은 발송을 중단했습니다"}
                    if fatal_code
                    else {}
                ),
                icon="⚠️",
            ),
            dedup_key=f"online-retro-poll-reminder:{now.date().isoformat()}",
        )
    return {"targets": targets, "delivered": delivered, "failed": failed}


async def run_online_retro_poll_reminder_scheduler(client: AsyncWebClient) -> None:
    logger.info("온라인 회고 투표 미응답 DM 스케줄러가 시작되었습니다.")
    while True:
        try:
            await send_poll_reminders(client)
        except Exception:
            logger.exception("온라인 회고 투표 미응답 DM 발송에 실패했습니다.")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
