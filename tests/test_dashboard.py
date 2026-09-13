import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from dashboard import register_dashboard_routes
from database.sqlite import get_connection, initialize_database


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")))
        self.enterContext(patch.object(settings, "DASHBOARD_PASSWORD", "test-password"))
        # parse_submission_teams가 만드는 멤버 ID -> 채널 ID 형태다.
        # .env의 SUBMISSION_TEAMS(채널 -> 멤버 목록)와 방향이 반대이므로 주의한다.
        # 두 명을 배정하고 한 명만 제출시켜 미제출 집계까지 확인한다.
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", {"U11111111": "C11111111", "U22222222": "C11111111"}))
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", "6기 1회차"))
        initialize_database()
        with get_connection() as connection:
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item) VALUES ('U11111111', '6기 1회차', 'C11111111', '1.0', '좋음', '개선', '학습', '실행')""")
        app = web.Application()
        register_dashboard_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_dashboard_requires_password_and_shows_submission(self):
        response = await self.client.get("/admin")
        self.assertEqual(response.status, 401)
        token = base64.b64encode(b"admin:test-password").decode()
        response = await self.client.get("/admin", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("1 / 2", body)
        self.assertIn("미제출 1명", body)

    async def test_dashboard_allows_local_access_without_password(self):
        with patch.object(settings, "DASHBOARD_PASSWORD", ""):
            response = await self.client.get("/admin")
        self.assertEqual(response.status, 200)
        self.assertIn("1 / 2", await response.text())


if __name__ == "__main__":
    unittest.main()
