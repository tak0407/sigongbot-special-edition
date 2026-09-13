import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import settings
from constants import SIXTH_FIRST_SESSION_START
from database.retrospective import create_retrospective
from database.sqlite import initialize_database
from slack.events.command_retrospective import build_retrospective_view
from slack.events.test_announcement import (
    SIXTH_FIRST_SESSION_NAME,
    post_sixth_first_announcement,
    post_sixth_first_reminder,
)
from utils import get_current_session_info


class SixthSessionAnnouncementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(
            patch.object(settings, "DATABASE_PATH", str(Path(self.temporary.name) / "test.db"))
        )
        self.enterContext(
            patch.object(
                settings,
                "SUBMISSION_DESTINATIONS",
                {"U11111111": "C11111111", "U22222222": "C22222222"},
            )
        )
        self.enterContext(patch.object(settings, "ANNOUNCEMENT_CHANNEL", "C99999999"))
        initialize_database()
        self.client = SimpleNamespace(chat_postMessage=AsyncMock())

    async def test_announcement_posts_once_in_announcement_channel(self):
        await post_sixth_first_announcement(self.client)
        await post_sixth_first_announcement(self.client)
        self.client.chat_postMessage.assert_awaited_once()
        message = self.client.chat_postMessage.await_args.kwargs
        self.assertEqual(message["channel"], "C99999999")
        self.assertIn("<!here>", message["blocks"][0]["text"]["text"])

    async def test_reminder_skips_completed_team_channel(self):
        await create_retrospective(
            user_id="U11111111",
            session_name=SIXTH_FIRST_SESSION_NAME,
            slack_channel="C11111111",
            slack_ts="1",
            good_points="good",
            improvements="improve",
            learnings="learn",
            action_item="act",
        )
        await post_sixth_first_reminder(self.client)
        self.client.chat_postMessage.assert_awaited_once()
        self.assertEqual(self.client.chat_postMessage.await_args.kwargs["channel"], "C99999999")


class SixthSessionScheduleTest(unittest.TestCase):
    def test_sixth_session_opens_on_friday_evening(self):
        with patch.object(settings, "SESSION_NAME_OVERRIDE", ""):
            before = SIXTH_FIRST_SESSION_START - datetime.timedelta(seconds=1)
            active = get_current_session_info(SIXTH_FIRST_SESSION_START)
            self.assertFalse(get_current_session_info(before)[3])
            self.assertEqual(active[1], SIXTH_FIRST_SESSION_NAME)
            self.assertTrue(active[3])
            self.assertEqual(SIXTH_FIRST_SESSION_START.weekday(), 4)
            self.assertEqual(SIXTH_FIRST_SESSION_START.tzinfo, ZoneInfo("Asia/Seoul"))

    def test_image_upload_has_no_analysis_type_selector(self):
        view = build_retrospective_view(
            channel_id="C11111111", session_name=SIXTH_FIRST_SESSION_NAME, test_mode=True
        )
        blocks = {block.get("block_id"): block for block in view["blocks"]}
        self.assertNotIn("calendar_type", blocks)
        self.assertEqual(
            blocks["calendar_image"]["hint"]["text"], "이미지는 회고와 함께 게시됩니다."
        )


if __name__ == "__main__":
    unittest.main()
