"""관리자 웹 인증. DASHBOARD_PASSWORD가 비어 있으면 인증 없이 연다."""

import base64
import functools
import hmac

from aiohttp import web

from config import settings

UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="sigongbot-admin"'}


def authorized(request: web.Request) -> bool:
    password = settings.DASHBOARD_PASSWORD
    if not password:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        username, supplied = base64.b64decode(header[6:]).decode().split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return False
    return username == "admin" and hmac.compare_digest(supplied, password)


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.StreamResponse:
        if not authorized(request):
            raise web.HTTPUnauthorized(headers=UNAUTHORIZED_HEADERS)
        return await handler(request)

    return wrapped
