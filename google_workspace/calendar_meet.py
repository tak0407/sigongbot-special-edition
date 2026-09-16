"""Google Calendar 이벤트에 Google Meet 회의를 붙이고 관리한다.

설계 근거(공식 문서):
- 이벤트에 Meet을 붙일 때는 `conferenceData.createRequest`와
  `conferenceSolutionKey.type = "hangoutsMeet"`를 쓰고 `conferenceDataVersion=1`을
  함께 보낸다.
  https://developers.google.com/workspace/calendar/api/guides/create-events
- 중복 생성 방지는 클라이언트가 만든 이벤트 ID로 한다. ID는 base32hex(a-v, 0-9)
  5~1024자이며 캘린더 안에서 유일하다. 같은 ID로 다시 insert하면 409
  "The requested identifier already exists"가 돌아온다.
  https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
  https://developers.google.com/workspace/calendar/api/guides/errors
- 429/403 rateLimitExceeded와 5xx는 지수 백오프로 재시도한다.
  https://developers.google.com/workspace/calendar/api/guides/quota
- Meet 공간의 입장 정책(accessType)은 Calendar API로 설정할 수 없어 Meet REST API
  `spaces.patch`를 쓴다. `meetings.space.settings` 스코프는 Calendar가 만든 공간에도
  쓸 수 있다고 공지돼 있다.
  https://developers.google.com/workspace/meet/api/reference/rest/v2/spaces/patch
"""

import asyncio
import datetime
import hashlib
import random
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import aiohttp
from loguru import logger

from config import GoogleCredentials
from google_workspace.oauth import GoogleAuthError, get_access_token

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
MEET_BASE = "https://meet.googleapis.com/v2"
CONFERENCE_SOLUTION_TYPE = "hangoutsMeet"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 32.0
# 회의 생성은 비동기라 status가 pending으로 돌아올 수 있다.
CONFERENCE_POLL_ATTEMPTS = 5
CONFERENCE_POLL_SECONDS = 2.0

# RFC2938 base32hex. Calendar 이벤트 ID에 허용되는 문자 집합이다.
_BASE32HEX = "0123456789abcdefghijklmnopqrstuv"
_EVENT_ID_PREFIX = "sigongbotretro"


class GoogleApiError(RuntimeError):
    """Calendar/Meet API가 돌려준 오류."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(f"[{status}] {reason}: {message}".strip())
        self.status = status
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class MeetEvent:
    calendar_event_id: str
    meet_url: str
    html_link: str
    created: bool


def _base32hex(digest: bytes, length: int) -> str:
    value = int.from_bytes(digest, "big")
    characters = []
    for _ in range(length):
        characters.append(_BASE32HEX[value & 31])
        value >>= 5
    return "".join(characters)


def deterministic_event_id(*, meeting_date: str, team_channel: str, is_test: bool) -> str:
    """같은 팀·같은 날짜면 항상 같은 이벤트 ID를 만든다.

    재시도가 새 이벤트를 만들지 않게 하는 1차 방어선이다. DB의 UNIQUE 제약이
    2차 방어선이고, 이 ID 덕분에 DB 기록이 유실돼도 Calendar 쪽에서 409가 난다.
    """
    seed = f"{meeting_date}|{team_channel}|{int(is_test)}".encode()
    return _EVENT_ID_PREFIX + _base32hex(hashlib.sha256(seed).digest(), 32)


def deterministic_request_id(*, meeting_date: str, team_channel: str, is_test: bool) -> str:
    """conferenceData.createRequest에 쓰는 안정적인 requestId."""
    seed = f"conference|{meeting_date}|{team_channel}|{int(is_test)}".encode()
    return _base32hex(hashlib.sha256(seed).digest(), 26)


def meeting_code_from_url(meet_url: str) -> str:
    """https://meet.google.com/abc-mnop-xyz → abc-mnop-xyz."""
    path = urlparse(meet_url).path.strip("/")
    return path.split("/")[-1] if path else ""


def _video_entry_point(event: dict) -> str:
    for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return ""


def _conference_pending(event: dict) -> bool:
    create_request = (event.get("conferenceData") or {}).get("createRequest") or {}
    return str((create_request.get("status") or {}).get("statusCode", "")) == "pending"


def _is_retryable(status: int, reason: str) -> bool:
    if status in {429, 500, 502, 503, 504}:
        return True
    return status == 403 and reason in {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "backendError",
    }


def _error_reason(body: dict) -> tuple[str, str]:
    error = (body or {}).get("error")
    if isinstance(error, dict):
        errors = error.get("errors") or []
        reason = str(errors[0].get("reason", "")) if errors else str(error.get("status", ""))
        return reason, str(error.get("message", ""))
    return "", str(error or "")


async def _sleep_backoff(attempt: int) -> None:
    delay = min(2**attempt + random.random(), MAX_BACKOFF_SECONDS)
    await asyncio.sleep(delay)


async def _request(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> tuple[int, dict]:
    """한 번의 HTTP 호출. 테스트는 이 함수만 대체하면 된다."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.request(
            method, url, headers=headers, params=params, json=json_body
        ) as response:
            if response.status == 204:
                return response.status, {}
            body = await response.json(content_type=None)
            return response.status, body if isinstance(body, dict) else {}


async def _call(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict | None = None,
    json_body: dict | None = None,
    allow_status: tuple[int, ...] = (),
) -> tuple[int, dict]:
    """재시도 가능한 오류만 지수 백오프로 다시 시도한다."""
    for attempt in range(MAX_ATTEMPTS):
        status, body = await _request(
            method, url, access_token=access_token, params=params, json_body=json_body
        )
        if 200 <= status < 300 or status in allow_status:
            return status, body
        reason, message = _error_reason(body)
        if _is_retryable(status, reason) and attempt < MAX_ATTEMPTS - 1:
            logger.warning(
                "Google API 재시도 - {} {} status={} reason={} attempt={}",
                method,
                url,
                status,
                reason,
                attempt + 1,
            )
            await _sleep_backoff(attempt)
            continue
        if status == 401:
            raise GoogleAuthError("Google 액세스 토큰이 거부됐어요. 인증을 다시 확인해 주세요.")
        raise GoogleApiError(status, reason, message)
    raise GoogleApiError(503, "retryExhausted", "Google API 재시도 한도를 넘었어요.")


def _events_url(calendar_id: str, event_id: str = "") -> str:
    base = f"{CALENDAR_BASE}/calendars/{quote(calendar_id, safe='')}/events"
    return f"{base}/{quote(event_id, safe='')}" if event_id else base


def _event_body(
    *,
    summary: str,
    description: str,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    timezone: str,
    attendees: list[str],
) -> dict:
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": starts_at.isoformat(), "timeZone": timezone},
        "end": {"dateTime": ends_at.isoformat(), "timeZone": timezone},
        "status": "confirmed",
        "guestsCanModify": False,
    }
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    return body


def _conference_body(request_id: str) -> dict:
    return {
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": CONFERENCE_SOLUTION_TYPE},
            }
        }
    }


async def _resolve_meet_url(
    event: dict, *, access_token: str, calendar_id: str, event_id: str
) -> tuple[dict, str]:
    """회의 생성이 pending이면 성공할 때까지 짧게 다시 읽는다."""
    meet_url = _video_entry_point(event)
    attempts = 0
    while not meet_url and _conference_pending(event) and attempts < CONFERENCE_POLL_ATTEMPTS:
        attempts += 1
        await asyncio.sleep(CONFERENCE_POLL_SECONDS)
        _, event = await _call(
            "GET",
            _events_url(calendar_id, event_id),
            access_token=access_token,
            params={"conferenceDataVersion": "1"},
        )
        meet_url = _video_entry_point(event)
    return event, meet_url


async def ensure_meet_event(
    *,
    credentials: GoogleCredentials,
    calendar_id: str,
    event_id: str,
    request_id: str,
    summary: str,
    description: str,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    timezone: str,
    attendees: list[str] | None = None,
) -> MeetEvent:
    """이벤트를 만들거나, 이미 있으면 시간을 갱신한다.

    같은 `event_id`로 몇 번을 호출해도 Calendar 이벤트와 Meet 링크는 하나만
    남는다. 이미 있는 이벤트에는 Meet이 붙어 있으면 회의를 다시 만들지 않는다.
    """
    access_token = await get_access_token(credentials)
    attendees = attendees or []
    params = {"conferenceDataVersion": "1"}
    if attendees:
        params["sendUpdates"] = "all"

    status, existing = await _call(
        "GET",
        _events_url(calendar_id, event_id),
        access_token=access_token,
        params={"conferenceDataVersion": "1"},
        allow_status=(404, 410),
    )
    found = 200 <= status < 300

    base_body = _event_body(
        summary=summary,
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone,
        attendees=attendees,
    )
    created = False
    if not found:
        status, event = await _call(
            "POST",
            _events_url(calendar_id),
            access_token=access_token,
            params=params,
            json_body={**base_body, "id": event_id, **_conference_body(request_id)},
            allow_status=(409,),
        )
        if status == 409:
            # 동시 요청이나 재시도로 이미 만들어져 있다. 새로 만들지 않고 갱신한다.
            logger.info("Calendar 이벤트가 이미 있어 갱신으로 전환합니다 - event_id={}", event_id)
            _, existing = await _call(
                "GET",
                _events_url(calendar_id, event_id),
                access_token=access_token,
                params={"conferenceDataVersion": "1"},
            )
            found = True
        else:
            created = True

    if found:
        # 이미 회의가 붙어 있으면 conferenceData를 다시 보내지 않는다.
        patch_body = dict(base_body)
        if not _video_entry_point(existing) and not _conference_pending(existing):
            patch_body.update(_conference_body(request_id))
        _, event = await _call(
            "PATCH",
            _events_url(calendar_id, event_id),
            access_token=access_token,
            params=params,
            json_body=patch_body,
        )

    event, meet_url = await _resolve_meet_url(
        event, access_token=access_token, calendar_id=calendar_id, event_id=event_id
    )
    if not meet_url:
        raise GoogleApiError(502, "conferenceMissing", "Google Meet 링크를 받지 못했어요.")
    return MeetEvent(
        calendar_event_id=str(event.get("id") or event_id),
        meet_url=meet_url,
        html_link=str(event.get("htmlLink") or ""),
        created=created,
    )


async def cancel_meet_event(
    *, credentials: GoogleCredentials, calendar_id: str, event_id: str
) -> None:
    """이벤트를 취소 상태로 바꾼다. 삭제하지 않아 같은 ID로 되살릴 수 있다."""
    access_token = await get_access_token(credentials)
    status, _ = await _call(
        "PATCH",
        _events_url(calendar_id, event_id),
        access_token=access_token,
        params={"conferenceDataVersion": "1", "sendUpdates": "all"},
        json_body={"status": "cancelled"},
        allow_status=(404, 410),
    )
    if status in {404, 410}:
        logger.info("취소할 Calendar 이벤트가 이미 없습니다 - event_id={}", event_id)


async def apply_meet_access_type(
    *, credentials: GoogleCredentials, meet_url: str, access_type: str
) -> str:
    """Meet 공간의 입장 정책을 설정한다.

    운영자가 들어오지 않아도 팀원이 입장하려면 OPEN이어야 한다. 이 호출이
    실패해도 이벤트와 Meet 링크는 그대로 쓸 수 있으므로 예외를 삼키지 않고
    호출부에서 경고로 처리한다.
    """
    meeting_code = meeting_code_from_url(meet_url)
    if not meeting_code:
        raise GoogleApiError(400, "invalidMeetUrl", "Meet 주소에서 회의 코드를 찾지 못했어요.")
    access_token = await get_access_token(credentials)
    _, space = await _call(
        "PATCH",
        f"{MEET_BASE}/spaces/{quote(meeting_code, safe='')}",
        access_token=access_token,
        params={"updateMask": "config.accessType"},
        json_body={"config": {"accessType": access_type}},
    )
    return str(((space.get("config") or {}).get("accessType")) or access_type)
