import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dashboard
from config import settings
from dashboard import register_dashboard_routes
from database.sqlite import get_connection, initialize_database


class FakeSlackClient:
    """이름/링크 조회에 쓰는 Slack 호출만 흉내 낸다."""

    def __init__(self, *, users_error: Exception | None = None):
        self.users_error = users_error
        self.conversations_calls = 0

    async def auth_test(self):
        return {"url": "https://example.slack.com/"}

    async def users_list(self, limit, cursor):
        if self.users_error:
            raise self.users_error
        return {
            "members": [
                {"id": "U11111111", "profile": {"display_name": "제출한사람"}},
                {"id": "U22222222", "profile": {"display_name": "", "real_name": "안낸사람"}},
            ],
            "response_metadata": {"next_cursor": ""},
        }

    async def conversations_info(self, channel):
        self.conversations_calls += 1
        return {"channel": {"name": f"team-{channel[-4:]}"}}


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
        dashboard._directory_cache["value"] = None
        dashboard._directory_cache["expires_at"] = None
        initialize_database()
        with get_connection() as connection:
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item) VALUES ('U11111111', '6기 1회차', 'C11111111', '1.0', '좋음', '개선', '학습', '실행')""")
            # 지난 회차 제출 1건. created_at은 SQLite가 UTC로 기록하는 형식 그대로 넣어
            # KST 변환과 회차별 추이를 함께 검증한다.
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item, created_at) VALUES ('U33333333', '5기 12회차', 'C22222222', '2.0', '좋음', '개선', '학습', '실행', '2026-09-12 20:30:00')""")
            connection.execute("""INSERT INTO ai_review_jobs (user_id, slack_channel, slack_ts, file_id, retrospective_text, status, attempts, last_error, updated_at) VALUES ('U11111111', 'C11111111', '1.0', 'F11111111', '회고 본문', 'failed', 3, 'agy 실행 파일을 찾을 수 없습니다.', '2026-09-12 20:40:00')""")
        self.client = await self._serve()

    async def _serve(self, slack_client=None) -> TestClient:
        app = web.Application()
        register_dashboard_routes(app, slack_client)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

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


    async def _body(self, client: TestClient | None = None) -> str:
        token = base64.b64encode(b"admin:test-password").decode()
        target = client or self.client
        response = await target.get("/admin", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status, 200)
        return await response.text()

    async def test_dashboard_shows_slack_names_and_links(self):
        slack = FakeSlackClient()
        body = await self._body(await self._serve(slack))
        # 사용자 ID 대신 표시 이름이, display_name이 비면 real_name이 나온다.
        self.assertIn("제출한사람", body)
        self.assertIn("안낸사람", body)
        # 채널은 이름과 함께 Slack 딥링크가 걸린다.
        self.assertIn("#team-1111", body)
        self.assertIn("https://example.slack.com/archives/C11111111", body)
        # 제출 시각은 해당 메시지로 바로 가는 링크가 된다 (slack_ts 1.0 -> p10).
        self.assertIn("https://example.slack.com/archives/C11111111/p10", body)
        # 붙여 넣기용 멘션 문자열은 그대로 남는다.
        self.assertIn("&lt;@U22222222&gt;", body)

    async def test_dashboard_falls_back_to_ids_without_users_scope(self):
        slack = FakeSlackClient(users_error=RuntimeError("missing_scope"))
        body = await self._body(await self._serve(slack))
        # users:read가 없어도 페이지는 뜨고 사용자만 ID로 표시된다.
        self.assertIn("U11111111", body)
        self.assertNotIn("제출한사람", body)
        # 채널 이름과 링크는 영향을 받지 않는다.
        self.assertIn("#team-1111", body)

    async def test_directory_is_cached_across_requests(self):
        slack = FakeSlackClient()
        client = await self._serve(slack)
        await self._body(client)
        first = slack.conversations_calls
        await self._body(client)
        # 채널 이름은 캐시되어 두 번째 요청에서 다시 조회하지 않는다.
        self.assertEqual(slack.conversations_calls, first)

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
