import os
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


@dataclass(frozen=True)
class GoogleCredentials:
    """Calendar/Meet 호출에 쓰는 운영자 Google 계정 자격증명."""

    client_id: str
    client_secret: str
    refresh_token: str


def parse_google_token_file(path: str) -> dict:
    """Git 비추적 인증 저장소(JSON)를 읽는다. 없으면 빈 dict를 돌려준다."""
    if not path:
        return {}
    token_path = Path(path).expanduser()
    if not token_path.exists():
        return {}
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Google 인증 파일을 읽을 수 없습니다: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError("Google 인증 파일은 JSON 객체여야 합니다.")
    # gcloud/oauth2l이 남기는 installed 래핑도 그대로 받아 준다.
    installed = payload.get("installed")
    if isinstance(installed, dict):
        payload = {**installed, **{k: v for k, v in payload.items() if k != "installed"}}
    return payload


MEET_ACCESS_TYPES = ("OPEN", "TRUSTED", "RESTRICTED")


def parse_meet_access_type(value: str) -> str:
    value = value.strip().upper()
    if value and value not in MEET_ACCESS_TYPES:
        raise ValueError(
            "GOOGLE_MEET_ACCESS_TYPE은 OPEN, TRUSTED, RESTRICTED 중 하나여야 합니다."
        )
    return value


def parse_team_attendees(value: str) -> dict[str, list[str]]:
    """팀 채널별로 Calendar 초대에 넣을 이메일 목록을 읽는다."""
    if not value.strip():
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("ONLINE_RETRO_TEAM_ATTENDEES는 JSON 객체여야 합니다.") from None
    if not isinstance(raw, dict):
        raise ValueError("ONLINE_RETRO_TEAM_ATTENDEES는 JSON 객체여야 합니다.")
    attendees: dict[str, list[str]] = {}
    for channel, emails in raw.items():
        if not re.fullmatch(r"[CG][A-Z0-9]{8,}", str(channel)):
            raise ValueError("ONLINE_RETRO_TEAM_ATTENDEES의 채널 ID를 확인하세요.")
        if not isinstance(emails, list):
            raise ValueError("ONLINE_RETRO_TEAM_ATTENDEES의 값은 이메일 목록이어야 합니다.")
        cleaned = []
        for email in emails:
            email = str(email).strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError(f"ONLINE_RETRO_TEAM_ATTENDEES의 이메일을 확인하세요: {email}")
            cleaned.append(email)
        if cleaned:
            attendees[str(channel)] = cleaned
    return attendees


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
        self.ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "").strip()
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

        # Google Calendar/Meet 연동. 자격증명은 .env 또는 Git 비추적 인증 파일에만 둔다.
        self.GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        self.GOOGLE_OAUTH_CLIENT_SECRET: str = os.getenv(
            "GOOGLE_OAUTH_CLIENT_SECRET", ""
        ).strip()
        self.GOOGLE_OAUTH_REFRESH_TOKEN: str = os.getenv(
            "GOOGLE_OAUTH_REFRESH_TOKEN", ""
        ).strip()
        self.GOOGLE_OAUTH_TOKEN_FILE: str = os.getenv(
            "GOOGLE_OAUTH_TOKEN_FILE", ""
        ).strip()
        self.GOOGLE_CALENDAR_ID: str = os.getenv(
            "GOOGLE_CALENDAR_ID", "primary"
        ).strip() or "primary"
        self.GOOGLE_CALENDAR_TIMEZONE: str = os.getenv(
            "GOOGLE_CALENDAR_TIMEZONE", "Asia/Seoul"
        ).strip() or "Asia/Seoul"
        self.GOOGLE_MEET_ACCESS_TYPE: str = parse_meet_access_type(
            os.getenv("GOOGLE_MEET_ACCESS_TYPE", "")
        )
        self.ONLINE_RETRO_TEAM_ATTENDEES: dict[str, list[str]] = parse_team_attendees(
            os.getenv("ONLINE_RETRO_TEAM_ATTENDEES", "")
        )
        self.ONLINE_RETRO_NOTIFY_MINUTES_BEFORE: int = int(
            os.getenv("ONLINE_RETRO_NOTIFY_MINUTES_BEFORE", "10")
        )
        self.ONLINE_RETRO_WRITING_MINUTES: int = int(
            os.getenv("ONLINE_RETRO_WRITING_MINUTES", "20")
        )
        if not 0 <= self.ONLINE_RETRO_NOTIFY_MINUTES_BEFORE <= 1440:
            raise ValueError("ONLINE_RETRO_NOTIFY_MINUTES_BEFORE는 0~1440이어야 합니다.")
        if not 15 <= self.ONLINE_RETRO_WRITING_MINUTES <= 20:
            raise ValueError("ONLINE_RETRO_WRITING_MINUTES는 15~20이어야 합니다.")

    def google_credentials(self) -> GoogleCredentials | None:
        """.env 값을 먼저 보고, 없으면 인증 파일에서 채운다. 부족하면 None."""
        stored = parse_google_token_file(self.GOOGLE_OAUTH_TOKEN_FILE)
        client_id = self.GOOGLE_OAUTH_CLIENT_ID or str(stored.get("client_id", "")).strip()
        client_secret = (
            self.GOOGLE_OAUTH_CLIENT_SECRET or str(stored.get("client_secret", "")).strip()
        )
        refresh_token = (
            self.GOOGLE_OAUTH_REFRESH_TOKEN or str(stored.get("refresh_token", "")).strip()
        )
        if not (client_id and client_secret and refresh_token):
            return None
        return GoogleCredentials(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )


settings = Settings()
