"""매회차 제출 공지 발송.

공지 문구와 공지 시각은 `sessions` 테이블이 정본이고 관리자 웹에서 고친다.
예전에는 6기 1회차 문구가 코드에 박혀 있어 그 회차 한 번만 나가고 끝났다.

발송 이력은 `sessions.announced_at`에 남는다. 스케줄러가 1분마다 도는 동안
같은 회차 공지가 두 번 나가지 않게 막는 유일한 근거라 게시 직후에 기록한다.
"""

import asyncio
import datetime
import json

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from constants import (
    SIXTH_FIRST_SESSION_DUE,
    SIXTH_FIRST_SESSION_NAME,
    SIXTH_FIRST_SESSION_START,
)
from database.retrospective import get_submitted_user_ids
from database.scheduled_announcements import announcement_sent, mark_announcement_sent
from database.sessions import mark_announced, pending_announcements
from utils import tz_now

POLL_SECONDS = 60
# 6기 1회차에만 붙였던 마감 하루 전 리마인더. 공지와 달리 아직 회차별로
# 일반화하지 않았다.
SIXTH_FIRST_REMINDER_AT = SIXTH_FIRST_SESSION_START.replace(day=13, hour=21)


def build_announcement_blocks(*, channel_id: str, session_name: str, body: str) -> list[dict]:
    metadata = json.dumps(
        {
            "channel_id": channel_id,
            "session_name": session_name,
            "test_mode": False,
        },
        ensure_ascii=False,
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "start_retrospective_from_announcement",
                    "text": {"type": "plain_text", "text": "회고 제출하기"},
                    "style": "primary",
                    "value": metadata,
                }
            ],
        },
    ]


async def post_due_announcements(
    client: AsyncWebClient, *, now: datetime.datetime | None = None
) -> int:
    """공지 시각이 된 회차를 모두 게시하고 게시한 건수를 돌려준다."""
    channel_id = settings.ANNOUNCEMENT_CHANNEL
    if not channel_id:
        logger.warning("ANNOUNCEMENT_CHANNEL이 비어 있어 회차 공지를 보내지 않습니다.")
        return 0

    now = now or tz_now()
    posted = 0
    for session in await asyncio.to_thread(pending_announcements, now):
        name = session["name"]
        # 문구에서 <!here>를 지웠다면 알림 폴백에서도 빼야 멘션이 살아남지 않는다.
        mention = "<!here> " if "<!here>" in session["body"] else ""
        await client.chat_postMessage(
            channel=channel_id,
            text=f"{mention}{name} 회고를 제출해 주세요.",
            blocks=build_announcement_blocks(
                channel_id=channel_id, session_name=name, body=session["body"]
            ),
        )
        await asyncio.to_thread(mark_announced, name, now)
        logger.info("{} 제출 공지를 보냈습니다 - channel={}", name, channel_id)
        posted += 1
    return posted


async def post_sixth_first_reminder(client: AsyncWebClient) -> None:
    submitted = await get_submitted_user_ids(SIXTH_FIRST_SESSION_NAME)
    members = set(settings.SUBMISSION_DESTINATIONS)
    channel_id = settings.ANNOUNCEMENT_CHANNEL
    if not channel_id or not members or members <= submitted:
        return
    key = f"6-1-reminder:{channel_id}"
    if await announcement_sent(key):
        return
    await client.chat_postMessage(
        channel=channel_id,
        text="6기 1회차 회고 마감까지 약 하루 남았어요.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*6기 1회차 회고 마감까지 약 하루 남았어요* ⏰\n"
                        "아직 회고를 남기지 않았다면 위 공지의 `회고 제출하기` 버튼에서 작성해 주세요.\n"
                        "마감은 화요일 오전 5시입니다."
                    ),
                },
            }
        ],
    )
    await mark_announcement_sent(key)


async def run_session_announcement_scheduler(client: AsyncWebClient) -> None:
    logger.info("회차 공지 스케줄러가 시작되었습니다.")
    while True:
        try:
            await post_due_announcements(client)
            if SIXTH_FIRST_REMINDER_AT <= tz_now() < SIXTH_FIRST_SESSION_DUE:
                await post_sixth_first_reminder(client)
        except Exception:
            logger.exception("회차 공지 발송에 실패했습니다.")
        await asyncio.sleep(POLL_SECONDS)
