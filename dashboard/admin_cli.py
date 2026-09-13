"""관리자 계정 운영 CLI. 비밀번호는 getpass로만 입력받는다."""

import argparse
import getpass
import sqlite3

from dashboard.auth import change_admin_password, create_admin, list_admins, set_admin_active
from database.sqlite import initialize_database


def _password_twice() -> str:
    password = getpass.getpass("비밀번호: ")
    confirmation = getpass.getpass("비밀번호 확인: ")
    if password != confirmation:
        raise ValueError("비밀번호가 서로 다릅니다.")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="시공봇 웹 관리자 계정 관리")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "password", "disable", "enable"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("username")
    subparsers.add_parser("list")
    args = parser.parse_args()
    # 운영 중 CLI를 실행해도 진행 중인 AI 작업 상태는 바꾸지 않는다.
    initialize_database(recover_processing_jobs=False)

    try:
        if args.command == "create":
            create_admin(args.username, _password_twice())
            print(f"관리자를 생성했습니다: {args.username}")
        elif args.command == "password":
            if not change_admin_password(args.username, _password_twice()):
                parser.error("관리자를 찾을 수 없습니다.")
            print(f"비밀번호를 변경하고 기존 세션을 종료했습니다: {args.username}")
        elif args.command in {"disable", "enable"}:
            active = args.command == "enable"
            if not set_admin_active(args.username, active):
                parser.error("관리자를 찾을 수 없습니다.")
            state = "활성화" if active else "비활성화"
            print(f"관리자 계정을 {state}했습니다: {args.username}")
        else:
            for admin in list_admins():
                state = "active" if admin["is_active"] else "disabled"
                print(f"{admin['username']}\t{state}\t{admin['updated_at']}")
    except (ValueError, sqlite3.IntegrityError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
