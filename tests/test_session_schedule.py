"""회차 일정을 DB에서 읽고 관리자 웹에서 편집하는 흐름."""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from constants import DUE_DATES, SESSION_NAMES
from dashboard import auth, register_dashboard_routes
from database import sessions
from database.sqlite import get_connection, initialize_database
from utils import get_current_session_info

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


if __name__ == "__main__":
    unittest.main()
