"""조별 전원 응답을 확인하고 1시간 뒤 기존 공지 메시지에서 시간을 확정한다."""

import asyncio
import datetime
import hashlib
from zoneinfo import ZoneInfo

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from alerts import build_alert, send_alert
from database.online_retro_confirmation import get_confirmation
from database.online_retro_poll import (
    list_polls,
    mark_auto_confirm_attempt,
    set_auto_confirm_error,
    sync_poll_completion,
)
from database.scheduled_announcements import announcement_sent, mark_announcement_sent
from slack.events import online_retro_lock as locks
from slack.events.online_retro_confirmation import (
    ConfirmationError,
    _confirm_meeting_time,
    slot_to_range,
)
from slack.events.online_retro_poll import TIME_SLOTS, _update_poll_message
from utils import tz_now

CONFIRM_DELAY = datetime.timedelta(hours=1)
RETRY_DELAY = datetime.timedelta(minutes=5)
SCHEDULER_INTERVAL_SECONDS = 60


def _eligible(poll: dict, now: datetime.datetime) -> bool:
    return bool(
        not poll["is_test"]
        and poll["slack_ts"]
        and settings.ANNOUNCEMENT_CHANNEL
        and poll["message_channel"] == settings.ANNOUNCEMENT_CHANNEL
        and datetime.date.fromisoformat(poll["meeting_date"]) >= now.date()
    )


def _winning_slot(poll: dict) -> str:
    members = {
        user for user, channel in settings.SUBMISSION_DESTINATIONS.items()
        if channel == poll["team_channel"]
    }
    counts = {
        slot: sum(slot in vote for user, vote in poll["votes"].items() if user in members)
        for slot in TIME_SLOTS
    }
    best = max(counts.values(), default=0)
    if not best:
        raise ConfirmationError("모두 불참이거나 참석 가능한 시간이 없어 운영자 확인이 필요해요.")
    # TIME_SLOTS의 이른 시간 순서가 관리자 화면의 동률 선택과 같다.
    return next(slot for slot in TIME_SLOTS if counts[slot] == best)


async def _process_poll(client: AsyncWebClient, poll_id: int, now: datetime.datetime) -> bool:
    async with locks.poll_lock(poll_id):
        # 잠금을 기다리는 동안 다른 요청이 확정했거나 응답을 바꿀 수 있으므로 다시 읽는다.
        poll = next((item for item in await list_polls() if item["id"] == poll_id), None)
        if not poll or not _eligible(poll, now):
            return False
        existing = await get_confirmation(poll_id)
        if existing and existing["status"] in {"confirmed", "cancelled"}:
            return False
        state = await sync_poll_completion(poll_id, now=now)
        completed = state["all_voted_at"]
        if not completed or now < datetime.datetime.fromisoformat(completed) + CONFIRM_DELAY:
            return False
        attempted = state["auto_confirm_attempted_at"]
        if attempted and now < datetime.datetime.fromisoformat(attempted) + RETRY_DELAY:
            return False
        await mark_auto_confirm_attempt(poll_id, now=now)
        try:
            slot = _winning_slot(poll)
            starts_at, _ = slot_to_range(poll["meeting_date"], slot)
            if starts_at <= now:
                raise ConfirmationError("최다 득표 시간이 이미 지나 운영자 확인이 필요해요.")
            await _confirm_meeting_time(client, poll=poll, slot=slot, announce=False)
        except Exception as error:
            message = str(error) if isinstance(error, ConfirmationError) else type(error).__name__
            await set_auto_confirm_error(poll_id, message)
            if state["auto_confirm_error"] != message:
                logger.error("온라인 회고 자동 확정 실패 - poll_id={} error={}", poll_id, message)
            return False
        await set_auto_confirm_error(poll_id, None)
        return True


async def process_auto_confirmations(
    client: AsyncWebClient, *, now: datetime.datetime | None = None
) -> dict[str, int]:
    now = (now or tz_now()).astimezone(ZoneInfo(settings.GOOGLE_CALENDAR_TIMEZONE))
    confirmed = 0
    for poll in await list_polls():
        if not _eligible(poll, now):
            continue
        try:
            confirmed += int(await _process_poll(client, poll["id"], now))
        except Exception:
            logger.exception("온라인 회고 자동 확정 점검 실패 - poll_id={}", poll["id"])

    # 같은 공지에 여러 조가 묶여 있다. 모든 DB 변경 후 메시지마다 한 번만 갱신한다.
    refreshed = set()
    updated = 0
    for poll in await list_polls():
        if not _eligible(poll, now):
            continue
        existing = await get_confirmation(poll["id"])
        if existing and existing["status"] == "confirmed" and existing.get("warning_text"):
            # Google 성공과 함께 저장된 경고를 알린다. 알림 실패/재시작에도 다시 시도하고
            # 이미 확정된 Calendar 이벤트는 다시 만들지 않는다.
            try:
                await _notify_confirmation_warning(existing)
            except Exception:
                logger.bind(alert=False).warning(
                    "온라인 회고 입장 설정 경고 알림 대기 - poll_id={}", poll["id"]
                )
        needs_confirmation_notice = (
            existing and existing["status"] == "confirmed"
            and existing["announced_slack_ts"] != poll["slack_ts"]
        )
        if not poll["message_dirty"] and not needs_confirmation_notice:
            continue
        key = (poll["message_channel"], poll["slack_ts"])
        if key in refreshed:
            continue
        refreshed.add(key)
        try:
            await _update_poll_message(client, poll)
            updated += 1
        except Exception:
            # DB의 갱신 대기를 지우지 않아 다음 주기에 Google 호출 없이 다시 고친다.
            logger.bind(alert=False).warning("온라인 회고 공지 갱신 대기 - poll_id={}", poll["id"])
    return {"confirmed": confirmed, "updated": updated}


async def _notify_confirmation_warning(confirmation: dict) -> None:
    warning = confirmation["warning_text"]
    digest = hashlib.sha256(warning.encode()).hexdigest()[:16]
    key = f"online-retro-confirmation-warning:{confirmation['id']}:{digest}"
    if await announcement_sent(key):
        return
    delivered = await send_alert(
        build_alert(
            "온라인 회고 모임 입장 설정 확인이 필요합니다",
            {"팀": confirmation["team_name"], "모임일": confirmation["meeting_date"], "확인사항": warning},
            icon="⚠️",
        ),
        dedup_key=key,
    )
    if delivered:
        await mark_announcement_sent(key)


async def run_online_retro_auto_confirm_scheduler(client: AsyncWebClient) -> None:
    logger.info("온라인 회고 시간 자동 확정 스케줄러가 시작되었습니다.")
    while True:
        try:
            await process_auto_confirmations(client)
        except Exception:
            logger.exception("온라인 회고 시간 자동 확정 주기 작업 실패")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
