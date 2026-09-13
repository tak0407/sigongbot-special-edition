import os
import json
import re
from dotenv import load_dotenv

if os.getenv("ENV", "dev") == "dev":
    load_dotenv()


def parse_admin_ids(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(admin_id).strip() for admin_id in parsed if str(admin_id).strip()]
    except json.JSONDecodeError:
        pass

    return [admin_id.strip() for admin_id in value.split(",") if admin_id.strip()]


def parse_submission_teams(value: str) -> dict[str, str]:
    """채널별 멤버 목록을 검증하고 멤버 ID별 목적지로 변환한다."""
    try:
        teams = json.loads(value or "{}")
    except json.JSONDecodeError:
        raise ValueError("SUBMISSION_TEAMS는 JSON 객체여야 합니다.") from None
    if not isinstance(teams, dict):
        raise ValueError("SUBMISSION_TEAMS는 JSON 객체여야 합니다.")
    destinations = {}
    for channel, members in teams.items():
        if not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
            raise ValueError("SUBMISSION_TEAMS의 채널 ID를 확인하세요.")
        if not isinstance(members, list) or not members:
            raise ValueError("각 팀에는 멤버 ID 목록이 필요합니다.")
        for member in members:
            if not isinstance(member, str) or not re.fullmatch(r"[UW][A-Z0-9]{8,}", member):
                raise ValueError("SUBMISSION_TEAMS의 멤버 ID를 확인하세요.")
            if member in destinations:
                raise ValueError("한 멤버를 중복 배정할 수 없습니다.")
            destinations[member] = channel
    return destinations


class Settings:
    def __init__(self):
        self.ENV: str = os.getenv("ENV", "dev")
        self.SESSION_NAME_OVERRIDE: str = os.getenv(
            "SESSION_NAME_OVERRIDE", ""
        ).strip()
        self.TEST_SUBMISSION_CHANNEL: str = os.getenv(
            "TEST_SUBMISSION_CHANNEL", ""
        ).strip()
        self.ANNOUNCEMENT_CHANNEL: str = os.getenv(
            "ANNOUNCEMENT_CHANNEL", ""
        ).strip()
        self.SUBMISSION_DESTINATIONS: dict[str, str] = parse_submission_teams(
            os.getenv("SUBMISSION_TEAMS", "")
        )
        self.SUBMISSION_CHANNEL_CHOOSER_IDS: list[str] = parse_admin_ids(
            os.getenv("SUBMISSION_CHANNEL_CHOOSER_IDS", "")
        )

        self.SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
        self.SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")

        self.ADMIN_CHANNEL: str = os.getenv("ADMIN_CHANNEL", "")
        self.SUPPORT_CHANNEL: str = os.getenv("SUPPORT_CHANNEL", "")

        self.ADMIN_IDS: list[str] = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
        self.DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

        self.DATABASE_PATH: str = os.getenv(
            "DATABASE_PATH", "data/sigongbot.db"
        ).strip()
        self.ANTIGRAVITY_COMMAND: str = os.getenv(
            "ANTIGRAVITY_COMMAND", "agy"
        ).strip()
        self.ANTIGRAVITY_MODEL: str = os.getenv(
            "ANTIGRAVITY_MODEL", "gemini-3.7-flash-low"
        ).strip()
        self.AI_REVIEW_TIMEOUT_SECONDS: int = int(
            os.getenv("AI_REVIEW_TIMEOUT_SECONDS", "180")
        )

settings = Settings()
