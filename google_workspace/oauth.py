"""설치형 앱 OAuth 2.0 refresh token으로 액세스 토큰을 받아 온다.

refresh token은 `.env`나 인증 파일에 남아 있으므로 컨테이너가 재시작해도 다시
인증할 필요가 없다. 액세스 토큰만 프로세스 메모리에 캐시한다.

참고: https://developers.google.com/identity/protocols/oauth2/native-app
"""

import asyncio
import datetime

import aiohttp
from loguru import logger

from config import GoogleCredentials

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# 만료 직전에 쓰다가 401을 맞지 않도록 여유를 둔다.
EXPIRY_MARGIN = datetime.timedelta(seconds=120)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class GoogleAuthError(RuntimeError):
    """refresh token이 없거나 더 이상 유효하지 않을 때."""


_lock = asyncio.Lock()
_cache: dict = {"key": "", "token": "", "expires_at": None}


def reset_token_cache() -> None:
    """인증을 다시 받았거나 테스트에서 상태를 비울 때 사용한다."""
    _cache.update({"key": "", "token": "", "expires_at": None})


def _cache_key(credentials: GoogleCredentials) -> str:
    # refresh token을 통째로 남기지 않도록 끝 8자만 쓴다.
    return f"{credentials.client_id}:{credentials.refresh_token[-8:]}"


async def _request_access_token(credentials: GoogleCredentials) -> tuple[str, int]:
    payload = {
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
        "grant_type": "refresh_token",
    }
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(TOKEN_ENDPOINT, data=payload) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                error = str((body or {}).get("error", response.status))
                description = str((body or {}).get("error_description", ""))
                if error in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                    raise GoogleAuthError(
                        "Google 인증이 만료됐어요. refresh token을 다시 발급해 주세요. "
                        f"({error})"
                    )
                raise GoogleAuthError(f"Google 토큰 갱신에 실패했어요. ({error} {description})")
    token = str((body or {}).get("access_token", ""))
    if not token:
        raise GoogleAuthError("Google 토큰 응답에 access_token이 없습니다.")
    return token, int((body or {}).get("expires_in", 3600))


async def get_access_token(credentials: GoogleCredentials) -> str:
    """캐시된 액세스 토큰을 돌려주고, 만료가 임박하면 갱신한다."""
    key = _cache_key(credentials)
    now = datetime.datetime.now(datetime.timezone.utc)
    async with _lock:
        expires_at = _cache["expires_at"]
        if _cache["key"] == key and _cache["token"] and expires_at and now < expires_at:
            return str(_cache["token"])
        token, expires_in = await _request_access_token(credentials)
        _cache.update(
            {
                "key": key,
                "token": token,
                "expires_at": now + datetime.timedelta(seconds=expires_in) - EXPIRY_MARGIN,
            }
        )
        logger.info("Google 액세스 토큰을 갱신했습니다 - expires_in={}초", expires_in)
        return token
