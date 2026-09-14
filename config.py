import os
import json
import re
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class OnlineRetroMeeting:
    session_name: str
    starts_at: datetime
    notify_at: datetime
    channel: str
    url: str
    writing_minutes: int = 20


def _parse_aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"ONLINE_RETRO_MEETINGS의 {field}는 ISO 8601 문자열이어야 합니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"ONLINE_RETRO_MEETINGS의 {field} 형식을 확인하세요: {value}"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"ONLINE_RETRO_MEETINGS의 {field}에는 +09:00 같은 시간대가 필요합니다."
        )
    return parsed


def parse_online_retro_meetings(value: str) -> list[OnlineRetroMeeting]:
    """온라인 모임 공지 일정을 검증한다.

    실제 채널과 링크를 코드에 남기지 않도록 운영 환경변수의 JSON 배열로 받는다.
    """
    if not value.strip():
        return []
    try:
        raw_meetings = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("ONLINE_RETRO_MEETINGS는 JSON 배열이어야 합니다.") from None
    if not isinstance(raw_meetings, list):
        raise ValueError("ONLINE_RETRO_MEETINGS는 JSON 배열이어야 합니다.")

    meetings = []
    identities = set()
    for raw in raw_meetings:
        if not isinstance(raw, dict):
            raise ValueError("ONLINE_RETRO_MEETINGS의 각 일정은 JSON 객체여야 합니다.")
        session_name = str(raw.get("session_name") or "").strip()
        channel = str(raw.get("channel") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not session_name:
            raise ValueError("온라인 회고 모임에는 session_name이 필요합니다.")
        if not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
            raise ValueError("온라인 회고 모임의 channel ID를 확인하세요.")
        if not re.fullmatch(r"https://[^\s]+", url):
            raise ValueError("온라인 회고 모임의 url은 https:// 주소여야 합니다.")
        starts_at = _parse_aware_datetime(raw.get("starts_at"), "starts_at")
        notify_at = _parse_aware_datetime(raw.get("notify_at"), "notify_at")
        writing_minutes = raw.get("writing_minutes", 20)
        if not isinstance(writing_minutes, int) or not 15 <= writing_minutes <= 20:
            raise ValueError("온라인 회고 모임의 writing_minutes는 15~20이어야 합니다.")
        if notify_at > starts_at:
            raise ValueError("온라인 회고 모임의 notify_at은 starts_at보다 늦을 수 없습니다.")
        identity = (session_name, channel)
        if identity in identities:
            raise ValueError("ONLINE_RETRO_MEETINGS에 같은 회차와 팀 채널이 중복됐습니다.")
        identities.add(identity)
        meetings.append(
            OnlineRetroMeeting(
                session_name=session_name,
                starts_at=starts_at,
                notify_at=notify_at,
                channel=channel,
                url=url,
                writing_minutes=writing_minutes,
            )
        )
    return sorted(meetings, key=lambda meeting: meeting.notify_at)


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
        self.ONLINE_RETRO_MEETINGS: list[OnlineRetroMeeting] = (
            parse_online_retro_meetings(os.getenv("ONLINE_RETRO_MEETINGS", ""))
        )

        self.SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
        self.SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")

        self.ADMIN_CHANNEL: str = os.getenv("ADMIN_CHANNEL", "")
        self.SUPPORT_CHANNEL: str = os.getenv("SUPPORT_CHANNEL", "")

        self.ADMIN_IDS: list[str] = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
        # 이전 배포 감지와 이관 안내용. HTTP Basic 인증에는 사용하지 않는다.
        self.DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")
        self.DASHBOARD_SESSION_SECRET: str = os.getenv(
            "DASHBOARD_SESSION_SECRET", ""
        ).strip()
        self.DASHBOARD_SESSION_HOURS: int = int(
            os.getenv("DASHBOARD_SESSION_HOURS", "12")
        )

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
