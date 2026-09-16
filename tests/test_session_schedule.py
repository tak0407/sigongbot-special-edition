"""회차 일정과 매회차 제출 공지를 DB에서 읽고 관리자 웹에서 편집하는 흐름."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from constants import (
    ANNOUNCE_LEAD,
    DEFAULT_SUBMISSION_ANNOUNCEMENT,
    DUE_DATES,
    SESSION_NAMES,
)
from dashboard import auth, register_dashboard_routes
from database import sessions
from database.sqlite import get_connection, initialize_database
from slack.session_announcement import post_due_announcements
from utils import get_current_session_info, tz_now

KST = ZoneInfo("Asia/Seoul")


class ScheduleTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db"))
        )
        self.enterContext(
            patch.object(
                settings,
                "DASHBOARD_SESSION_SECRET",
                "test-session-secret-at-least-32-characters",
            )
        )
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", ""))
        initialize_database()
        self.addCleanup(sessions.invalidate)
        auth.create_admin("admin", "test-password-long")
        self.session_token = auth._create_session(1)

        app = web.Application()
        register_dashboard_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"{auth.SESSION_COOKIE}={self.session_token}"}

    def _form(self, **fields) -> dict[str, str]:
        return {"csrf_token": auth._csrf_token(self.session_token), **fields}

    async def _post(self, path: str, **fields):
        return await self.client.post(
            path, data=self._form(**fields), headers=self._headers(), allow_redirects=False
        )

    @staticmethod
    def _query(response) -> dict:
        return parse_qs(urlparse(response.headers["Location"]).query)


class ScheduleSeedTest(ScheduleTestCase):
    async def test_migration_seeds_the_schedule_from_constants(self):
        """전환 시점의 동작이 같으려면 시드가 코드 상수와 정확히 일치해야 한다."""
        names, dues = sessions.load_schedule()
        self.assertEqual(names, list(SESSION_NAMES))
        self.assertEqual(dues, list(DUE_DATES))

    async def test_current_session_follows_the_database(self):
        """DB에서 마감을 옮기면 회차 판정이 따라와야 한다."""
        sessions.invalidate()
        with get_connection() as connection:
            connection.execute("DELETE FROM sessions")
            connection.execute(
                "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
                ("9기 1회차", datetime.datetime(2099, 1, 8, 5, 0, tzinfo=KST).isoformat()),
            )
            connection.execute(
                "INSERT INTO sessions (name, due_at) VALUES (?, ?)",
                ("9기 2회차", datetime.datetime(2099, 1, 15, 5, 0, tzinfo=KST).isoformat()),
            )
        sessions.invalidate()

        moment = datetime.datetime(2099, 1, 10, 12, 0, tzinfo=KST)
        _, name, _, is_active = get_current_session_info(moment)
        self.assertEqual(name, "9기 2회차")
        self.assertTrue(is_active)

    async def test_falls_back_to_constants_when_the_table_is_empty(self):
        """일정을 못 읽으면 제출 자체가 막히므로 코드 상수로 되돌아간다."""
        with get_connection() as connection:
            connection.execute("DELETE FROM sessions")
        sessions.invalidate()
        names, _ = sessions.load_schedule()
        self.assertEqual(names, list(SESSION_NAMES))


class ScheduleEditTest(ScheduleTestCase):
    def _future(self, days: int) -> datetime.datetime:
        last = sessions.load_schedule()[1][-1]
        return last + datetime.timedelta(days=days)

    async def test_adds_a_session_after_the_last_one(self):
        due = self._future(7)
        response = await self._post(
            "/schedule/add", name="7기 1회차", due_at=due.strftime("%Y-%m-%dT%H:%M")
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(self._query(response)["added"], ["7기 1회차"])

        names, dues = sessions.load_schedule()
        self.assertEqual(names[-1], "7기 1회차")
        self.assertEqual(dues[-1], due)

    async def test_rejects_a_session_that_would_break_the_order(self):
        """중간에 끼워 넣으면 이미 제출된 회고가 다른 회차 구간으로 넘어간다."""
        earlier = sessions.load_schedule()[1][0] - datetime.timedelta(days=1)
        response = await self._post(
            "/schedule/add", name="끼어드는 회차", due_at=earlier.strftime("%Y-%m-%dT%H:%M")
        )
        self.assertIn("마지막 회차", self._query(response)["error"][0])
        self.assertNotIn("끼어드는 회차", sessions.load_schedule()[0])

    async def test_rejects_a_duplicate_name(self):
        existing = sessions.load_schedule()[0][-1]
        response = await self._post(
            "/schedule/add",
            name=existing,
            due_at=self._future(7).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertIn("이미 있는 회차", self._query(response)["error"][0])

    async def test_rejects_a_name_with_unexpected_characters(self):
        response = await self._post(
            "/schedule/add",
            name="<script>7기</script>",
            due_at=self._future(7).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertIn("회차 이름", self._query(response)["error"][0])

    async def test_moves_the_due_date_of_an_upcoming_session(self):
        name = "7기 1회차"
        await self._post(
            "/schedule/add", name=name, due_at=self._future(7).strftime("%Y-%m-%dT%H:%M")
        )
        moved = self._future(10)
        response = await self._post(
            "/schedule/due", name=name, due_at=moved.strftime("%Y-%m-%dT%H:%M")
        )
        self.assertEqual(self._query(response)["updated"], [name])
        names, dues = sessions.load_schedule()
        self.assertEqual(dues[names.index(name)], moved)

    async def test_refuses_to_move_a_closed_session(self):
        """지난 회차를 옮기면 그 구간 제출이 다른 회차로 집계된다."""
        past = sessions.load_schedule()[0][0]
        response = await self._post(
            "/schedule/due",
            name=past,
            due_at=self._future(7).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertIn("이미 마감된", self._query(response)["error"][0])

    async def test_refuses_a_due_date_in_the_past(self):
        name = "7기 1회차"
        await self._post(
            "/schedule/add", name=name, due_at=self._future(7).strftime("%Y-%m-%dT%H:%M")
        )
        response = await self._post(
            "/schedule/due", name=name, due_at="2020-01-01T05:00"
        )
        self.assertIn("현재보다 뒤여야", self._query(response)["error"][0])

    async def test_editing_requires_a_session_and_csrf_token(self):
        response = await self.client.post(
            "/schedule/add",
            data={"name": "7기 1회차", "due_at": "2099-01-01T05:00"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertTrue(response.headers["Location"].startswith("/login"))

        response = await self.client.post(
            "/schedule/add",
            data={"name": "7기 1회차", "due_at": "2099-01-01T05:00"},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 403)

    async def test_page_only_offers_forms_for_upcoming_sessions(self):
        response = await self.client.get("/schedule?all=1", headers=self._headers())
        body = await response.text()
        self.assertIn("회차 추가", body)
        # 마감된 회차 행에는 변경 폼이 없다.
        closed = sessions.load_schedule()[0][0]
        row = body[body.index(f"<td>{closed}</td>") :][:600]
        self.assertNotIn("/schedule/due", row)


class AnnouncementTestCase(ScheduleTestCase):
    """공지 발송/편집 테스트가 함께 쓰는 준비 코드."""

    FUTURE = datetime.datetime(2099, 3, 10, 5, 0, tzinfo=KST)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(settings, "ANNOUNCEMENT_CHANNEL", "C99999999"))
        self.slack = SimpleNamespace(chat_postMessage=AsyncMock())

    def _replace_schedule(self, rows: list[tuple]) -> None:
        """(이름, 마감, 공지 시각, 문구) 목록으로 일정을 통째로 갈아 끼운다."""
        with get_connection() as connection:
            connection.execute("DELETE FROM sessions")
            for name, due, announce, body in rows:
                connection.execute(
                    "INSERT INTO sessions (name, due_at, announce_at, announcement)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        name,
                        due.isoformat(),
                        announce.isoformat() if announce else None,
                        body,
                    ),
                )
        sessions.invalidate()

    @staticmethod
    def _posted(mock) -> str:
        return mock.await_args.kwargs["blocks"][0]["text"]["text"]


class AnnouncementSeedTest(AnnouncementTestCase):
    async def test_migration_gives_every_session_an_announce_time(self):
        """공지 시각이 비면 그 회차만 조용히 공지가 빠진다."""
        listed = sessions.list_sessions()
        self.assertEqual(len(listed), len(SESSION_NAMES))
        for row in listed:
            self.assertEqual(row["announce_at"], row["due_at"] - ANNOUNCE_LEAD)
            self.assertIsNone(row["announcement"])

    async def test_default_body_is_seeded_from_constants(self):
        self.assertEqual(sessions.get_template(), DEFAULT_SUBMISSION_ANNOUNCEMENT)

    async def test_placeholders_are_replaced_at_send_time(self):
        rendered = sessions.render_announcement(
            "{회차} 마감은 {마감}", name="6기 2회차", due_at=self.FUTURE
        )
        self.assertEqual(rendered, "6기 2회차 마감은 3월 10일 화요일 05:00")

    async def test_a_stray_brace_does_not_swallow_the_announcement(self):
        """str.format을 쓰면 중괄호 하나에 그 회차 공지가 통째로 빠진다."""
        rendered = sessions.render_announcement(
            "{회차} {오타 {", name="6기 2회차", due_at=self.FUTURE
        )
        self.assertEqual(rendered, "6기 2회차 {오타 {")


class AnnouncementSendingTest(AnnouncementTestCase):
    async def test_posts_once_when_the_announce_time_arrives(self):
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [("9기 1회차", self.FUTURE, now - datetime.timedelta(hours=1), None)]
        )

        self.assertEqual(await post_due_announcements(self.slack, now=now), 1)
        self.assertEqual(await post_due_announcements(self.slack, now=now), 0)
        self.slack.chat_postMessage.assert_awaited_once()

        message = self.slack.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C99999999")
        self.assertIn("9기 1회차", self._posted(self.slack.chat_postMessage))
        self.assertIsNotNone(sessions.get_session("9기 1회차")["announced_at"])

    async def test_does_not_send_before_the_announce_time(self):
        now = datetime.datetime(2099, 3, 1, 9, 0, tzinfo=KST)
        self._replace_schedule(
            [("9기 1회차", self.FUTURE, now + datetime.timedelta(days=1), None)]
        )
        self.assertEqual(await post_due_announcements(self.slack, now=now), 0)
        self.slack.chat_postMessage.assert_not_awaited()

    async def test_skips_sessions_whose_due_date_already_passed(self):
        """봇이 오래 멈췄다 올라와도 묵은 공지가 한꺼번에 나가면 안 된다."""
        closed = datetime.datetime(2099, 1, 5, 5, 0, tzinfo=KST)
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [
                ("9기 0회차", closed, closed - ANNOUNCE_LEAD, None),
                ("9기 1회차", self.FUTURE, now - datetime.timedelta(hours=1), None),
            ]
        )
        self.assertEqual(await post_due_announcements(self.slack, now=now), 1)
        self.assertIn("9기 1회차", self._posted(self.slack.chat_postMessage))

    async def test_a_session_body_overrides_the_default(self):
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [
                (
                    "9기 1회차",
                    self.FUTURE,
                    now - datetime.timedelta(hours=1),
                    "이번 주는 온라인 모임이 있어요. {회차}",
                )
            ]
        )
        await post_due_announcements(self.slack, now=now)
        self.assertEqual(
            self._posted(self.slack.chat_postMessage),
            "이번 주는 온라인 모임이 있어요. 9기 1회차",
        )

    async def test_dropping_the_mention_also_drops_it_from_the_fallback(self):
        """본문에서 <!here>를 지웠는데 폴백에 남으면 멘션이 그대로 나간다."""
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [("9기 1회차", self.FUTURE, now - datetime.timedelta(hours=1), "조용한 공지")]
        )
        await post_due_announcements(self.slack, now=now)
        self.assertNotIn("<!here>", self.slack.chat_postMessage.await_args.kwargs["text"])

    async def test_announcement_carries_the_submit_button(self):
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [("9기 1회차", self.FUTURE, now - datetime.timedelta(hours=1), None)]
        )
        await post_due_announcements(self.slack, now=now)
        button = self.slack.chat_postMessage.await_args.kwargs["blocks"][1]["elements"][0]
        self.assertEqual(button["action_id"], "start_retrospective_from_announcement")
        self.assertEqual(json.loads(button["value"])["session_name"], "9기 1회차")
        self.assertFalse(json.loads(button["value"])["test_mode"])

    async def test_sends_nothing_without_an_announcement_channel(self):
        now = datetime.datetime(2099, 3, 6, 20, 0, tzinfo=KST)
        self._replace_schedule(
            [("9기 1회차", self.FUTURE, now - datetime.timedelta(hours=1), None)]
        )
        with patch.object(settings, "ANNOUNCEMENT_CHANNEL", ""):
            self.assertEqual(await post_due_announcements(self.slack, now=now), 0)
        self.slack.chat_postMessage.assert_not_awaited()
        self.assertIsNone(sessions.get_session("9기 1회차")["announced_at"])


class AnnouncementEditTest(AnnouncementTestCase):
    def _upcoming(self) -> str:
        return sessions.load_schedule()[0][-1]

    async def test_saves_the_default_body_for_every_session(self):
        response = await self._post(
            "/schedule/announcement/template", body="새 기본 문구 {회차}"
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(sessions.get_template(), "새 기본 문구 {회차}")

    async def test_rejects_an_empty_default_body(self):
        response = await self._post("/schedule/announcement/template", body="   ")
        self.assertIn("비어 있습니다", self._query(response)["error"][0])
        self.assertEqual(sessions.get_template(), DEFAULT_SUBMISSION_ANNOUNCEMENT)

    async def test_overrides_and_restores_a_single_session(self):
        name = self._upcoming()
        response = await self._post(
            "/schedule/announcement", name=name, body="이 회차만 다른 문구"
        )
        self.assertEqual(self._query(response)["saved"], ["1"])
        self.assertEqual(sessions.get_session(name)["announcement"], "이 회차만 다른 문구")

        response = await self._post("/schedule/announcement", name=name, body="", reset="1")
        self.assertEqual(self._query(response)["reset"], ["1"])
        self.assertIsNone(sessions.get_session(name)["announcement"])

    async def test_moves_and_turns_off_the_announce_time(self):
        name = self._upcoming()
        due = sessions.get_session(name)["due_at"]
        moved = due - datetime.timedelta(days=2)
        response = await self._post(
            "/schedule/announcement/time",
            name=name,
            announce_at=moved.strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertIn("scheduled", self._query(response))
        self.assertEqual(sessions.get_session(name)["announce_at"], moved)

        response = await self._post(
            "/schedule/announcement/time",
            name=name,
            announce_at=moved.strftime("%Y-%m-%dT%H:%M"),
            off="1",
        )
        self.assertEqual(self._query(response)["off"], ["1"])
        self.assertIsNone(sessions.get_session(name)["announce_at"])

    async def test_refuses_an_announce_time_after_the_due_date(self):
        """마감 뒤로 넘어간 공지 시각은 영영 오지 않아 그 회차 공지가 사라진다."""
        name = self._upcoming()
        due = sessions.get_session(name)["due_at"]
        response = await self._post(
            "/schedule/announcement/time",
            name=name,
            announce_at=(due + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertIn("마감보다 앞서야", self._query(response)["error"][0])

    async def test_refuses_to_edit_a_closed_session(self):
        past = sessions.load_schedule()[0][0]
        response = await self._post("/schedule/announcement", name=past, body="늦은 수정")
        self.assertIn("이미 마감된", self._query(response)["error"][0])

    async def test_refuses_to_edit_an_announcement_already_sent(self):
        """문구를 고쳐도 다시 보내지 않으므로, 고쳐진 줄 알면 안 된다."""
        name = self._upcoming()
        sessions.mark_announced(name, tz_now())
        response = await self._post("/schedule/announcement", name=name, body="수정")
        self.assertIn("이미 나갔습니다", self._query(response)["error"][0])
        self.assertIsNone(sessions.get_session(name)["announcement"])

    async def test_moving_the_due_date_moves_the_announcement_with_it(self):
        """공지 시각만 남으면 마감 뒤로 밀려 그 회차 공지가 조용히 빠진다."""
        name = "7기 1회차"
        due = sessions.load_schedule()[1][-1] + datetime.timedelta(days=7)
        await self._post("/schedule/add", name=name, due_at=due.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(
            sessions.get_session(name)["announce_at"], due - ANNOUNCE_LEAD
        )

        moved = due + datetime.timedelta(days=3)
        await self._post("/schedule/due", name=name, due_at=moved.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(
            sessions.get_session(name)["announce_at"], moved - ANNOUNCE_LEAD
        )

    async def test_editor_shows_the_rendered_preview(self):
        name = self._upcoming()
        await self._post("/schedule/announcement", name=name, body="미리보기 {회차} 확인")
        response = await self.client.get(
            f"/schedule/announcement?name={quote(name)}", headers=self._headers()
        )
        body = await response.text()
        self.assertIn(f"미리보기 {name} 확인", body)

    async def test_schedule_page_links_to_each_announcement(self):
        response = await self.client.get("/schedule", headers=self._headers())
        body = await response.text()
        self.assertIn("제출 공지 기본 문구", body)
        self.assertIn("/schedule/announcement?name=", body)

    async def test_editing_requires_a_session_and_csrf_token(self):
        response = await self.client.post(
            "/schedule/announcement/template",
            data={"body": "무단 수정"},
            allow_redirects=False,
        )
        self.assertTrue(response.headers["Location"].startswith("/login"))
        self.assertEqual(sessions.get_template(), DEFAULT_SUBMISSION_ANNOUNCEMENT)

        response = await self.client.post(
            "/schedule/announcement/template",
            data={"body": "무단 수정"},
            headers=self._headers(),
            allow_redirects=False,
        )
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
