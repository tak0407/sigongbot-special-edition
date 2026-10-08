"""시간 확정과 Google Meet 생성 테스트.

Google API는 전부 mock으로 대체한다. `FakeGoogle`은 실제 Calendar처럼 이벤트를
ID로 저장하고 같은 ID로 다시 insert하면 409를 돌려주므로, 중복 생성 방지와
재시도 동작을 실제 계약에 가깝게 확인할 수 있다.
"""

import datetime
import json
import tempfile
import unittest
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slack_sdk.errors import SlackApiError

from config import GoogleCredentials, settings
from dashboard import auth, register_dashboard_routes
from dashboard import directory as slack_directory
from dashboard.online_retro import _team_labels, second_sunday_after, team_name_from_channel
from database.online_retro_confirmation import get_confirmation, list_confirmed
from database.online_retro_poll import (
    create_poll,
    get_poll,
    list_polls,
    mark_poll_posted,
    save_vote,
)
from database.scheduled_announcements import mark_announcement_sent
from database.sqlite import get_connection, initialize_database
from google_workspace import calendar_meet
from google_workspace.calendar_meet import (
    deterministic_event_id,
    deterministic_request_id,
    meeting_code_from_url,
)
from slack.events.online_retro_confirmation import (
    ConfirmationError,
    cancel_meeting,
    confirm_meeting_time,
    slot_to_range,
)
from slack.events.online_retro_meeting import (
    _meeting,
    _scheduled_meetings,
    handle_online_retro_attendance,
)
from slack.events.online_retro_poll import ALL_OPTIONS, DEFAULT_POLL_INTRO, UNAVAILABLE
from slack.events.online_retro_poll_reminder import reminder_key

KST = ZoneInfo("Asia/Seoul")
CHANNEL_NAMES = {
    "C11111111": "green-team-weekly-report",
    "C22222222": "blue-team-weekly-report",
    "C33333333": "yellow-team-weekly-report",
    "C0ANNOUNCE": "announcement",
}
CREDENTIALS = GoogleCredentials(
    client_id="test-client-id",
    client_secret="test-client-secret",
    refresh_token="test-refresh-token",
)


def _error(reason: str, message: str) -> dict:
    return {"error": {"errors": [{"reason": reason}], "message": message}}


class FakeGoogle:
    """Calendar 이벤트 저장소와 Meet 공간 설정을 흉내 낸다."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.spaces: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict | None] = []
        self.queued_failures: list[tuple[int, str]] = []
        self.meet_patch_error: tuple[int, str] | None = None
        self._next_code = 0

    def counts(self, method: str) -> int:
        return sum(1 for call_method, _ in self.calls if call_method == method)

    def inserted(self) -> dict:
        return next(
            body for (method, _), body in zip(self.calls, self.bodies) if method == "POST"
        )

    def _new_meet_url(self) -> str:
        self._next_code += 1
        return f"https://meet.google.com/abc-defg-h{self._next_code:02d}"

    async def request(self, method, url, *, access_token, params=None, json_body=None):
        assert access_token, "액세스 토큰 없이 호출하면 안 된다."
        self.calls.append((method, url))
        self.bodies.append(json_body)
        if self.queued_failures:
            status, reason = self.queued_failures.pop(0)
            return status, _error(reason, "테스트용 실패")

        path = urlparse(url).path
        if path.startswith("/v2/spaces/"):
            return self._meet_space(path, json_body)
        return self._calendar(method, path, json_body)

    def _meet_space(self, path: str, json_body: dict | None):
        if self.meet_patch_error:
            status, reason = self.meet_patch_error
            return status, _error(reason, "Meet 설정 실패")
        code = path.rsplit("/", 1)[-1]
        access_type = ((json_body or {}).get("config") or {}).get("accessType", "")
        self.spaces[code] = access_type
        return 200, {"name": f"spaces/{code}", "config": {"accessType": access_type}}

    def _calendar(self, method: str, path: str, json_body: dict | None):
        tail = path.split("/events", 1)[1].lstrip("/")
        if method == "GET":
            event = self.events.get(tail)
            if event is None:
                return 404, _error("notFound", "Not Found")
            return 200, event
        if method == "POST":
            event_id = str((json_body or {}).get("id", ""))
            if event_id in self.events:
                return 409, _error("duplicate", "The requested identifier already exists.")
            self.events[event_id] = self._materialize(event_id, {}, json_body or {})
            return 200, self.events[event_id]
        if method == "PATCH":
            event = self.events.get(tail)
            if event is None:
                return 404, _error("notFound", "Not Found")
            self.events[tail] = self._materialize(tail, event, json_body or {})
            return 200, self.events[tail]
        raise AssertionError(f"예상하지 못한 호출: {method} {path}")

    def _materialize(self, event_id: str, existing: dict, body: dict) -> dict:
        event = {**existing, **{k: v for k, v in body.items() if k != "conferenceData"}}
        event["id"] = event_id
        event.setdefault("htmlLink", f"https://calendar.google.com/event?eid={event_id}")
        conference = body.get("conferenceData") or {}
        if conference.get("createRequest") and not existing.get("conferenceData"):
            request_id = conference["createRequest"]["requestId"]
            event["conferenceData"] = {
                "createRequest": {
                    "requestId": request_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "status": {"statusCode": "success"},
                },
                "entryPoints": [
                    {"entryPointType": "video", "uri": self._new_meet_url()},
                    {"entryPointType": "more", "uri": "https://tel.meet/abc"},
                ],
            }
        elif existing.get("conferenceData"):
            event["conferenceData"] = existing["conferenceData"]
        return event


class ConfirmationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(patch.object(settings, "ONLINE_RETRO_MEETINGS", []))
        self.enterContext(patch.object(settings, "GOOGLE_CALENDAR_ID", "primary"))
        self.enterContext(patch.object(settings, "GOOGLE_CALENDAR_TIMEZONE", "Asia/Seoul"))
        self.enterContext(patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", "OPEN"))
        self.enterContext(patch.object(settings, "ONLINE_RETRO_TEAM_ATTENDEES", {}))
        self.enterContext(
            patch.object(settings, "google_credentials", lambda: CREDENTIALS)
        )
        self.google = FakeGoogle()
        self.enterContext(patch.object(calendar_meet, "_request", self.google.request))
        self.enterContext(
            patch.object(
                calendar_meet, "get_access_token", AsyncMock(return_value="test-token")
            )
        )
        initialize_database()
        self.slack = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "1700000000.0001"}),
            chat_update=AsyncMock(),
            chat_postEphemeral=AsyncMock(),
        )

    async def make_poll(self, *, team_channel="C11111111", is_test=False) -> dict:
        poll = await create_poll(
            meeting_date="2026-10-11",
            session_name="6기 1회차",
            team_channel=team_channel,
            team_name="그린팀",
            slots=ALL_OPTIONS,
            is_test=is_test,
        )
        await save_vote(poll_id=poll["id"], user_id="U11111111", slots=["20:00~21:00"])
        return await get_poll(poll["id"])


class SecondSundayTest(unittest.TestCase):
    def test_next_meeting_is_the_coming_second_sunday(self):
        cases = {
            datetime.date(2026, 10, 5): datetime.date(2026, 10, 11),
            datetime.date(2026, 10, 11): datetime.date(2026, 11, 8),
            datetime.date(2026, 12, 20): datetime.date(2027, 1, 10),
            datetime.date(2026, 2, 28): datetime.date(2026, 3, 8),
        }
        for today, expected in cases.items():
            with self.subTest(today=today):
                self.assertEqual(second_sunday_after(today), expected)


class TeamNameTest(unittest.TestCase):
    def test_team_name_comes_from_a_color_channel_name(self):
        self.assertEqual(team_name_from_channel("blue-team-weekly-report"), "블루팀")
        self.assertEqual(team_name_from_channel("Yellow-Team"), "옐로우팀")
        self.assertEqual(team_name_from_channel("team-a-weekly-report"), "")

    def test_label_falls_back_to_the_last_poll_then_the_channel(self):
        directory = {
            "channels": {
                "C1": "green-team-weekly-report",
                "C2": "alpha-squad",
                "C3": "beta-squad",
            }
        }
        polls = [{"team_channel": "C2", "team_name": "알파팀", "is_test": 0}]
        self.assertEqual(
            _team_labels(["C1", "C2", "C3", "C4"], directory, polls),
            {"C1": "그린팀", "C2": "알파팀", "C3": "#beta-squad", "C4": "C4"},
        )


class SlotRangeTest(unittest.TestCase):
    def test_slot_becomes_timezone_aware_range(self):
        starts_at, ends_at = slot_to_range("2026-10-11", "20:00~21:00")
        self.assertEqual(starts_at.isoformat(), "2026-10-11T20:00:00+09:00")
        self.assertEqual(ends_at.isoformat(), "2026-10-11T21:00:00+09:00")

    def test_unavailable_option_cannot_be_confirmed(self):
        with self.assertRaises(ConfirmationError):
            slot_to_range("2026-10-11", UNAVAILABLE)


class EventIdentityTest(unittest.TestCase):
    def test_event_id_is_stable_and_uses_allowed_characters(self):
        identity = {
            "meeting_date": "2026-10-11",
            "team_channel": "C11111111",
            "is_test": False,
        }
        first = deterministic_event_id(**identity)
        self.assertEqual(first, deterministic_event_id(**identity))
        # Calendar가 요구하는 base32hex(a-v, 0-9) 5~1024자여야 한다.
        self.assertTrue(set(first) <= set("0123456789abcdefghijklmnopqrstuv"))
        self.assertTrue(5 <= len(first) <= 1024)

    def test_different_team_or_date_gets_different_ids(self):
        base = {"meeting_date": "2026-10-11", "team_channel": "C11111111", "is_test": False}
        other_team = {**base, "team_channel": "C22222222"}
        other_date = {**base, "meeting_date": "2026-11-08"}
        ids = {
            deterministic_event_id(**base),
            deterministic_event_id(**other_team),
            deterministic_event_id(**other_date),
        }
        self.assertEqual(len(ids), 3)
        self.assertNotEqual(
            deterministic_request_id(**base), deterministic_request_id(**other_team)
        )

    def test_meeting_code_comes_from_meet_url(self):
        self.assertEqual(
            meeting_code_from_url("https://meet.google.com/abc-mnop-xyz"), "abc-mnop-xyz"
        )


class ConfirmMeetingTest(ConfirmationTestCase):
    async def test_confirm_creates_event_and_posts_after_saving(self):
        poll = await self.make_poll()
        confirmation, warning = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )

        self.assertEqual(warning, "")
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(confirmation["starts_at"], "2026-10-11T20:00:00+09:00")
        self.assertEqual(confirmation["ends_at"], "2026-10-11T21:00:00+09:00")
        self.assertTrue(confirmation["meet_url"].startswith("https://meet.google.com/"))
        self.assertEqual(confirmation["meet_access_type"], "OPEN")

        stored = await get_confirmation(poll["id"])
        self.assertEqual(stored["meet_url"], confirmation["meet_url"])
        self.assertEqual(stored["announced_slack_ts"], "1700000000.0001")

        # Slack 공지는 DB 기록이 끝난 뒤에 올라간다.
        message = self.slack.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C11111111")
        actions = message["blocks"][1]["elements"]
        self.assertEqual(
            [action["action_id"] for action in actions],
            ["open_online_retro_meeting", "attend_online_retro_meeting"],
        )
        self.assertEqual(actions[0]["url"], confirmation["meet_url"])
        self.assertIn("10월 11일 일요일 20:00~21:00", message["blocks"][0]["text"]["text"])

    async def test_event_body_carries_conference_and_timezone(self):
        poll = await self.make_poll()
        await confirm_meeting_time(self.slack, poll=poll, slot="21:00~22:00")
        insert_body = next(
            body
            for method, body in zip([m for m, _ in self.google.calls], self.google.bodies)
            if method == "POST"
        )
        self.assertEqual(
            insert_body["conferenceData"]["createRequest"]["conferenceSolutionKey"],
            {"type": "hangoutsMeet"},
        )
        self.assertEqual(insert_body["start"]["timeZone"], "Asia/Seoul")
        self.assertEqual(insert_body["start"]["dateTime"], "2026-10-11T21:00:00+09:00")

    async def test_repeated_confirmation_never_creates_a_second_event(self):
        poll = await self.make_poll()
        first, _ = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        second, _ = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")

        self.assertEqual(first["meet_url"], second["meet_url"])
        self.assertEqual(first["calendar_event_id"], second["calendar_event_id"])
        self.assertEqual(len(self.google.events), 1)
        self.assertEqual(self.google.counts("POST"), 1)
        with get_connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM online_retro_confirmed_meetings"
            ).fetchone()[0]
        self.assertEqual(total, 1)
        # 재확정은 새 공지가 아니라 기존 공지를 갱신한다.
        self.slack.chat_postMessage.assert_awaited_once()
        self.slack.chat_update.assert_awaited_once()

    async def test_duplicate_identifier_falls_back_to_update(self):
        poll = await self.make_poll()
        event_id = deterministic_event_id(
            meeting_date="2026-10-11", team_channel="C11111111", is_test=False
        )
        # DB 기록이 사라진 채 Calendar에만 이벤트가 남은 상황을 만든다.
        self.google.events[event_id] = {
            "id": event_id,
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/old-code-001"}
                ]
            },
        }
        confirmation, _ = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )
        self.assertEqual(confirmation["meet_url"], "https://meet.google.com/old-code-001")
        self.assertEqual(len(self.google.events), 1)
        self.assertEqual(self.google.counts("POST"), 0)

    async def test_concurrent_insert_conflict_reuses_existing_event(self):
        """GET에서는 없다가 POST에서 409가 나는 경쟁 상태를 확인한다."""
        poll = await self.make_poll()
        event_id = deterministic_event_id(
            meeting_date="2026-10-11", team_channel="C11111111", is_test=False
        )
        original_request = self.google.request

        async def racing_request(method, url, **kwargs):
            if method == "POST" and event_id not in self.google.events:
                # 다른 요청이 먼저 만들어 버린 것처럼 끼워 넣는다.
                self.google.events[event_id] = self.google._materialize(
                    event_id, {}, {"conferenceData": {"createRequest": {"requestId": "x"}}}
                )
            return await original_request(method, url, **kwargs)

        with patch.object(calendar_meet, "_request", racing_request):
            confirmation, _ = await confirm_meeting_time(
                self.slack, poll=poll, slot="20:00~21:00"
            )
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(len(self.google.events), 1)

    async def test_time_change_updates_the_same_event(self):
        poll = await self.make_poll()
        first, _ = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        changed, _ = await confirm_meeting_time(self.slack, poll=poll, slot="22:00~23:00")

        self.assertEqual(changed["calendar_event_id"], first["calendar_event_id"])
        self.assertEqual(changed["meet_url"], first["meet_url"])
        self.assertEqual(changed["starts_at"], "2026-10-11T22:00:00+09:00")
        stored_event = self.google.events[changed["calendar_event_id"]]
        self.assertEqual(stored_event["start"]["dateTime"], "2026-10-11T22:00:00+09:00")
        self.assertEqual(stored_event["end"]["dateTime"], "2026-10-11T23:00:00+09:00")
        # 회의는 다시 만들지 않는다.
        self.assertEqual(self.google.counts("POST"), 1)

    async def test_update_does_not_resend_conference_or_event_id(self):
        """이미 회의가 붙은 이벤트에는 conferenceData를 다시 보내지 않는다."""
        poll = await self.make_poll()
        await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        await confirm_meeting_time(self.slack, poll=poll, slot="21:00~22:00")
        patch_bodies = [
            body
            for (method, _), body in zip(self.google.calls, self.google.bodies)
            if method == "PATCH"
        ]
        self.assertTrue(patch_bodies)
        for body in patch_bodies:
            self.assertNotIn("conferenceData", body)
            self.assertNotIn("id", body)

    async def test_event_without_conference_gets_one_on_update(self):
        poll = await self.make_poll()
        event_id = deterministic_event_id(
            meeting_date="2026-10-11", team_channel="C11111111", is_test=False
        )
        # 회의 없이 이벤트만 남아 있는 경우에도 Meet을 붙여 준다.
        self.google.events[event_id] = {"id": event_id}
        confirmation, _ = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )
        self.assertTrue(confirmation["meet_url"].startswith("https://meet.google.com/"))
        self.assertEqual(self.google.counts("POST"), 0)

    async def test_teams_get_separate_events_for_the_same_slot(self):
        green = await self.make_poll(team_channel="C11111111")
        blue = await self.make_poll(team_channel="C22222222")
        green_confirmation, _ = await confirm_meeting_time(
            self.slack, poll=green, slot="20:00~21:00"
        )
        blue_confirmation, _ = await confirm_meeting_time(
            self.slack, poll=blue, slot="20:00~21:00"
        )
        self.assertNotEqual(
            green_confirmation["calendar_event_id"],
            blue_confirmation["calendar_event_id"],
        )
        self.assertNotEqual(green_confirmation["meet_url"], blue_confirmation["meet_url"])
        self.assertEqual(len(self.google.events), 2)

    async def test_confirmation_goes_where_the_poll_was_posted(self):
        poll = await self.make_poll()
        await mark_poll_posted(poll["id"], "1700000000.0009", "C0ANNOUNCE")
        poll = await get_poll(poll["id"])
        await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        message = self.slack.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C0ANNOUNCE")
        self.assertIn("<#C11111111>에서 해요", message["blocks"][2]["elements"][0]["text"])

        # 시간을 바꾸면 같은 채널의 확정 공지를 고친다.
        await confirm_meeting_time(self.slack, poll=poll, slot="21:00~22:00")
        self.assertEqual(self.slack.chat_update.await_args.kwargs["channel"], "C0ANNOUNCE")
        self.slack.chat_postMessage.assert_awaited_once()

    async def test_attendees_are_invited_when_configured(self):
        poll = await self.make_poll()
        with patch.object(
            settings,
            "ONLINE_RETRO_TEAM_ATTENDEES",
            {"C11111111": ["member@example.com"]},
        ):
            await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        insert_body = next(
            body
            for method, body in zip([m for m, _ in self.google.calls], self.google.bodies)
            if method == "POST"
        )
        self.assertEqual(insert_body["attendees"], [{"email": "member@example.com"}])

    async def test_team_gmail_members_are_invited_with_the_guest_list_hidden(self):
        poll = await self.make_poll()
        profiles = {
            "U11111111": "Green.One@Gmail.com",
            "U22222222": "green.two@naver.com",
            "U33333333": "green3@googlemail.com",
            "U44444444": "blue@gmail.com",
        }
        self.slack.users_info = AsyncMock(
            side_effect=lambda user: {"user": {"profile": {"email": profiles[user]}}}
        )
        with patch.object(
            settings,
            "SUBMISSION_DESTINATIONS",
            {
                "U11111111": "C11111111",
                "U22222222": "C11111111",
                "U33333333": "C11111111",
                "U44444444": "C22222222",
            },
        ), patch.object(
            settings,
            "ONLINE_RETRO_TEAM_ATTENDEES",
            {"C11111111": ["green.one@gmail.com", "Outside@Company.com"]},
        ):
            _, warning = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")

        inserted = self.google.inserted()
        self.assertEqual(
            [attendee["email"] for attendee in inserted["attendees"]],
            ["green.one@gmail.com", "green3@googlemail.com", "outside@company.com"],
        )
        self.assertFalse(inserted["guestsCanSeeOtherGuests"])
        self.assertEqual(
            sorted(call.kwargs["user"] for call in self.slack.users_info.await_args_list),
            ["U11111111", "U22222222", "U33333333"],
        )
        self.assertEqual(warning, "")

    async def test_unreadable_profile_is_left_out(self):
        poll = await self.make_poll()

        def profile(user):
            if user == "U22222222":
                raise SlackApiError("user_not_found", {"ok": False, "error": "user_not_found"})
            return {"user": {"profile": {"email": "green@gmail.com"}}}

        self.slack.users_info = AsyncMock(side_effect=profile)
        with patch.object(
            settings,
            "SUBMISSION_DESTINATIONS",
            {"U11111111": "C11111111", "U22222222": "C11111111"},
        ):
            await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        self.assertEqual(self.google.inserted()["attendees"], [{"email": "green@gmail.com"}])

    async def test_no_attendees_leaves_a_warning_unless_meet_is_open(self):
        poll = await self.make_poll()
        self.slack.users_info = AsyncMock(return_value={"user": {"profile": {}}})
        with patch.object(
            settings, "SUBMISSION_DESTINATIONS", {"U11111111": "C11111111"}
        ), patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""):
            confirmation, warning = await confirm_meeting_time(
                self.slack, poll=poll, slot="20:00~21:00"
            )
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertNotIn("attendees", self.google.inserted())
        self.assertIn("users:read.email", warning)

        # 링크만으로 들어오는 OPEN이면 초대가 없어도 경고하지 않는다.
        _, warning = await confirm_meeting_time(self.slack, poll=poll, slot="21:00~22:00")
        self.assertEqual(warning, "")


class ConfirmFailureTest(ConfirmationTestCase):
    async def test_missing_credentials_stops_before_touching_google(self):
        poll = await self.make_poll()
        with patch.object(settings, "google_credentials", lambda: None):
            with self.assertRaisesRegex(ConfirmationError, "Google 인증정보"):
                await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        self.assertEqual(self.google.calls, [])
        self.assertIsNone(await get_confirmation(poll["id"]))

    async def test_failure_is_recorded_and_retry_succeeds(self):
        poll = await self.make_poll()
        # GET 404 뒤 POST가 403으로 막히는 상황.
        self.google.queued_failures = [(404, "notFound"), (403, "forbidden")]
        with self.assertRaises(ConfirmationError):
            await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")

        failed = await get_confirmation(poll["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("403", failed["last_error"])
        self.slack.chat_postMessage.assert_not_awaited()
        # 실패해도 이벤트 ID는 남아 재시도가 같은 이벤트를 겨냥한다.
        self.assertTrue(failed["calendar_event_id"])

        retried, _ = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        self.assertEqual(retried["status"], "confirmed")
        self.assertEqual(retried["calendar_event_id"], failed["calendar_event_id"])
        self.assertIsNone(retried["last_error"])
        self.assertEqual(len(self.google.events), 1)

    async def test_rate_limit_is_retried_with_backoff(self):
        poll = await self.make_poll()
        self.google.queued_failures = [(404, "notFound"), (429, "rateLimitExceeded")]
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with patch.object(calendar_meet.asyncio, "sleep", fake_sleep):
            confirmation, _ = await confirm_meeting_time(
                self.slack, poll=poll, slot="20:00~21:00"
            )
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 1)

    async def test_meet_access_type_failure_keeps_the_meeting(self):
        poll = await self.make_poll()
        self.google.meet_patch_error = (403, "forbidden")
        confirmation, warning = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertTrue(confirmation["meet_url"])
        self.assertIsNone(confirmation["meet_access_type"])
        self.assertIn("입장 정책", warning)
        self.slack.chat_postMessage.assert_awaited_once()

    async def test_access_type_is_left_alone_when_not_configured(self):
        poll = await self.make_poll()
        with patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""), patch.object(
            settings, "ONLINE_RETRO_TEAM_ATTENDEES", {"C11111111": ["member@gmail.com"]}
        ):
            confirmation, warning = await confirm_meeting_time(
                self.slack, poll=poll, slot="20:00~21:00"
            )
        self.assertEqual(warning, "")
        self.assertIsNone(confirmation["meet_access_type"])
        self.assertEqual(self.google.spaces, {})

    async def test_non_retryable_error_is_not_retried(self):
        poll = await self.make_poll()
        self.google.queued_failures = [(404, "notFound"), (400, "badRequest")]
        with self.assertRaises(ConfirmationError):
            await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        self.assertEqual(self.google.counts("POST"), 1)


class CancelMeetingTest(ConfirmationTestCase):
    async def test_cancel_marks_event_and_row_cancelled(self):
        poll = await self.make_poll()
        confirmation, _ = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )
        cancelled = await cancel_meeting(poll_id=poll["id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            self.google.events[confirmation["calendar_event_id"]]["status"], "cancelled"
        )
        # 취소된 모임은 더 이상 스케줄러 대상이 아니다.
        self.assertEqual(await list_confirmed(), [])

    async def test_cancelled_meeting_can_be_confirmed_again(self):
        poll = await self.make_poll()
        first, _ = await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        await cancel_meeting(poll_id=poll["id"])
        again, _ = await confirm_meeting_time(self.slack, poll=poll, slot="21:00~22:00")

        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(again["calendar_event_id"], first["calendar_event_id"])
        self.assertEqual(self.google.events[again["calendar_event_id"]]["status"], "confirmed")
        self.assertEqual(len(self.google.events), 1)

    async def test_cancel_without_confirmation_returns_none(self):
        poll = await self.make_poll()
        self.assertIsNone(await cancel_meeting(poll_id=poll["id"]))


class SchedulerIntegrationTest(ConfirmationTestCase):
    async def test_confirmed_meeting_feeds_the_announcement_scheduler(self):
        poll = await self.make_poll()
        confirmation, _ = await confirm_meeting_time(
            self.slack, poll=poll, slot="20:00~21:00"
        )
        with patch.object(settings, "ONLINE_RETRO_NOTIFY_MINUTES_BEFORE", 10):
            meetings = await _scheduled_meetings()

        self.assertEqual(len(meetings), 1)
        meeting = meetings[0]
        self.assertEqual(meeting.channel, "C11111111")
        self.assertEqual(meeting.url, confirmation["meet_url"])
        self.assertEqual(
            meeting.starts_at, datetime.datetime(2026, 10, 11, 20, 0, tzinfo=KST)
        )
        self.assertEqual(
            meeting.notify_at, datetime.datetime(2026, 10, 11, 19, 50, tzinfo=KST)
        )
        self.assertEqual(meeting.writing_minutes, settings.ONLINE_RETRO_WRITING_MINUTES)

    async def test_confirmed_meeting_overrides_env_schedule(self):
        from config import OnlineRetroMeeting

        configured = OnlineRetroMeeting(
            session_name="6기 1회차",
            starts_at=datetime.datetime(2026, 10, 11, 18, 0, tzinfo=KST),
            notify_at=datetime.datetime(2026, 10, 11, 17, 50, tzinfo=KST),
            channel="C11111111",
            url="https://meet.example.com/old",
        )
        poll = await self.make_poll()
        await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        with patch.object(settings, "ONLINE_RETRO_MEETINGS", [configured]):
            meetings = await _scheduled_meetings()
            resolved = await _meeting("6기 1회차", "C11111111")

        self.assertEqual(len(meetings), 1)
        self.assertEqual(
            meetings[0].starts_at, datetime.datetime(2026, 10, 11, 20, 0, tzinfo=KST)
        )
        self.assertNotEqual(resolved.url, configured.url)

    async def test_attendance_button_works_for_a_confirmed_meeting(self):
        poll = await self.make_poll()
        await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        body = {
            "actions": [
                {
                    "value": json.dumps(
                        {"session_name": "6기 1회차", "team_channel": "C11111111"},
                        ensure_ascii=False,
                    )
                }
            ],
            "user": {"id": "U11111111"},
            "channel": {"id": "C11111111"},
        }
        await handle_online_retro_attendance(AsyncMock(), body, self.slack)
        with get_connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM online_retro_attendance"
            ).fetchone()[0]
        self.assertEqual(total, 1)

    async def test_only_team_members_check_in_from_the_announcement(self):
        poll = await self.make_poll()
        await confirm_meeting_time(self.slack, poll=poll, slot="20:00~21:00")
        value = json.dumps(
            {"session_name": "6기 1회차", "team_channel": "C11111111"}, ensure_ascii=False
        )
        with patch.object(
            settings,
            "SUBMISSION_DESTINATIONS",
            {"U11111111": "C11111111", "U22222222": "C22222222"},
        ):
            for user_id in ("U22222222", "U11111111"):
                await handle_online_retro_attendance(
                    AsyncMock(),
                    {
                        "actions": [{"value": value}],
                        "user": {"id": user_id},
                        "channel": {"id": "C0ANNOUNCE"},
                    },
                    self.slack,
                )
                if user_id == "U22222222":
                    self.assertIn(
                        "다른 팀 모임이에요",
                        self.slack.chat_postEphemeral.await_args.kwargs["text"],
                    )
        with get_connection() as connection:
            attendees = [
                row[0]
                for row in connection.execute("SELECT user_id FROM online_retro_attendance")
            ]
        self.assertEqual(attendees, ["U11111111"])


class ConfirmationDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(
                settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")
            )
        )
        self.enterContext(
            patch.object(
                settings,
                "DASHBOARD_SESSION_SECRET",
                "test-session-secret-at-least-32-characters",
            )
        )
        self.enterContext(patch.object(settings, "ONLINE_RETRO_MEETINGS", []))
        self.enterContext(patch.object(settings, "GOOGLE_CALENDAR_ID", "primary"))
        self.enterContext(patch.object(settings, "GOOGLE_CALENDAR_TIMEZONE", "Asia/Seoul"))
        self.enterContext(patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", "OPEN"))
        self.enterContext(patch.object(settings, "ONLINE_RETRO_TEAM_ATTENDEES", {}))
        self.enterContext(
            patch.object(settings, "google_credentials", lambda: CREDENTIALS)
        )
        self.google = FakeGoogle()
        self.enterContext(patch.object(calendar_meet, "_request", self.google.request))
        self.enterContext(
            patch.object(
                calendar_meet, "get_access_token", AsyncMock(return_value="test-token")
            )
        )
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", ""))
        self.enterContext(patch.object(settings, "ANNOUNCEMENT_CHANNEL", "C0ANNOUNCE"))
        self.enterContext(
            patch(
                "dashboard.online_retro.tz_now",
                return_value=datetime.datetime(2026, 10, 5, 21, 0, tzinfo=KST),
            )
        )
        slack_directory.reset_cache()
        initialize_database()
        auth.create_admin("admin", "test-password-long")
        self.session_token = auth._create_session(1)
        poll = await create_poll(
            meeting_date="2026-10-11",
            session_name="6기 1회차",
            team_channel="C11111111",
            team_name="그린팀",
            slots=ALL_OPTIONS,
            is_test=False,
        )
        self.poll_id = poll["id"]
        await save_vote(poll_id=self.poll_id, user_id="U11111111", slots=["20:00~21:00"])
        await save_vote(
            poll_id=self.poll_id, user_id="U22222222", slots=["20:00~21:00", "21:00~22:00"]
        )
        self.slack = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "1700000000.0001"}),
            chat_update=AsyncMock(),
            auth_test=AsyncMock(return_value={"url": "https://example.slack.com/"}),
            users_list=AsyncMock(return_value={"members": []}),
            conversations_info=AsyncMock(
                side_effect=lambda channel: {"channel": {"name": CHANNEL_NAMES[channel]}}
            ),
        )
        app = web.Application()
        register_dashboard_routes(app, self.slack)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"{auth.SESSION_COOKIE}={self.session_token}"}

    def _csrf(self) -> str:
        return auth._csrf_token(self.session_token)

    async def test_page_requires_admin_session(self):
        response = await self.client.get("/online-retro", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertTrue(response.headers["Location"].startswith("/login"))

    async def test_page_shows_vote_counts_and_confirm_button(self):
        response = await self.client.get("/online-retro", headers=self._headers())
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("그린팀", body)
        self.assertIn("2명", body)
        self.assertIn("시간 확정 및 Meet 생성", body)

    async def test_confirm_requires_csrf_token(self):
        response = await self.client.post(
            f"/online-retro/{self.poll_id}/confirm",
            data={"slot": "20:00~21:00"},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 403)
        self.assertIsNone(await get_confirmation(self.poll_id))
        self.assertEqual(self.google.calls, [])

    async def test_confirm_creates_meeting_and_shows_it(self):
        response = await self.client.post(
            f"/online-retro/{self.poll_id}/confirm",
            data={"slot": "20:00~21:00", "csrf_token": self._csrf()},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertIn("done=confirmed", response.headers["Location"])

        confirmation = await get_confirmation(self.poll_id)
        self.assertEqual(confirmation["status"], "confirmed")

        body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn(confirmation["meet_url"], body)
        self.assertIn("시간 변경 및 Meet 갱신", body)
        self.assertIn("확정 취소", body)

    async def test_second_confirm_updates_instead_of_creating(self):
        for slot in ("20:00~21:00", "21:00~22:00"):
            response = await self.client.post(
                f"/online-retro/{self.poll_id}/confirm",
                data={"slot": slot, "csrf_token": self._csrf()},
                headers=self._headers(),
                allow_redirects=False,
            )
            self.assertEqual(response.status, 302)
        self.assertIn("done=updated", response.headers["Location"])
        self.assertEqual(self.google.counts("POST"), 1)
        self.assertEqual(len(self.google.events), 1)

    async def test_rejected_slot_does_not_reach_google(self):
        response = await self.client.post(
            f"/online-retro/{self.poll_id}/confirm",
            data={"slot": UNAVAILABLE, "csrf_token": self._csrf()},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(self.google.calls, [])

    async def test_cancel_route_cancels_the_event(self):
        await self.client.post(
            f"/online-retro/{self.poll_id}/confirm",
            data={"slot": "20:00~21:00", "csrf_token": self._csrf()},
            headers=self._headers(),
            allow_redirects=False,
        )
        response = await self.client.post(
            f"/online-retro/{self.poll_id}/cancel",
            data={"csrf_token": self._csrf()},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertIn("done=cancelled", response.headers["Location"])
        confirmation = await get_confirmation(self.poll_id)
        self.assertEqual(confirmation["status"], "cancelled")

    async def test_missing_credentials_are_flagged_on_the_page(self):
        with patch.object(settings, "google_credentials", lambda: None):
            body = await (
                await self.client.get("/online-retro", headers=self._headers())
            ).text()
        self.assertIn("Google 인증정보가 없어", body)

    async def test_confirm_notice_keeps_the_attendee_warning(self):
        with patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""):
            response = await self.client.post(
                f"/online-retro/{self.poll_id}/confirm",
                data={"slot": "20:00~21:00", "csrf_token": self._csrf()},
                headers=self._headers(),
                allow_redirects=False,
            )
        body = await (
            await self.client.get(response.headers["Location"], headers=self._headers())
        ).text()
        self.assertIn("시간을 확정하고 Google Meet 링크를 만들었습니다.", body)
        self.assertIn("users:read.email", body)

    def _teams(self):
        # 블루팀(C22222222) 1명, 옐로우팀(C33333333) 2명.
        return patch.object(
            settings,
            "SUBMISSION_DESTINATIONS",
            {"U22222222": "C22222222", "U33333333": "C33333333", "U44444444": "C33333333"},
        )

    async def _post_polls(self, **fields: str):
        data = {"csrf_token": self._csrf(), "meeting_date": "2026-10-11", **fields}
        return await self.client.post(
            "/online-retro/polls", data=data, headers=self._headers(), allow_redirects=False
        )

    def _posted_channels(self) -> list[str]:
        return [call.kwargs["channel"] for call in self.slack.chat_postMessage.await_args_list]

    async def _new_polls(self) -> dict[str, dict]:
        # setUp에서 만든 C11111111 투표는 팀 배정 밖이라 이 테스트들과 무관하다.
        return {
            poll["team_channel"]: poll
            for poll in await list_polls()
            if poll["team_channel"] != "C11111111"
        }

    async def test_page_offers_poll_for_the_next_second_sunday(self):
        with self._teams():
            body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn('data-dialog="poll-dialog"', body)
        self.assertIn('value="2026-10-11"', body)
        self.assertIn("6기 5회차", body)
        self.assertIn("#announcement", body)
        self.assertIn("<b>블루팀</b> · 팀원 1명", body)
        self.assertIn("<b>옐로우팀</b> · 팀원 2명", body)
        self.assertNotIn('name="team_name_', body)
        self.assertIn('name="intro"', body)
        self.assertIn(escape(DEFAULT_POLL_INTRO), body)

    async def test_one_poll_goes_to_the_announcement_channel(self):
        with self._teams():
            response = await self._post_polls()
        self.assertEqual(response.status, 302)
        location = unquote(response.headers["Location"])
        self.assertIn("polls=", location)
        self.assertIn("6기 5회차", location)
        self.assertIn("공지 채널에 올렸습니다: 블루팀, 옐로우팀", location)
        self.assertEqual(self._posted_channels(), ["C0ANNOUNCE"])
        progress = self.slack.chat_postMessage.await_args.kwargs["blocks"][1]["text"]["text"]
        self.assertIn("블루팀 0/1 · 옐로우팀 0/2", progress)
        polls = await self._new_polls()
        self.assertEqual(polls["C22222222"]["team_name"], "블루팀")
        self.assertEqual(polls["C33333333"]["team_name"], "옐로우팀")
        for poll in polls.values():
            self.assertEqual(poll["meeting_date"], "2026-10-11")
            self.assertEqual(poll["session_name"], "6기 5회차")
            self.assertFalse(poll["is_test"])
            self.assertEqual(poll["slack_ts"], "1700000000.0001")
            self.assertEqual(poll["message_channel"], "C0ANNOUNCE")
            self.assertIsNone(poll["intro_template"])

    async def test_edited_intro_is_rendered_and_offered_next_time(self):
        intro = "*{날짜}* 온라인 회고, 가능한 시간을 골라 주세요!"
        with self._teams():
            await self._post_polls(intro=intro)
            body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        header = self.slack.chat_postMessage.await_args.kwargs["blocks"][0]["text"]["text"]
        self.assertEqual(header, "*10월 11일(일)* 온라인 회고, 가능한 시간을 골라 주세요!")
        polls = await self._new_polls()
        self.assertEqual({poll["intro_template"] for poll in polls.values()}, {intro})
        self.assertIn(escape(intro), body)

    async def test_blank_or_default_intro_follows_the_default(self):
        with self._teams():
            await self._post_polls(intro=DEFAULT_POLL_INTRO)
            await self._post_polls(meeting_date="2026-10-18", intro="   ")
        polls = [poll for poll in await list_polls() if poll["team_channel"] != "C11111111"]
        self.assertEqual(len(polls), 4)
        self.assertTrue(all(poll["intro_template"] is None for poll in polls))

    async def test_too_long_intro_is_rejected(self):
        with self._teams():
            response = await self._post_polls(intro="가" * 1001)
        self.assertIn("1000자까지", unquote(response.headers["Location"]))
        self.slack.chat_postMessage.assert_not_awaited()
        self.assertEqual(await self._new_polls(), {})

    async def test_second_press_does_not_post_again(self):
        with self._teams():
            await self._post_polls()
            response = await self._post_polls()
            body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertEqual(self._posted_channels(), ["C0ANNOUNCE"])
        self.assertIn("이미 공지 채널에 올라가 있어요", unquote(response.headers["Location"]))
        self.assertIn("이미 올림", body)

    async def test_failed_post_is_retried_with_the_new_intro(self):
        self.slack.chat_postMessage.side_effect = [
            SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"}),
            {"ts": "1700000000.0002"},
        ]
        with self._teams():
            response = await self._post_polls()
            location = unquote(response.headers["Location"])
            self.assertIn("error=", location)
            self.assertIn("공지 채널에 올리지 못했어요(not_in_channel)", location)
            self.assertTrue(all(not poll["slack_ts"] for poll in (await self._new_polls()).values()))

            # 아직 Slack에 없는 투표라 고친 문구로 다시 올라간다.
            response = await self._post_polls(intro="{날짜} 다시 올립니다")
        self.assertIn("polls=", unquote(response.headers["Location"]))
        self.assertEqual(self._posted_channels(), ["C0ANNOUNCE", "C0ANNOUNCE"])
        for poll in (await self._new_polls()).values():
            self.assertEqual(poll["slack_ts"], "1700000000.0002")
            self.assertEqual(poll["intro_template"], "{날짜} 다시 올립니다")

    async def test_missing_announcement_channel_posts_nothing(self):
        with self._teams(), patch.object(settings, "ANNOUNCEMENT_CHANNEL", ""):
            response = await self._post_polls()
        self.assertIn("ANNOUNCEMENT_CHANNEL", unquote(response.headers["Location"]))
        self.slack.chat_postMessage.assert_not_awaited()
        self.assertEqual(await self._new_polls(), {})

    async def test_dates_without_an_open_session_post_nothing(self):
        cases = {
            "2026-10-04": "지난 날짜",
            "2027-06-13": "열려 있는 회차가 없어요",
            "not-a-date": "날짜 형식",
        }
        with self._teams():
            for meeting_date, message in cases.items():
                with self.subTest(meeting_date=meeting_date):
                    response = await self._post_polls(meeting_date=meeting_date)
                    self.assertIn(message, unquote(response.headers["Location"]))
        self.slack.chat_postMessage.assert_not_awaited()
        self.assertEqual(await self._new_polls(), {})

    async def test_page_groups_meetings_and_preselects_the_top_slot(self):
        for meeting_date, is_test in (("2026-09-13", False), ("2026-11-08", True)):
            await create_poll(
                meeting_date=meeting_date,
                session_name="6기 1회차",
                team_channel="C22222222",
                team_name="블루팀",
                slots=ALL_OPTIONS,
                is_test=is_test,
            )
        body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        upcoming = body.index("10월 11일(일) · 6기 1회차")
        self.assertLess(upcoming, body.index("<summary>지난 투표 1건</summary>"))
        self.assertLess(upcoming, body.index("<summary>테스트 투표 1건</summary>"))
        self.assertIn("확정 0/1팀", body)
        # setUp 투표는 20:00~21:00이 2표로 가장 많다.
        self.assertIn('<option value="20:00~21:00" selected>', body)

    async def test_card_shows_who_chose_what_and_who_has_not_answered(self):
        self.slack.users_list.return_value = {
            "members": [
                {"id": "U11111111", "profile": {"display_name": "민수"}},
                {"id": "U22222222", "profile": {"display_name": "영희"}},
                {"id": "U33333333", "profile": {"display_name": "철수"}},
            ]
        }
        with patch.object(
            settings,
            "SUBMISSION_DESTINATIONS",
            {"U11111111": "C11111111", "U22222222": "C11111111", "U33333333": "C11111111"},
        ):
            body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn("<summary>응답 2명 · 미응답 1명</summary>", body)
        self.assertIn("<li><b>민수</b> 20시</li>", body)
        self.assertIn("<li><b>영희</b> 20시, 21시</li>", body)
        self.assertIn("미응답: 철수", body)
        self.assertIn('title="민수, 영희">20:00~21:00</span>', body)

    async def test_date_heading_shows_when_unanswered_members_get_a_dm(self):
        body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn("<small>미응답 DM 10월 9일(금) 20:00 예정</small>", body)

        await mark_announcement_sent(reminder_key(self.poll_id, "U33333333"))
        body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn("<small>미응답 DM 1명 보냄 · 10월 9일(금) 20:00</small>", body)

    async def test_afternoon_votes_show_on_polls_made_before_the_change(self):
        older_slots = ["18:00~19:00", "19:00~20:00", "20:00~21:00", "21:00~22:00", "22:00~23:00", UNAVAILABLE]
        with get_connection() as connection:
            connection.execute(
                "UPDATE online_retro_time_polls SET slots_json = ? WHERE id = ?",
                (json.dumps(older_slots, ensure_ascii=False), self.poll_id),
            )
        await save_vote(poll_id=self.poll_id, user_id="U33333333", slots=["14:00~15:00"])
        body = await (await self.client.get("/online-retro", headers=self._headers())).text()
        self.assertIn('title="U33333333">14:00~15:00</span>', body)
        self.assertIn('<option value="13:00~14:00">', body)
        polls = {poll["id"]: poll for poll in await list_polls()}
        self.assertEqual(polls[self.poll_id]["counts"]["14:00~15:00"], 1)

    async def test_test_poll_on_a_real_team_cannot_be_confirmed(self):
        poll = await create_poll(
            meeting_date="2026-10-11",
            session_name="6기 5회차",
            team_channel="C22222222",
            team_name="블루팀",
            slots=ALL_OPTIONS,
            is_test=True,
        )
        with self._teams():
            body = await (await self.client.get("/online-retro", headers=self._headers())).text()
            response = await self.client.post(
                f"/online-retro/{poll['id']}/confirm",
                data={"slot": "20:00~21:00", "csrf_token": self._csrf()},
                headers=self._headers(),
                allow_redirects=False,
            )
        self.assertIn("실제 팀을 가리키는 테스트 투표라 확정하지 않습니다", body)
        self.assertIn("확정할 수 없어요", unquote(response.headers["Location"]))
        self.assertIsNone(await get_confirmation(poll["id"]))
        self.assertEqual(self.google.calls, [])

    async def test_poll_requires_csrf_token(self):
        with self._teams():
            response = await self._post_polls(csrf_token="")
        self.assertEqual(response.status, 403)
        self.slack.chat_postMessage.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
