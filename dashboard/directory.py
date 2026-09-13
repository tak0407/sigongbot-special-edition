"""Slack 사용자/채널 이름과 딥링크. 실패해도 ID 표기로 조용히 폴백한다."""

import datetime
from html import escape

from aiohttp import web
from loguru import logger

DIRECTORY_TTL = datetime.timedelta(minutes=10)
USER_PAGE_LIMIT = 200
USER_PAGE_MAX = 10

SLACK_CLIENT = web.AppKey("slack_client")

# 페이지가 60초마다 자동 새로고침되므로 매 요청마다 Slack을 부르면 rate limit에
# 걸린다. 프로세스 안에서만 캐시한다.
_cache: dict = {"expires_at": None, "value": None}


def empty_directory() -> dict:
    return {"url": "", "users": {}, "channels": {}}


def reset_cache() -> None:
    _cache["value"] = None
    _cache["expires_at"] = None


async def _load(client) -> dict:
    directory = empty_directory()

    try:
        auth = await client.auth_test()
        directory["url"] = str(auth["url"]).rstrip("/")
    except Exception as error:
        logger.warning("Slack auth.test 실패 - 링크를 비활성화합니다 - {}", error)

    # users:read 스코프가 없으면 여기서 실패한다. 이름 없이 ID로 표시하면 되므로
    # 페이지 전체를 실패시키지 않는다.
    cursor = ""
    for _ in range(USER_PAGE_MAX):
        try:
            page = await client.users_list(limit=USER_PAGE_LIMIT, cursor=cursor)
        except Exception as error:
            logger.warning("Slack users.list 실패 - 사용자는 ID로 표시합니다 - {}", error)
            break
        for member in page.get("members", []):
            profile = member.get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or member.get("name")
                or ""
            ).strip()
            if name:
                directory["users"][member["id"]] = name
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    return directory


async def _resolve_channels(client, directory: dict, channel_ids: set[str]) -> None:
    for channel_id in sorted(channel_ids - set(directory["channels"])):
        if not channel_id:
            continue
        try:
            info = await client.conversations_info(channel=channel_id)
            directory["channels"][channel_id] = info["channel"]["name"]
        except Exception as error:
            # 비공개 채널은 groups:read가 없으면 조회되지 않는다. ID로 표시한다.
            logger.warning("Slack conversations.info 실패 - channel={} {}", channel_id, error)


async def get_directory(request: web.Request, channel_ids: set[str]) -> dict:
    client = request.app.get(SLACK_CLIENT)
    if client is None:
        return empty_directory()

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    expires_at = _cache["expires_at"]
    if _cache["value"] is None or expires_at is None or now >= expires_at:
        _cache["value"] = await _load(client)
        _cache["expires_at"] = now + DIRECTORY_TTL

    directory = _cache["value"]
    await _resolve_channels(client, directory, channel_ids)
    return directory


def user_label(directory: dict, user_id: str) -> str:
    """이름을 알면 이름을, 모르면 ID를 그대로 돌려준다."""
    return directory["users"].get(user_id) or user_id


def user_cell(directory: dict, user_id: str) -> str:
    name = directory["users"].get(user_id)
    if not name:
        return escape(user_id)
    return f'{escape(name)}<div class="sub">{escape(user_id)}</div>'


def channel_cell(directory: dict, channel_id: str) -> str:
    name = directory["channels"].get(channel_id)
    label = f"#{name}" if name else channel_id
    url = directory["url"]
    if not url or not channel_id:
        return escape(label)
    href = f"{url}/archives/{channel_id}"
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(label)}</a>'


def message_cell(directory: dict, channel_id: str, slack_ts: str, label: str) -> str:
    """제출 메시지로 바로 가는 Slack 딥링크."""
    url = directory["url"]
    if not url or not slack_ts or not channel_id:
        return escape(label)
    href = f"{url}/archives/{channel_id}/p{str(slack_ts).replace('.', '')}"
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(label)}</a>'
