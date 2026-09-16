"""Optional real-browser regression: DASHBOARD_BROWSER_TESTS=1, playwright chromium installed.

Uses only the existing fake Slack / temporary database fixture.
"""
import os
import unittest
from unittest.mock import patch
from urllib.parse import quote

import test_dashboard as fixtures
from database.sqlite import get_connection

PAGES = (
    "/", "/retrospectives", "/retrospectives/1", "/suggestions",
    "/suggestions/1", "/guided", "/schedule", "/attendance", "/members",
    "/ai-jobs", "/ai-jobs/1",
)


class MobileMarkupTest(fixtures.AdminWebTestCase):
    async def test_all_lists_have_keyboard_accessible_scroll_regions(self):
        for path in PAGES:
            with self.subTest(path=path):
                response = await self._get(path)
                self.assertEqual(response.status, 200)
                body = await response.text()
                self.assertIn('name="viewport"', body)
                self.assertIn('aria-label="관리자 메뉴"', body)
                if not path.startswith("/ai-jobs"):
                    self.assertIn('aria-current="page"', body)
                self.assertEqual(body.count("<thead>"), body.count('class="table-scroll"'))
                self.assertEqual(body.count("<thead>"), body.count('aria-label="목록 표" tabindex="0"'))
                self.assertIn(f'name="csrf_token" value="{self._csrf()}"', body)


@unittest.skipUnless(os.getenv("DASHBOARD_BROWSER_TESTS") == "1", "optional Playwright render checks")
class MobileBrowserTest(fixtures.AdminWebTestCase):
    async def test_mobile_and_desktop_layout_and_controls(self):
        from playwright.async_api import async_playwright

        self.enterContext(patch.object(fixtures.settings, "SUBMISSION_CHANNEL_CHOOSER_IDS", ["U33333333"]))
        with get_connection() as connection:
            connection.execute(
                """INSERT INTO retrospectives (user_id, session_name, slack_channel, good_points, slack_ts, improvements, learnings, action_item)
                   VALUES ('U33333333', '6기 1회차', 'C11111111', '별도 제출', '3.0', '', '', '')"""
            )
        # Include unbroken content and a future editable schedule.
        long_text = "긴문자열https://example.invalid/" + "x" * 500
        with get_connection() as connection:
            connection.execute("UPDATE retrospectives SET good_points = ?", (long_text,))
            connection.execute("UPDATE bot_improvement_suggestions SET content = ?", (long_text,))
        response = await self.client.post(
            "/schedule/add", headers=self._auth_headers(),
            data={"csrf_token": self._csrf(), "name": "모바일 테스트 회차", "due_at": "2099-09-20T05:00"},
        )
        self.assertEqual(response.status, 200)
        announcement = "/schedule/announcement?name=" + quote("모바일 테스트 회차")
        pages = ("/login", *PAGES, announcement)
        html = {}
        for path in pages:
            response = await (self.client.get(path) if path == "/login" else self._get(path))
            self.assertEqual(response.status, 200, path)
            html[path] = await response.text()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            page.set_default_timeout(5000)
            for width in (320, 360, 375, 390, 430, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                for path, body in html.items():
                    with self.subTest(width=width, path=path):
                        await page.set_content(body)
                        self.assertTrue(await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), f"page overflow: {width} {path}")
                        if path != "/login":
                            menu = page.locator(".menu summary")
                            if width <= 760:
                                self.assertFalse(await page.locator(".menu-links").is_visible())
                                await menu.click()
                            self.assertTrue(await page.locator(".menu-links").is_visible())
                            self.assertTrue(await page.locator('button:has-text("로그아웃")').is_visible())
                            self.assertTrue(await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            ))
                        if width <= 760:
                            sizes = await page.locator(
                                'button, select, input:not([type=hidden]), textarea'
                            ).evaluate_all("""els => els.filter(e => e.getClientRects().length)
                                .map(e => ({height:e.getBoundingClientRect().height,
                                           font:parseFloat(getComputedStyle(e).fontSize)}))""")
                            self.assertTrue(all(s["height"] >= 44 and s["font"] >= 16 for s in sizes), sizes)
                            for region in await page.locator(".table-scroll").all():
                                await region.focus()
                                await page.keyboard.press("ArrowRight")
                                await page.wait_for_timeout(100)
                                self.assertGreater(await region.evaluate("e => e.scrollLeft"), 0)
                        if width == 320 and path in ("/login", "/", "/schedule", "/retrospectives/1"):
                            artifact_dir = os.getenv("DASHBOARD_SCREENSHOTS")
                            if artifact_dir:
                                from pathlib import Path
                                Path(artifact_dir).mkdir(parents=True, exist_ok=True)
                                filename = path.strip("/").replace("/", "-") or "overview"
                                await page.screenshot(path=str(Path(artifact_dir) / (filename + ".png")), full_page=True)
                        if path == "/schedule":
                            await page.get_by_label("새 회차 이름").fill("입력 확인")
                            self.assertEqual(await page.get_by_label("새 회차 이름").input_value(), "입력 확인")
                        if path == announcement:
                            await page.get_by_label("공지 문구").fill("모바일 문구 입력")
                            self.assertEqual(await page.get_by_label("공지 문구").input_value(), "모바일 문구 입력")

            # Rotation must restore desktop navigation after closing the mobile menu.
            await page.set_viewport_size({"width": 320, "height": 900})
            await page.set_content(html["/"])
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.locator(".menu-links").wait_for(state="visible")
            self.assertTrue(await page.locator(".menu-links").is_visible())
            # Without JavaScript navigation is expanded and usable at both sizes.
            no_js = await browser.new_context(java_script_enabled=False)
            fallback = await no_js.new_page()
            for width in (320, 1280):
                await fallback.set_viewport_size({"width": width, "height": 900})
                await fallback.set_content(html["/"])
                self.assertTrue(await fallback.locator(".menu-links").is_visible())
            await no_js.close()

            # Actual navigation, filter GET and state-changing form POST with session/CSRF.
            await page.set_viewport_size({"width": 320, "height": 900})
            await page.set_extra_http_headers(self._auth_headers())
            await page.goto(str(self.client.make_url("/")))
            await page.locator(".menu summary").click()
            await page.locator(".menu-links").get_by_text("회고 열람").click()
            await page.get_by_label("회차 필터").select_option("6기 1회차")
            await page.get_by_role("button", name="필터", exact=True).click()
            self.assertIn("session=", page.url)
            await page.goto(str(self.client.make_url("/suggestions/1")))
            await page.locator("#status").select_option("completed")
            async with page.expect_navigation():
                await page.get_by_role("button", name="변경", exact=True).click()
            with get_connection() as connection:
                status = connection.execute("SELECT status FROM bot_improvement_suggestions WHERE id=1").fetchone()[0]
            self.assertEqual(status, "completed")
