"""Google refresh token 발급·재발급 CLI.

OAuth 동의 화면의 게시 상태가 `테스트`이면 refresh token이 7일마다 만료된다.
이 스크립트는 그 재인증을 한 번의 명령으로 끝내기 위한 도구다.

받은 토큰은 `GOOGLE_OAUTH_TOKEN_FILE`이 가리키는 JSON 파일에 저장한다. 봇은 이
파일을 호출할 때마다 다시 읽으므로, 재발급 후 컨테이너를 재시작하지 않아도 된다.

    docker compose exec sigongbot python scripts/google_oauth_setup.py

클라이언트 시크릿과 인증 코드는 셸 기록에 남지 않도록 숨김 입력으로만 받는다.
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# 브라우저가 이 주소로 되돌아오면서 code를 붙여 준다. 실제로 서버를 띄우지는 않고
# 주소창에 남은 code를 사람이 복사한다.
REDIRECT_URI = "http://localhost:8765"
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/meetings.space.settings",
)
DEFAULT_TOKEN_FILE = "data/google_oauth_token.json"


def authorization_url(client_id: str, scopes: tuple[str, ...]) -> str:
    """동의 화면 주소를 만든다. offline + consent라야 refresh token이 내려온다."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(scopes),
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def normalize_code(raw: str) -> str:
    """주소창에서 통째로 붙여넣어도 code 값만 골라낸다."""
    value = raw.strip()
    if not value:
        raise ValueError("인증 코드가 비어 있습니다.")
    if "code=" in value:
        query = urllib.parse.urlparse(value).query or value.split("?", 1)[-1]
        codes = urllib.parse.parse_qs(query).get("code")
        if not codes:
            raise ValueError("붙여넣은 주소에서 code를 찾지 못했습니다.")
        return codes[0]
    # 주소창에서 잘라 온 값은 보통 퍼센트 인코딩이 남아 있다.
    return urllib.parse.unquote(value)


def exchange_code(*, client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    request = urllib.request.Request(TOKEN_ENDPOINT, data=payload)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = json.loads(error.read().decode() or "{}")
        reason = detail.get("error", error.code)
        description = detail.get("error_description", "")
        if reason == "invalid_grant":
            raise SystemExit(
                "인증 코드가 이미 사용됐거나 만료됐습니다. 주소를 다시 열어 새 코드를 받으세요."
            )
        raise SystemExit(f"토큰 교환에 실패했습니다: {reason} {description}")


def write_token_file(path: Path, payload: dict) -> None:
    """소유자만 읽을 수 있는 권한으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 먼저 빈 파일을 600으로 만들어 두고 쓴다. 쓰는 도중에도 남이 못 읽게 한다.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Google Calendar/Meet용 refresh token을 발급해 인증 파일에 저장합니다."
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        help="비우면 GOOGLE_OAUTH_CLIENT_ID 환경변수를 씁니다.",
    )
    parser.add_argument(
        "--output",
        default=os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "").strip() or DEFAULT_TOKEN_FILE,
        help=f"저장할 인증 파일 경로 (기본: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="파일에 쓰지 않고 refresh token을 화면에 출력합니다.",
    )
    args = parser.parse_args()

    client_id = args.client_id or input("클라이언트 ID: ").strip()
    if not client_id:
        parser.error("클라이언트 ID가 필요합니다.")
    # 시크릿은 인자로 받지 않는다. 셸 기록과 프로세스 목록에 남기지 않기 위해서다.
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or getpass.getpass(
        "클라이언트 보안 비밀: "
    )
    if not client_secret:
        parser.error("클라이언트 보안 비밀이 필요합니다.")

    print("\n아래 주소를 브라우저에서 열고 권한을 허용하세요.\n")
    print(authorization_url(client_id, DEFAULT_SCOPES))
    print(
        "\n허용하면 'localhost:8765에 연결할 수 없음' 화면이 뜹니다. 정상입니다."
        "\n그 화면의 주소창 내용을 통째로 복사해 아래에 붙여넣으세요.\n"
    )
    code = normalize_code(getpass.getpass("주소 또는 code 값(입력이 보이지 않습니다): "))

    tokens = exchange_code(
        client_id=client_id, client_secret=client_secret, code=code
    )
    refresh_token = str(tokens.get("refresh_token", ""))
    if not refresh_token:
        raise SystemExit(
            "응답에 refresh_token이 없습니다. 인증 주소에 prompt=consent가 있는지 확인하세요."
        )

    if args.print_only:
        print("\nrefresh_token:")
        print(refresh_token)
        return 0

    output = Path(args.output).expanduser()
    write_token_file(
        output,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    print(f"\n인증 파일을 저장했습니다: {output}")
    print(
        "봇은 호출할 때마다 이 파일을 다시 읽으므로 컨테이너를 재시작하지 않아도 됩니다.\n"
        "`.env`에 GOOGLE_OAUTH_REFRESH_TOKEN이 남아 있으면 그 값이 먼저 쓰이니 비워 두세요."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
