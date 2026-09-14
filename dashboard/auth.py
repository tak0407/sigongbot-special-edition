"""DB 계정, 서버 세션, CSRF 보호를 사용하는 관리자 웹 인증."""

import asyncio
import base64
import functools
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from aiohttp import web
from loguru import logger

from config import settings
from database.sqlite import get_connection

SESSION_COOKIE = "sigongbot_admin_session"
# 대시보드는 전용 호스트의 루트에 올라가므로 쿠키와 링크 모두 루트 기준이다.
COOKIE_PATH = "/"
LOGIN_PATH = "/login"
LEGACY_PREFIX = "/admin"
LOGIN_CSRF_COOKIE = "sigongbot_login_csrf"
# 로그인 화면을 열어 둔 채 자리를 비워도 한 번은 제출할 수 있게 넉넉히 잡는다.
LOGIN_CSRF_MINUTES = 30
AUTH_CONTEXT = web.AppKey("admin_auth_context", object)
PASSWORD_N = 2**14
PASSWORD_R = 8
PASSWORD_P = 1
MAX_LOGIN_FAILURES = 5
LOCK_MINUTES = 5
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,64}")


@dataclass(frozen=True)
class AuthContext:
    admin_user_id: int
    username: str
    session_token: str


def validate_dashboard_security() -> None:
    """운영에서 세션 키 없이 대시보드가 기동하는 것을 막는다."""
    if settings.DASHBOARD_PASSWORD:
        logger.warning(
            "DASHBOARD_PASSWORD는 더 이상 HTTP Basic 인증에 사용되지 않습니다. "
            "관리자 CLI로 계정을 이관한 뒤 제거하세요."
        )
    if settings.ENV == "prod" and len(settings.DASHBOARD_SESSION_SECRET) < 32:
        raise RuntimeError(
            "운영 환경에서는 32자 이상의 DASHBOARD_SESSION_SECRET가 필요합니다."
        )
    if not 1 <= settings.DASHBOARD_SESSION_HOURS <= 168:
        raise RuntimeError("DASHBOARD_SESSION_HOURS는 1~168 사이여야 합니다.")


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("관리자 비밀번호는 12자 이상이어야 합니다.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=PASSWORD_N, r=PASSWORD_R, p=PASSWORD_P
    )
    return "scrypt${}${}${}${}${}".format(
        PASSWORD_N,
        PASSWORD_R,
        PASSWORD_P,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "==")
        expected = base64.urlsafe_b64decode(digest_text + "==")
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD_HASH = hash_password("dummy-password-value")


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("계정명은 영문자, 숫자, _ . @ - 문자 1~64자만 사용할 수 있습니다.")
    return username


def create_admin(username: str, password: str) -> None:
    username = validate_username(username)
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
            (username, hash_password(password)),
        )


def change_admin_password(username: str, password: str) -> bool:
    password_hash = hash_password(password)
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE admin_users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
             WHERE username = ? COLLATE NOCASE
            """,
            (password_hash, username.strip()),
        )
        if cursor.rowcount:
            connection.execute(
                "DELETE FROM admin_sessions WHERE admin_user_id IN "
                "(SELECT id FROM admin_users WHERE username = ? COLLATE NOCASE)",
                (username.strip(),),
            )
        return cursor.rowcount > 0


def set_admin_active(username: str, active: bool) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE admin_users SET is_active = ?, updated_at = CURRENT_TIMESTAMP
             WHERE username = ? COLLATE NOCASE
            """,
            (int(active), username.strip()),
        )
        if cursor.rowcount and not active:
            connection.execute(
                "DELETE FROM admin_sessions WHERE admin_user_id IN "
                "(SELECT id FROM admin_users WHERE username = ? COLLATE NOCASE)",
                (username.strip(),),
            )
        return cursor.rowcount > 0


def list_admins() -> list[dict]:
    with get_connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT username, is_active, created_at, updated_at FROM admin_users ORDER BY username"
            )
        ]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _csrf_token(session_token: str) -> str:
    if len(settings.DASHBOARD_SESSION_SECRET) < 32:
        raise RuntimeError("DASHBOARD_SESSION_SECRET가 설정되지 않았습니다.")
    return hmac.new(
        settings.DASHBOARD_SESSION_SECRET.encode(),
        ("csrf:" + session_token).encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf_token(request: web.Request) -> str:
    context = request.get(AUTH_CONTEXT)
    if not isinstance(context, AuthContext):
        raise RuntimeError("인증된 요청에서만 CSRF 토큰을 만들 수 있습니다.")
    return _csrf_token(context.session_token)


def _attempt_key(username: str) -> str:
    # 헤더나 IP를 바꾸어도 우회하지 못하게 계정 단위로 잠그다.
    return hashlib.sha256(username.casefold().encode()).hexdigest()


def _authenticate_login(username: str, password: str) -> tuple[str, int | None]:
    """(status, user_id). status는 ok, invalid, locked 중 하나다."""
    key = _attempt_key(username)
    with get_connection() as connection:
        attempt = connection.execute(
            "SELECT locked_until FROM admin_login_attempts WHERE attempt_key = ?", (key,)
        ).fetchone()
        if attempt and attempt["locked_until"]:
            locked_until = datetime.fromisoformat(attempt["locked_until"]).replace(tzinfo=timezone.utc)
            if locked_until > datetime.now(timezone.utc):
                return "locked", None

        row = connection.execute(
            "SELECT id, password_hash, is_active FROM admin_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        # 없는 계정도 scrypt 연산을 해 시간차 노출을 줄인다.
        candidate_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(password, candidate_hash)
        valid = bool(row and row["is_active"] and password_valid)
        if valid:
            connection.execute("DELETE FROM admin_login_attempts WHERE attempt_key = ?", (key,))
            return "ok", int(row["id"])

        connection.execute(
            """
            INSERT INTO admin_login_attempts (attempt_key, failed_count, last_failed_at, locked_until)
            VALUES (?, 1, CURRENT_TIMESTAMP, NULL)
            ON CONFLICT(attempt_key) DO UPDATE SET
                failed_count = CASE WHEN last_failed_at < datetime('now', '-15 minutes')
                                    THEN 1 ELSE failed_count + 1 END,
                last_failed_at = CURRENT_TIMESTAMP,
                locked_until = CASE
                    WHEN (CASE WHEN last_failed_at < datetime('now', '-15 minutes')
                              THEN 1 ELSE failed_count + 1 END) >= ?
                    THEN datetime('now', ?)
                    ELSE NULL END
            """,
            (key, MAX_LOGIN_FAILURES, f"+{LOCK_MINUTES} minutes"),
        )
        return "invalid", None


def _create_session(admin_user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.DASHBOARD_SESSION_HOURS)
    with get_connection() as connection:
        connection.execute("DELETE FROM admin_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
        connection.execute(
            "INSERT INTO admin_sessions (token_hash, admin_user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), admin_user_id, expires.strftime("%Y-%m-%d %H:%M:%S")),
        )
    return token


def _load_session(token: str) -> AuthContext | None:
    if not token:
        return None
    token_hash = _token_hash(token)
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT u.id, u.username FROM admin_sessions s
            JOIN admin_users u ON u.id = s.admin_user_id
            WHERE s.token_hash = ? AND s.expires_at > CURRENT_TIMESTAMP AND u.is_active = 1
            """,
            (token_hash,),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
                (token_hash,),
            )
    return AuthContext(int(row["id"]), str(row["username"]), token) if row else None


def _delete_session(token: str) -> None:
    with get_connection() as connection:
        connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_token_hash(token),))


def safe_internal_path(value: str) -> str:
    """같은 사이트의 루트 기준 경로만 허용하고, 예전 /admin 접두사는 벗긴다."""
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    # 예전 /admin 북마크가 next로 들어와도 루트 기준으로 되돌린다.
    if value == LEGACY_PREFIX:
        return "/"
    if value.startswith(LEGACY_PREFIX + "/"):
        return value[len(LEGACY_PREFIX):]
    return value


def _set_secure_cookie(response: web.StreamResponse, name: str, value: str, *, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, httponly=True, secure=True, samesite="Strict", path=COOKIE_PATH
    )


def _login_page(*, csrf: str, next_path: str, error: str = "") -> str:
    notice = f'<p class="error">{escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>관리자 로그인</title><style>
body{{font-family:system-ui,sans-serif;background:#f5f7fa;margin:0;color:#202124}}
main{{max-width:380px;margin:10vh auto;background:white;padding:30px;border-radius:14px;box-shadow:0 8px 30px #0001}}
label{{display:block;margin:16px 0 6px}}input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd1d9;border-radius:8px}}
button{{width:100%;margin-top:22px;padding:10px;border:0;border-radius:8px;background:#2b5ce6;color:white;font-weight:600}}
.error{{color:#b42318}}small{{color:#667085}}</style><body><main><h1>시공삶 관리자</h1>
<small>관리자 계정으로 로그인하세요.</small>{notice}
<form method="post" action="/login"><input type="hidden" name="csrf_token" value="{escape(csrf)}">
<input type="hidden" name="next" value="{escape(next_path)}"><label for="username">계정명</label>
<input id="username" name="username" autocomplete="username" required autofocus><label for="password">비밀번호</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">로그인</button></form></main></body></html>"""


def _login_response(*, next_path: str, error: str = "", status: int = 200) -> web.Response:
    """로그인 화면을 새 CSRF 논스와 함께 내려준다."""
    nonce = secrets.token_urlsafe(24)
    try:
        csrf = _csrf_token("login:" + nonce)
    except RuntimeError as error_detail:
        raise web.HTTPServiceUnavailable(text=str(error_detail))
    response = web.Response(
        text=_login_page(csrf=csrf, next_path=next_path, error=error),
        status=status,
        content_type="text/html",
    )
    # 중간 캐시가 남의 논스가 담긴 화면을 돌려주지 않게 막는다.
    response.headers["Cache-Control"] = "no-store"
    _set_secure_cookie(response, LOGIN_CSRF_COOKIE, nonce, max_age=LOGIN_CSRF_MINUTES * 60)
    return response


async def handle_login_form(request: web.Request) -> web.Response:
    if await asyncio.to_thread(_load_session, request.cookies.get(SESSION_COOKIE, "")):
        raise web.HTTPFound(safe_internal_path(request.query.get("next", "/")))
    return _login_response(next_path=safe_internal_path(request.query.get("next", "/")))


async def handle_login(request: web.Request) -> web.Response:
    form = await request.post()
    nonce = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    supplied_csrf = str(form.get("csrf_token", ""))
    next_path = safe_internal_path(str(form.get("next", "/")))
    try:
        expected_csrf = _csrf_token("login:" + nonce) if nonce else ""
    except RuntimeError as error:
        raise web.HTTPServiceUnavailable(text=str(error))
    if not expected_csrf or not hmac.compare_digest(supplied_csrf, expected_csrf):
        logger.warning(
            "관리자 로그인 CSRF 검증 실패: 논스 쿠키 {}. 새 로그인 화면을 내려보냅니다.",
            "없음" if not nonce else "불일치",
        )
        # 거절은 하되 새 논스를 담은 화면을 함께 돌려줘 곧바로 다시 시도할 수 있게 한다.
        return _login_response(
            next_path=next_path,
            error="로그인 화면이 만료되었습니다. 다시 입력해 주세요.",
            status=403,
        )

    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    status, admin_user_id = await asyncio.to_thread(_authenticate_login, username, password)
    if status != "ok" or admin_user_id is None:
        error = (f"로그인 시도가 잠겼습니다. {LOCK_MINUTES}분 후 다시 시도하세요."
                 if status == "locked" else "계정명 또는 비밀번호가 올바르지 않습니다.")
        # 실패 화면에서도 논스를 새로 발급해 만료 시계를 다시 돌린다.
        return _login_response(
            next_path=next_path, error=error, status=429 if status == "locked" else 401
        )

    token = await asyncio.to_thread(_create_session, admin_user_id)
    response = web.HTTPFound(next_path)
    _set_secure_cookie(response, SESSION_COOKIE, token, max_age=settings.DASHBOARD_SESSION_HOURS * 3600)
    response.del_cookie(LOGIN_CSRF_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="Strict")
    raise response


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.StreamResponse:
        token = request.cookies.get(SESSION_COOKIE, "")
        context = await asyncio.to_thread(_load_session, token)
        if context is None:
            raise web.HTTPFound(LOGIN_PATH + "?next=" + quote(request.path_qs, safe="/?=&"))
        request[AUTH_CONTEXT] = context
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            form = await request.post()
            supplied = str(form.get("csrf_token", ""))
            if not supplied or not hmac.compare_digest(supplied, csrf_token(request)):
                logger.warning(
                    "관리자 요청 CSRF 검증 실패: {} {} (계정 {}).",
                    request.method,
                    request.path,
                    context.username,
                )
                raise web.HTTPForbidden(
                    text="CSRF 검증에 실패했습니다. 화면을 새로고침한 뒤 다시 시도하세요."
                )
        return await handler(request)

    return wrapped


@require_admin
async def handle_logout(request: web.Request) -> web.StreamResponse:
    context = request[AUTH_CONTEXT]
    await asyncio.to_thread(_delete_session, context.session_token)
    response = web.HTTPFound(LOGIN_PATH)
    response.del_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="Strict")
    raise response
