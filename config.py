import os
import json
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


class Settings:
    def __init__(self):
        self.ENV: str = os.getenv("ENV", "dev")
        self.SESSION_NAME_OVERRIDE: str = os.getenv(
            "SESSION_NAME_OVERRIDE", ""
        ).strip()
        self.TEST_SUBMISSION_CHANNEL: str = os.getenv(
            "TEST_SUBMISSION_CHANNEL", ""
        ).strip()

        self.SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
        self.SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")

        self.ADMIN_CHANNEL: str = os.getenv("ADMIN_CHANNEL", "")
        self.SUPPORT_CHANNEL: str = os.getenv("SUPPORT_CHANNEL", "")

        self.ADMIN_IDS: list[str] = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

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
