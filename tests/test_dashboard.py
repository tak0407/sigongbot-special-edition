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
            # 지난 회차 제출 1건. created_at은 SQLite가 UTC로 기록하는 형식 그대로 넣어
            # KST 변환과 회차별 추이를 함께 검증한다.
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item, created_at) VALUES ('U33333333', '5기 12회차', 'C22222222', '2.0', '좋음', '개선', '학습', '실행', '2026-09-12 20:30:00')""")
            connection.execute("""INSERT INTO ai_review_jobs (user_id, slack_channel, slack_ts, file_id, retrospective_text, status, attempts, last_error, updated_at) VALUES ('U11111111', 'C11111111', '1.0', 'F11111111', '회고 본문', 'failed', 3, 'agy 실행 파일을 찾을 수 없습니다.', '2026-09-12 20:40:00')""")
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


    async def _body(self) -> str:
        token = base64.b64encode(b"admin:test-password").decode()
        response = await self.client.get("/admin", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status, 200)
        return await response.text()

    async def test_dashboard_lists_missing_members_per_team(self):
        body = await self._body()
        # 미제출자는 Slack에 붙여 넣을 수 있는 멘션 형태로 나온다 (HTML 이스케이프된 상태).
        self.assertIn("&lt;@U22222222&gt;", body)
        self.assertNotIn("&lt;@U11111111&gt;", body)
        self.assertIn("C11111111", body)

    async def test_dashboard_shows_submission_time_in_kst(self):
        body = await self._body()
        # 2026-09-12 20:30 UTC == 2026-09-13 05:30 KST
        self.assertIn("2026-09-13 05:30", body)
        self.assertNotIn("2026-09-12 20:30", body)
        self.assertIn("제출 시각 (KST)", body)

    async def test_dashboard_shows_failed_ai_job_reason(self):
        body = await self._body()
        self.assertIn("agy 실행 파일을 찾을 수 없습니다.", body)
        self.assertIn("3회", body)

    async def test_dashboard_shows_submission_trend_per_session(self):
        body = await self._body()
        trend = body.split("회차별 제출 추이", 1)[1].split("최근 제출", 1)[0]
        self.assertIn("5기 12회차", trend)
        self.assertIn("6기 1회차", trend)

    async def test_dashboard_auto_refreshes(self):
        body = await self._body()
        self.assertIn('http-equiv="refresh"', body)


if __name__ == "__main__":
    unittest.main()
