"""특정 회차 미제출자에게 본인에게만 보이는 회고 리마인더를 보낸다.

누가 아직 제출하지 않았는지가 공개 채널에 드러나면 안 되므로 발송은 1:1 DM으로만
한다. `chat.postMessage`의 channel에 사용자 ID를 그대로 넘기면 Slack이 DM 채널을
잡아 주므로 `conversations.open`을 따로 부르지 않는다. 그래도 실패하는 경우가 있어
본인 팀 채널의 ephemeral로 되돌리고, 어느 쪽이든 결과를 운영 알림으로 남긴다.

미제출자 명단은 로그에도 알림에도 남기지 않는다. 남기는 것은 오류 코드와 인원 수뿐이다.
"""

import asyncio
import datetime
import json

from loguru import logger
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from alerts import build_alert, send_alert
from config import settings
from database.retrospective import get_submitted_user_ids
from database.scheduled_announcements import announcement_sent, mark_announcement_sent
from slack.ephemeral import post_ephemeral
from utils import get_current_session_info, tz_now

# 마감 8시간 전부터 마감까지가 발송 창이다. 마감이 화요일 05:00이면 월요일 21:00에 나간다.
# 기존 팀 채널 공개 리마인더(마감 32시간 전)와 시간대가 겹치지 않는다.
REMINDER_LEAD = datetime.timedelta(hours=8)
# 마감 직전에 한꺼번에 보내므로 chat.postMessage 제한에 걸리지 않게 한 명씩 간격을 둔다.
SEND_INTERVAL_SECONDS = 1.0
# ratelimited 응답에 Retry-After가 없을 때 쓸 대기 시간이다.
DEFAULT_RETRY_AFTER_SECONDS = 1.0
SCHEDULER_INTERVAL_SECONDS = 60
# 다시 보내도 결과가 같은 오류들이다. 스케줄러가 60초마다 발송 창 8시간을 돌기
# 때문에 표시해 두지 않으면 한 사람에게 같은 실패를 수백 번 반복한다.
PERMANENT_USER_ERRORS = frozenset(
    {
        "user_not_found",
        "users_not_found",
        "invalid_user",
        "user_disabled",
        "is_inactive",
        "cannot_dm_bot",
        "restricted_action",
    }
)
# 토큰이나 스코프 문제는 사람마다 확인할 필요가 없다. 한 명에게서 나왔다면
# 나머지도 같은 결과이므로 그 회차 발송을 즉시 접는다. 다음 회차에는 다시 시도한다.
FATAL_ERRORS = frozenset(
    {
        "missing_scope",
        "not_authed",
        "invalid_auth",
        "token_revoked",
        "token_expired",
        "account_inactive",
    }
)


def reminder_key(session_name: str, user_id: str) -> str:
    """중복 발송 방지 키. 회차 이름이 들어가므로 회차마다 1인 1회가 된다."""
    return f"unsubmitted-dm:{session_name}:{user_id}"


def build_reminder_blocks(
    *, session_name: str, team_channel: str, due_at: datetime.datetime
) -> list[dict]:
    metadata = json.dumps(
        {
            "channel_id": team_channel,
            "session_name": session_name,
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
                    f"*{session_name} 회고를 아직 기다리고 있어요* ⏰\n"
                    f"마감은 <!date^{int(due_at.timestamp())}^{{date_short}} {{time}}|"
                    f"{due_at.isoformat()}>이에요.\n"
                    "이 알림은 나에게만 보여요. 아래 버튼에서 작성하면 "
                    f"<#{team_channel}> 채널에 공유됩니다."
                ),
            },
        },
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


def _error_code(error: BaseException) -> str:
    if isinstance(error, SlackApiError):
        try:
            return str(error.response["error"] or "unknown")
        except Exception:
            return "unknown"
    return type(error).__name__


def _retry_after_seconds(error: SlackApiError) -> float:
    headers = getattr(error.response, "headers", None) or {}
    try:
        return max(float(headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECONDS)), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS


def _count(counts: dict[str, int], code: str) -> None:
    counts[code] = counts.get(code, 0) + 1


def _counts_text(counts: dict[str, int]) -> str:
    """오류 코드별 인원 수만 문자열로 만든다. 사용자 ID는 담지 않는다."""
    return ", ".join(f"{code} {number}명" for code, number in sorted(counts.items()))


async def _post_dm(
    client: AsyncWebClient, *, user_id: str, text: str, blocks: list[dict]
) -> None:
    """channel에 사용자 ID를 넘겨 1:1 DM으로 보낸다.

    rate limit에 걸리면 Slack이 알려 준 시간만큼 기다렸다가 한 번만 다시 시도한다.
    """
    try:
        await client.chat_postMessage(channel=user_id, text=text, blocks=blocks)
    except SlackApiError as error:
        if _error_code(error) != "ratelimited":
            raise
        await asyncio.sleep(_retry_after_seconds(error))
        await client.chat_postMessage(channel=user_id, text=text, blocks=blocks)


async def send_unsubmitted_reminders(
    client: AsyncWebClient, *, session_name: str, due_at: datetime.datetime
) -> dict[str, int]:
    """미제출자에게만 개인 리마인더를 보내고 발송 결과 집계를 돌려준다."""
    members = set(settings.SUBMISSION_DESTINATIONS)
    if not members:
        return {"targets": 0, "delivered": 0, "fallback": 0, "failed": 0}

    submitted = await get_submitted_user_ids(session_name, include_test=False)
    # 발송 순서를 재현 가능하게만 정렬한다. 이 목록은 밖으로 내보내지 않는다.
    targets = sorted(members - submitted)

    text = f"{session_name} 회고 마감이 다가오고 있어요."
    delivered = 0
    fallbacks: dict[str, int] = {}
    failures: dict[str, int] = {}
    sent_any = False
    fatal_code = ""

    for user_id in targets:
        key = reminder_key(session_name, user_id)
        if await announcement_sent(key):
            continue

        team_channel = settings.SUBMISSION_DESTINATIONS.get(user_id, "")
        blocks = build_reminder_blocks(
            session_name=session_name, team_channel=team_channel, due_at=due_at
        )
        if sent_any:
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
        sent_any = True

        try:
            await _post_dm(client, user_id=user_id, text=text, blocks=blocks)
        except Exception as error:
            code = _error_code(error)
            # 여기서 ERROR로 남기면 알림 싱크가 사람 수만큼 알림을 쏜다.
            # 발송이 끝난 뒤 집계 한 건으로 알린다.
            logger.bind(alert=False).warning(
                "미제출 회고 개인 리마인더 DM 실패 - session={}, error={}",
                session_name,
                code,
            )
            if code in FATAL_ERRORS:
                # 남은 사람에게 같은 실패를 반복하지 않고 접는다.
                fatal_code = code
                _count(failures, code)
                break
            reached = False
            if team_channel:
                try:
                    # ephemeral도 본인에게만 보이므로 공개 노출은 없다. 다만 새로고침하면
                    # 사라지므로 폴백이 쓰였다는 사실 자체를 운영 알림으로 올린다.
                    await post_ephemeral(
                        client,
                        channel=team_channel, user=user_id, text=text, blocks=blocks
                    )
                except Exception as fallback_error:
                    logger.bind(alert=False).warning(
                        "미제출 회고 개인 리마인더 폴백 실패 - session={}, error={}",
                        session_name,
                        _error_code(fallback_error),
                    )
                else:
                    _count(fallbacks, code)
                    reached = True
            if not reached:
                _count(failures, code)
                # 영구 오류는 다음 tick에 다시 시도해도 같은 실패이므로 끊는다.
                # 일시적 오류라면 표시하지 않아 다음 tick에 다시 시도한다.
                if code in PERMANENT_USER_ERRORS:
                    await mark_announcement_sent(key)
                continue
        else:
            delivered += 1

        await mark_announcement_sent(key)

    fallback_total = sum(fallbacks.values())
    failed_total = sum(failures.values())
    logger.info(
        "미제출 회고 개인 리마인더 - session={}, 대상={}명, DM={}명, 폴백={}명, 실패={}명",
        session_name,
        len(targets),
        delivered,
        fallback_total,
        failed_total,
    )
    if fallbacks or failures:
        await send_alert(
            build_alert(
                "미제출 회고 개인 리마인더 발송 문제",
                {
                    "회차": session_name,
                    "대상": f"{len(targets)}명",
                    "DM 성공": f"{delivered}명",
                    "ephemeral 폴백": _counts_text(fallbacks),
                    "발송 실패": _counts_text(failures),
                }
                | (
                    {"중단": f"{fatal_code} - 토큰/스코프 문제로 남은 발송을 중단했습니다"}
                    if fatal_code
                    else {}
                ),
                icon="⚠️",
            ),
            dedup_key=f"unsubmitted-reminder:{session_name}",
        )
    return {
        "targets": len(targets),
        "delivered": delivered,
        "fallback": fallback_total,
        "failed": failed_total,
    }


async def run_unsubmitted_reminder_once(
    client: AsyncWebClient, *, now: datetime.datetime | None = None
) -> bool:
    """발송 창 안이면 한 번 돌리고 True를 돌려준다."""
    now = now or tz_now()
    _, session_name, remaining, is_active = get_current_session_info(now)
    if not is_active or not session_name:
        return False
    # 테스트 전용인 SESSION_NAME_OVERRIDE는 남은 시간을 0으로 돌려주므로
    # 아래 조건에서 함께 걸러진다. 마감이 지난 회차도 같은 이유로 제외된다.
    if not (datetime.timedelta(0) < remaining <= REMINDER_LEAD):
        return False
    await send_unsubmitted_reminders(
        client, session_name=session_name, due_at=now + remaining
    )
    return True


async def run_unsubmitted_reminder_scheduler(client: AsyncWebClient) -> None:
    logger.info("미제출 회고 개인 리마인더 스케줄러가 시작되었습니다.")
    while True:
        try:
            await run_unsubmitted_reminder_once(client)
        except Exception:
            logger.exception("미제출 회고 개인 리마인더 발송에 실패했습니다.")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
