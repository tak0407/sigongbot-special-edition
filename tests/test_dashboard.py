import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from dashboard import register_dashboard_routes
from dashboard import auth
from dashboard import directory as slack_directory
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


class AdminWebTestCase(unittest.IsolatedAsyncioTestCase):
    """모든 관리자 웹 테스트가 함께 쓰는 준비 코드."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db")))
        self.enterContext(patch.object(settings, "DASHBOARD_PASSWORD", ""))
        self.enterContext(patch.object(settings, "DASHBOARD_SESSION_SECRET", "test-session-secret-at-least-32-characters"))
        # parse_submission_teams가 만드는 멤버 ID -> 채널 ID 형태다.
        # .env의 SUBMISSION_TEAMS(채널 -> 멤버 목록)와 방향이 반대이므로 주의한다.
        # 두 명을 배정하고 한 명만 제출시켜 미제출 집계까지 확인한다.
        self.enterContext(patch.object(settings, "SUBMISSION_DESTINATIONS", {"U11111111": "C11111111", "U22222222": "C11111111"}))
        self.enterContext(patch.object(settings, "SESSION_NAME_OVERRIDE", "6기 1회차"))
        slack_directory.reset_cache()
        initialize_database()
        auth.create_admin("admin", "test-password-long")
        self.session_token = auth._create_session(1)
        with get_connection() as connection:
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item) VALUES ('U11111111', '6기 1회차', 'C11111111', '1.0', '좋음', '개선', '학습', '실행')""")
            # 지난 회차 제출 1건. created_at은 SQLite가 UTC로 기록하는 형식 그대로 넣어
            # KST 변환과 회차별 추이를 함께 검증한다.
            connection.execute("""INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item, created_at) VALUES ('U33333333', '5기 12회차', 'C22222222', '2.0', '좋음', '개선', '학습', '실행', '2026-09-12 20:30:00')""")
            connection.execute("""INSERT INTO ai_review_jobs (user_id, slack_channel, slack_ts, file_id, retrospective_text, status, attempts, last_error, updated_at) VALUES ('U11111111', 'C11111111', '1.0', 'F11111111', '회고 본문', 'failed', 3, 'agy 실행 파일을 찾을 수 없습니다.', '2026-09-12 20:40:00')""")
            connection.execute("""INSERT INTO ai_review_jobs (user_id, slack_channel, slack_ts, file_id, retrospective_text, status, attempts, updated_at) VALUES ('U22222222', 'C11111111', '3.0', 'F22222222', '다른 회고', 'completed', 1, '2026-09-12 20:45:00')""")
            # 5개 질문 중 2개만 답한 진행 중 플로우.
            connection.execute(
                """INSERT INTO guided_reflections (flow_id, user_id, slack_channel, session_name, questions_json, answers_json, current_index, updated_at) VALUES (?, 'U22222222', 'C11111111', '6기 1회차', ?, ?, 2, '2026-09-12 20:00:00')""",
                (
                    "flow-abc",
                    json.dumps([{"question": f"질문{i}"} for i in range(5)], ensure_ascii=False),
                    json.dumps([{"answer": "첫 답변"}, {"answer": "둘째 답변"}], ensure_ascii=False),
                ),
            )
        self.client = await self._serve()

    async def _serve(self, slack_client=None) -> TestClient:
        app = web.Application()
        register_dashboard_routes(app, slack_client)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def _body(self, client: TestClient | None = None) -> str:
        target = client or self.client
        response = await target.get("/", headers=self._auth_headers())
        self.assertEqual(response.status, 200)
        return await response.text()

    async def _get(self, path: str, client: TestClient | None = None):
        target = client or self.client
        return await target.get(path, headers=self._auth_headers())

    def _auth_headers(self) -> dict[str, str]:
        return {"Cookie": f"{auth.SESSION_COOKIE}={self.session_token}"}

    def _csrf(self) -> str:
        return auth._csrf_token(self.session_token)


class DashboardTest(AdminWebTestCase):
    async def test_dashboard_requires_session_and_shows_submission(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertTrue(response.headers["Location"].startswith("/login"))
        response = await self.client.get("/", headers=self._auth_headers())
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("1 / 2", body)
        self.assertIn("미제출 1명", body)

    async def test_dashboard_never_allows_access_without_session(self):
        with patch.object(settings, "DASHBOARD_PASSWORD", ""):
            response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)


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

    async def test_dashboard_counts_retrospectives_missing_post_record(self):
        """Slack에는 올라갔지만 게시 기록이 끝나지 않은 회고를 눈에 띄게 센다."""
        body = await self._body()
        self.assertIn("게시 기록 미확인", body)
        with get_connection() as connection:
            connection.execute(
                """INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts, good_points, improvements, learnings, action_item, slack_post_status) VALUES ('U22222222', '6기 1회차', 'C11111111', '', '좋음', '개선', '학습', '실행', 'pending')"""
            )
        body = await self._body()
        self.assertIn('<div class="card alert">게시 기록 미확인<div class="number">1건</div>', body)

    async def test_dashboard_auto_refreshes(self):
        body = await self._body()
        self.assertIn('http-equiv="refresh"', body)



class AdminTabsTest(AdminWebTestCase):
    async def test_every_tab_requires_password(self):
        for path in (
            "/",
            "/retrospectives",
            "/ai-jobs",
            "/guided",
            "/schedule",
        ):
            with self.subTest(path=path):
                response = await self.client.get(path, allow_redirects=False)
                self.assertEqual(response.status, 302)

    async def test_pages_link_to_root_paths_only(self):
        for path in ("/", "/retrospectives", "/ai-jobs", "/guided", "/schedule"):
            with self.subTest(path=path):
                body = await (await self._get(path)).text()
                self.assertNotIn("/admin", body)
        self.assertNotIn("/admin", await (await self.client.get("/login")).text())

    async def test_legacy_admin_paths_redirect_to_root(self):
        for legacy, expected in (
            ("/admin", "/"),
            ("/admin/retrospectives", "/retrospectives"),
            ("/admin/ai-jobs?status=failed", "/ai-jobs?status=failed"),
            ("/admin/login", "/login"),
        ):
            with self.subTest(legacy=legacy):
                response = await self.client.get(
                    legacy, headers=self._auth_headers(), allow_redirects=False
                )
                self.assertEqual(response.status, 302)
                self.assertEqual(response.headers["Location"], expected)

    async def test_tabs_share_navigation(self):
        response = await self._get("/schedule")
        body = await response.text()
        for label in ("대시보드", "회고 열람", "AI 처리 큐", "진행 중 회고", "회차 일정"):
            self.assertIn(label, body)

    async def test_retrospective_list_and_detail(self):
        body = await (await self._get("/retrospectives")).text()
        self.assertIn("6기 1회차", body)
        self.assertIn("5기 12회차", body)

        # 회차 필터를 걸면 해당 회차만 남는다.
        filtered = await (await self._get("/retrospectives?session=5기 12회차")).text()
        table = filtered.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertIn("5기 12회차", table)
        self.assertNotIn("6기 1회차", table)

        detail = await (await self._get("/retrospectives/1")).text()
        # 상세에는 본문 네 항목이 모두 나온다.
        for text in ("좋음", "개선", "학습", "실행"):
            self.assertIn(text, detail)

    async def test_retrospective_detail_404_for_unknown_id(self):
        self.assertEqual((await self._get("/retrospectives/9999")).status, 404)

    async def test_ai_job_list_filter_and_detail(self):
        body = await (await self._get("/ai-jobs")).text()
        self.assertIn("failed", body)
        self.assertIn("completed", body)

        only_failed = await (await self._get("/ai-jobs?status=failed")).text()
        table = only_failed.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertNotIn("F22222222", table)

        detail = await (await self._get("/ai-jobs/1")).text()
        self.assertIn("agy 실행 파일을 찾을 수 없습니다.", detail)
        self.assertIn("다시 시도", detail)

    async def test_retry_requeues_only_failed_jobs(self):
        response = await self.client.post(
            "/ai-jobs/1/retry",
            headers=self._auth_headers(),
            data={"csrf_token": self._csrf()},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        with get_connection() as connection:
            status = connection.execute(
                "SELECT status FROM ai_review_jobs WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual(status, "pending")

        # 완료된 작업은 다시 시도할 수 없다.
        conflict = await self.client.post(
            "/ai-jobs/2/retry",
            headers=self._auth_headers(),
            data={"csrf_token": self._csrf()},
            allow_redirects=False,
        )
        self.assertEqual(conflict.status, 409)

    async def test_guided_tab_shows_progress_and_stall(self):
        body = await (await self._get("/guided")).text()
        self.assertIn("3 / 5", body)      # current_index 2 -> 3번째 질문에서 멈춤
        self.assertIn("둘째 답변", body)
        self.assertIn("정체", body)        # 24시간 이상 갱신 없음

    async def test_schedule_tab_lists_sessions_and_remaining(self):
        body = await (await self._get("/schedule")).text()
        self.assertIn("6기 12회차", body)
        self.assertIn("남은 회차", body)

    async def test_schedule_tab_can_show_every_cohort(self):
        current = await (await self._get("/schedule")).text()
        self.assertNotIn("5기 12회차", current)
        every = await (await self._get("/schedule?all=1")).text()
        self.assertIn("5기 12회차", every)


class AdminAuthenticationTest(AdminWebTestCase):
    async def _login_form(self):
        response = await self.client.get("/login")
        body = await response.text()
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        cookie = response.cookies[auth.LOGIN_CSRF_COOKIE]
        return csrf, cookie.value, response

    async def test_login_cookie_is_hardened_and_logout_revokes_session(self):
        csrf, nonce, _ = await self._login_form()
        response = await self.client.post(
            "/login",
            data={"username": "admin", "password": "test-password-long", "csrf_token": csrf},
            headers={"Cookie": f"{auth.LOGIN_CSRF_COOKIE}={nonce}"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        set_cookie = response.headers.getall("Set-Cookie")
        session_header = next(value for value in set_cookie if value.startswith(auth.SESSION_COOKIE + "="))
        for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, session_header)
        token = response.cookies[auth.SESSION_COOKIE].value

        rejected = await self.client.post(
            "/logout",
            data={"csrf_token": "wrong"},
            headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"},
            allow_redirects=False,
        )
        self.assertEqual(rejected.status, 403)
        logged_out = await self.client.post(
            "/logout",
            data={"csrf_token": auth._csrf_token(token)},
            headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"},
            allow_redirects=False,
        )
        self.assertEqual(logged_out.status, 302)
        after = await self.client.get(
            "/", headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"}, allow_redirects=False
        )
        self.assertEqual(after.status, 302)

    async def test_login_returns_to_root_paths_only(self):
        # 외부 주소나 예전 /admin 경로가 next로 들어와도 루트 기준으로만 되돌린다.
        self.assertEqual(auth.safe_internal_path("//evil.example.com"), "/")
        self.assertEqual(auth.safe_internal_path("https://evil.example.com"), "/")
        self.assertEqual(auth.safe_internal_path("/admin"), "/")
        self.assertEqual(auth.safe_internal_path("/admin/schedule"), "/schedule")
        self.assertEqual(auth.safe_internal_path("/ai-jobs?status=failed"), "/ai-jobs?status=failed")

    async def test_legacy_redirect_cannot_leave_the_site(self):
        response = await self.client.get(
            "/admin//evil.example.com", headers=self._auth_headers(), allow_redirects=False
        )
        self.assertEqual(response.headers["Location"], "/")

    async def test_protected_page_redirects_to_root_login_with_next(self):
        response = await self.client.get("/schedule", allow_redirects=False)
        self.assertEqual(response.headers["Location"], "/login?next=/schedule")

    async def test_login_rejects_csrf_and_limits_failures(self):
        csrf, nonce, _ = await self._login_form()
        missing = await self.client.post(
            "/login", data={"username": "admin", "password": "test-password-long"}
        )
        self.assertEqual(missing.status, 403)
        headers = {"Cookie": f"{auth.LOGIN_CSRF_COOKIE}={nonce}"}
        for _ in range(auth.MAX_LOGIN_FAILURES):
            failed = await self.client.post(
                "/login",
                data={"username": "admin", "password": "wrong-password", "csrf_token": csrf},
                headers=headers,
            )
            self.assertEqual(failed.status, 401)
        locked = await self.client.post(
            "/login",
            data={"username": "admin", "password": "test-password-long", "csrf_token": csrf},
            headers=headers,
        )
        self.assertEqual(locked.status, 429)

    async def test_state_change_requires_csrf(self):
        response = await self.client.post(
            "/ai-jobs/1/retry", headers=self._auth_headers(), allow_redirects=False
        )
        self.assertEqual(response.status, 403)

    async def test_password_is_stored_as_scrypt_hash(self):
        with get_connection() as connection:
            stored = connection.execute(
                "SELECT password_hash FROM admin_users WHERE username = 'admin'"
            ).fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("test-password-long", stored)

    def test_production_requires_session_secret(self):
        with patch.object(settings, "ENV", "prod"), patch.object(
            settings, "DASHBOARD_SESSION_SECRET", ""
        ):
            with self.assertRaises(RuntimeError):
                auth.validate_dashboard_security()


if __name__ == "__main__":
    unittest.main()
