import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from loguru import logger
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import alerts
import background
from ai_review import worker
from config import settings


class WebhookTestCase(unittest.IsolatedAsyncioTestCase):
    """테스트용 Incoming Webhook 서버를 띄운다."""

    async def asyncSetUp(self):
        self.received = []

        async def webhook(request: web.Request) -> web.Response:
            self.received.append(await request.json())
            return web.Response(text=self.response_body, status=self.response_status)

        self.response_body = "ok"
        self.response_status = 200
        app = web.Application()
        app.router.add_post("/webhook", webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.url = str(self.client.make_url("/webhook"))
        alerts.reset_alert_throttle()
        self.addCleanup(alerts.reset_alert_throttle)

    async def wait_for_alerts(self, count: int, timeout: float = 3.0) -> None:
        """알림은 백그라운드 태스크로 나가므로 도착할 때까지 기다린다."""
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.received) < count:
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"알림 {count}건을 기다렸지만 {len(self.received)}건만 왔습니다.")
            await asyncio.sleep(0.02)
        # 예상보다 더 오지 않는지 확인할 여유를 둔다.
        await asyncio.sleep(0.05)


class AlertTest(WebhookTestCase):

    async def test_sends_payload_to_webhook(self):
        with patch.object(settings, "ALERT_WEBHOOK_URL", self.url):
            sent = await alerts.send_alert("테스트 알림")
        self.assertTrue(sent)
        self.assertEqual(self.received, [{"text": "테스트 알림"}])

    async def test_skips_when_webhook_is_not_configured(self):
        with patch.object(settings, "ALERT_WEBHOOK_URL", ""):
            sent = await alerts.send_alert("테스트 알림")
        self.assertFalse(sent)
        self.assertEqual(self.received, [])

    async def test_returns_false_on_webhook_error(self):
        self.response_body = "invalid_token"
        self.response_status = 403
        with patch.object(settings, "ALERT_WEBHOOK_URL", self.url):
            sent = await alerts.send_alert("테스트 알림")
        self.assertFalse(sent)

    async def test_never_raises_when_webhook_is_unreachable(self):
        # 오류 처리 경로에서 호출되므로 전송 실패가 예외로 번지면 안 된다.
        with patch.object(settings, "ALERT_WEBHOOK_URL", "http://127.0.0.1:1/webhook"):
            sent = await alerts.send_alert("테스트 알림")
        self.assertFalse(sent)

    def test_failure_alert_uses_raw_ids(self):
        job = {
            "id": 7,
            "attempts": 2,
            "user_id": "U11111111",
            "slack_channel": "C11111111",
            "slack_ts": "1.0",
        }
        message = alerts.build_ai_review_failure_alert(job, "agy를 찾지 못했습니다.")
        # 다른 워크스페이스에서는 멘션·채널 링크가 풀리지 않으므로 ID를 그대로 노출한다.
        self.assertIn("U11111111", message)
        self.assertIn("C11111111", message)
        self.assertNotIn("<@U11111111>", message)
        self.assertNotIn("<#C11111111>", message)
        self.assertIn("agy를 찾지 못했습니다.", message)


class ThrottleTest(WebhookTestCase):
    """오류가 반복될 때 알림 채널이 잠기지 않는지 확인한다."""

    async def test_same_key_is_suppressed_within_window(self):
        with patch.object(settings, "ALERT_WEBHOOK_URL", self.url):
            first = await alerts.send_alert("같은 오류", dedup_key="boom")
            second = await alerts.send_alert("같은 오류", dedup_key="boom")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.received), 1)

    async def test_different_keys_are_sent_until_flood_limit(self):
        with patch.object(settings, "ALERT_WEBHOOK_URL", self.url):
            sent = [
                await alerts.send_alert("오류", dedup_key=f"key-{index}")
                for index in range(alerts.ALERT_FLOOD_LIMIT + 3)
            ]
        self.assertEqual(sum(sent), alerts.ALERT_FLOOD_LIMIT)
        # 마지막으로 나간 알림에 억제 안내를 붙여 조용해진 이유를 남긴다.
        self.assertIn("억제", self.received[-1]["text"])


class ErrorSinkTest(WebhookTestCase):
    """ERROR 로그가 그대로 알림이 되는지 확인한다."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        alerts.install_error_alert_sink()
        self.addCleanup(alerts.uninstall_error_alert_sink)
        self.enterContext(patch.object(settings, "ALERT_WEBHOOK_URL", self.url))

    async def test_error_log_becomes_alert(self):
        logger.error("회고 게시에 실패했습니다 - User: U11111111")
        await self.wait_for_alerts(1)
        text = self.received[0]["text"]
        self.assertIn("시공봇 오류", text)
        self.assertIn("U11111111", text)

    async def test_exception_log_includes_traceback_without_local_values(self):
        secret = "회고 본문은 알림 워크스페이스로 나가면 안 된다"
        try:
            raise RuntimeError("이미지를 내려받지 못했습니다.")
        except RuntimeError:
            logger.exception("AI 리뷰 실패 - {}", secret[:0] or "Job 1")
        await self.wait_for_alerts(1)
        text = self.received[0]["text"]
        self.assertIn("RuntimeError: 이미지를 내려받지 못했습니다.", text)
        self.assertIn("Traceback", text)
        # loguru의 diagnose 출력은 지역 변수를 노출하므로 쓰지 않는다.
        self.assertNotIn(secret, text)

    async def test_alert_can_be_disabled_per_log(self):
        logger.bind(alert=False).error("직접 알림을 보내는 자리")
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])

    async def test_warning_does_not_alert(self):
        logger.warning("경고는 알리지 않는다")
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])


class StdlibAlertTest(WebhookTestCase):
    """slack_bolt·aiohttp처럼 표준 logging을 쓰는 라이브러리 오류도 알린다."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        alerts.install_stdlib_error_alerts()
        self.addCleanup(self.remove_handlers)
        self.enterContext(patch.object(settings, "ALERT_WEBHOOK_URL", self.url))

    def remove_handlers(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, alerts._StdlibAlertHandler):
                root.removeHandler(handler)

    async def test_library_error_becomes_alert(self):
        logging.getLogger("slack_bolt.test").error("Failed to establish a connection")
        await self.wait_for_alerts(1)
        text = self.received[0]["text"]
        self.assertIn("라이브러리 오류", text)
        self.assertIn("slack_bolt.test", text)

    async def test_library_warning_does_not_alert(self):
        logging.getLogger("slack_bolt.test").warning("reconnecting")
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])


class SuperviseTest(WebhookTestCase):
    """백그라운드 작업이 죽으면 알리고 되살리는지 확인한다."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        alerts.install_error_alert_sink()
        self.addCleanup(alerts.uninstall_error_alert_sink)
        self.enterContext(patch.object(settings, "ALERT_WEBHOOK_URL", self.url))

    async def test_restarts_and_alerts_after_crash(self):
        started = asyncio.Event()
        attempts = []

        async def start():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("작업자가 죽었습니다.")
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            background.supervise("AI 리뷰 작업자", start, restart_delay=0)
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await self.wait_for_alerts(1)
        text = self.received[0]["text"]
        self.assertIn("AI 리뷰 작업자", text)
        self.assertIn("RuntimeError: 작업자가 죽었습니다.", text)
        self.assertEqual(len(attempts), 2)


class StartupAlertTest(unittest.TestCase):
    def test_reports_missing_settings(self):
        message = alerts.build_startup_alert(["ADMIN_CHANNEL이 비어 있습니다."])
        self.assertIn("⚠️", message)
        self.assertIn("ADMIN_CHANNEL이 비어 있습니다.", message)

    def test_reports_clean_start(self):
        message = alerts.build_startup_alert([])
        self.assertIn("✅", message)


class WorkerFailureAlertTest(unittest.IsolatedAsyncioTestCase):
    """워커가 최종 실패에서만 알림을 보내는지 확인한다."""

    async def run_worker_once(self, job: dict):
        claimed = [job]

        async def claim():
            if claimed:
                return claimed.pop()
            raise asyncio.CancelledError

        with (
            patch.object(worker, "claim_next_ai_review", claim),
            patch.object(worker, "process_job", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(worker, "fail_ai_review", AsyncMock()) as fail,
            patch.object(worker, "send_alert", AsyncMock()) as alert,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await worker.run_ai_review_worker(AsyncMock())
        return fail, alert

    async def test_alerts_when_no_retry_remains(self):
        job = {
            "id": 7,
            "attempts": 2,
            "user_id": "U11111111",
            "slack_channel": "C11111111",
            "slack_ts": "1.0",
        }
        fail, alert = await self.run_worker_once(job)
        fail.assert_awaited_once_with(7, "boom", retry=False)
        alert.assert_awaited_once()
        self.assertIn("boom", alert.await_args.args[0])

    async def test_does_not_alert_while_retries_remain(self):
        job = {
            "id": 8,
            "attempts": 1,
            "user_id": "U11111111",
            "slack_channel": "C11111111",
            "slack_ts": "1.0",
        }
        fail, alert = await self.run_worker_once(job)
        fail.assert_awaited_once_with(8, "boom", retry=True)
        alert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
