"""자유 에세이의 관리자 웹 표시와 회차별 낱말 집계 회귀 테스트."""

import tempfile
import unittest
from datetime import timedelta
from html import escape
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from dashboard import auth, overview, retrospectives
from dashboard import directory as slack_directory
from database.sqlite import get_connection, initialize_database


class EssayDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(
            patch.object(settings, "DATABASE_PATH", str(Path(temporary.name) / "test.db"))
        )
        self.enterContext(
            patch.object(settings, "DASHBOARD_SESSION_SECRET", "essay-dashboard-test-secret-at-least-32-characters")
        )
        self.enterContext(
            patch.object(
                settings,
                "SUBMISSION_DESTINATIONS",
                {"U11111111": "C11111111", "U22222222": "C11111111", "U33333333": "C11111111"},
            )
        )
        self.enterContext(patch.object(settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", ["U99999999"]))
        self.enterContext(
            patch.object(overview, "get_current_session_info", return_value=(0, "6기 1회차", timedelta(), False))
        )
        self.enterContext(patch.object(overview, "_word_session", return_value="6기 1회차"))
        initialize_database()
        auth.create_admin("admin", "test-password-long")
        token = auth._create_session(1)
        self.headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}"}
        app = web.Application()
        app[slack_directory.SLACK_CLIENT] = None
        app.router.add_get("/retrospectives", retrospectives.handle_list)
        app.router.add_get("/retrospectives/{retrospective_id}", retrospectives.handle_detail)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _insert(self, *, user="U11111111", session="6기 1회차", title=None, body=None, test=False, good=""):
        with get_connection() as connection:
            if body is None:
                # 새 컬럼을 지정하지 않은 기존 직접·질문형 기록은 structured다.
                cursor = connection.execute(
                    "INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts,"
                    " good_points, improvements, learnings, action_item, is_test_submission)"
                    " VALUES (?, ?, 'C11111111', '1.0', ?, '개선 본문', '배운 본문', '실행 본문', ?)",
                    (user, session, good, int(test)),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO retrospectives (user_id, session_name, slack_channel, slack_ts,"
                    " good_points, improvements, learnings, action_item, is_test_submission,"
                    " content_format, essay_title, essay_body)"
                    " VALUES (?, ?, 'C11111111', '1.0', '', '', '', '', ?, 'essay', ?, ?)",
                    (user, session, int(test), title, body),
                )
            return cursor.lastrowid

    async def _get(self, path):
        response = await self.client.get(path, headers=self.headers)
        self.assertEqual(response.status, 200)
        return await response.text()

    async def test_list_previews_essay_title_and_body_as_plain_text(self):
        self._insert(title="<script>제목</script> & 마음", body="본문 미리보기 <b>사실</b>\n둘째 줄 " + "회고 내용 " * 30)

        html = await self._get("/retrospectives")

        self.assertIn("회고 미리보기", html)
        self.assertIn("&lt;script&gt;제목&lt;/script&gt; &amp; 마음", html)
        self.assertIn("본문 미리보기 &lt;b&gt;사실&lt;/b&gt;", html)
        self.assertNotIn("<script>제목</script>", html)
        self.assertNotIn("<b>사실</b>", html)
        self.assertIn("1건", html)

    async def test_list_previews_body_when_essay_has_no_title(self):
        self._insert(body="제목 없는 에세이의 첫 문장입니다. " + "회고 내용 " * 30)

        html = await self._get("/retrospectives?session=6기+1회차")

        self.assertIn("제목 없는 에세이의 첫 문장입니다.", html)
        self.assertIn("1건", html)

    async def test_detail_escapes_essay_and_preserves_the_full_multiline_body(self):
        title = '<img src=x onerror="alert(1)"> 이번 경험'
        body = "첫 줄 <script>실행금지</script> & <@U22222222>\n둘째 줄\n\n마지막 줄 " + "회고" * 100
        retrospective_id = self._insert(title=title, body=body)

        html = await self._get(f"/retrospectives/{retrospective_id}")

        self.assertIn(f'<div class="text">{escape(title)}</div>', html)
        self.assertIn(f'<div class="text">{escape(body)}</div>', html)
        self.assertIn("white-space:pre-wrap", html)
        self.assertIn("자유 에세이", html)
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertNotIn("<script>실행금지</script>", html)
        self.assertNotIn("잘했고 좋았던 점", html)

    async def test_legacy_structured_records_keep_the_four_fields(self):
        retrospective_id = self._insert(good="기존 회고 <b>내용</b>")

        html = await self._get(f"/retrospectives/{retrospective_id}")
        listing = await self._get("/retrospectives")

        for label in ("잘했고 좋았던 점", "아쉽고 개선하고 싶은 점", "새롭게 배운 점", "해볼만한 액션 아이템"):
            self.assertIn(label, html)
        self.assertIn("기존 회고 &lt;b&gt;내용&lt;/b&gt;", html)
        self.assertIn("기존 회고 &lt;b&gt;내용&lt;/b&gt;", listing)
        self.assertNotIn("자유 에세이", html)

    def test_essay_title_and_body_join_shared_words_without_changing_counts_or_privacy(self):
        self._insert(title="독서 계획", body="면접 혼자만의고민 혼자만의고민 " + "나의 경험 " * 30)
        self._insert(user="U22222222", good="독서 면접 테스트비밀")
        self._insert(user="U33333333", body="테스트비밀 " * 30, test=True)
        self._insert(user="U99999999", body="별도계정 " * 30)
        self._insert(user="U44444444", session="5기 12회차", body="혼자만의고민 테스트비밀 " * 20)

        data = overview._collect()

        found = {row["word"]: row for row in data["words"]}
        self.assertIn("독서", found)
        self.assertIn("면접", found)
        self.assertEqual(found["독서"]["writers"], 2)
        self.assertEqual(found["면접"]["writers"], 2)
        self.assertNotIn("혼자만의고민", found)
        self.assertNotIn("테스트비밀", found)
        self.assertNotIn("별도계정", found)
        self.assertEqual(data["word_sources"], 3)
        self.assertEqual(data["submitted"], 2)
        self.assertEqual(data["expected"], 3)
        self.assertEqual(data["missing"], 1)
        self.assertEqual(data["separate"], ["U99999999"])
        trend = next(row for row in data["trend"] if row["session_name"] == "6기 1회차")
        self.assertEqual(trend["submitters"], 2)
        self.assertEqual(trend["separate_submitters"], 1)


if __name__ == "__main__":
    unittest.main()
