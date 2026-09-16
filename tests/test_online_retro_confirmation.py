"""시간 확정과 Google Meet 생성 테스트.

Google API는 전부 mock으로 대체한다. `FakeGoogle`은 실제 Calendar처럼 이벤트를
ID로 저장하고 같은 ID로 다시 insert하면 409를 돌려주므로, 중복 생성 방지와
재시도 동작을 실제 계약에 가깝게 확인할 수 있다.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import GoogleCredentials, settings
from dashboard import auth, register_dashboard_routes
from dashboard import directory as slack_directory
from database.online_retro_confirmation import get_confirmation, list_confirmed
from database.online_retro_poll import create_poll, get_poll, save_vote
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
from slack.events.online_retro_poll import ALL_OPTIONS, UNAVAILABLE

KST = ZoneInfo("Asia/Seoul")
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
        with patch.object(settings, "GOOGLE_MEET_ACCESS_TYPE", ""):
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
            conversations_info=AsyncMock(return_value={"channel": {"name": "green"}}),
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


if __name__ == "__main__":
    unittest.main()
