import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import settings
from constants import (
    DUE_DATES,
    SESSION_NAMES,
    SIXTH_FIRST_PREVIOUS_DUE,
    SIXTH_FIRST_SESSION_DUE,
    SIXTH_FIRST_SESSION_NAME,
    SIXTH_FIRST_SESSION_START,
)
from database.retrospective import create_retrospective
from database.sqlite import initialize_database
from slack.events.command_retrospective import build_retrospective_view
from slack.session_announcement import post_sixth_first_reminder
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

    def test_session_anchors_survive_appending_new_sessions(self):
        """회차를 뒤에 붙여도 6기 1회차 기준점이 밀리지 않아야 한다.

        예전에는 DUE_DATES[-1] / DUE_DATES[-2] 같은 위치 참조를 써서
        6기 2회차를 추가하자 공지 스케줄러 마감이 12월로 밀렸다.
        """
        self.assertEqual(len(DUE_DATES), len(SESSION_NAMES))
        expected_due = DUE_DATES[SESSION_NAMES.index(SIXTH_FIRST_SESSION_NAME)]
        self.assertEqual(SIXTH_FIRST_SESSION_DUE, expected_due)
        self.assertEqual(
            SIXTH_FIRST_PREVIOUS_DUE,
            DUE_DATES[SESSION_NAMES.index(SIXTH_FIRST_SESSION_NAME) - 1],
        )
        # 스케줄러 창은 1회차 마감에서 닫혀야 한다. 마지막 회차 마감이 아니다.
        self.assertLess(SIXTH_FIRST_SESSION_DUE, DUE_DATES[-1])

    def test_sixth_cohort_runs_weekly_through_twelfth_session(self):
        with patch.object(settings, "SESSION_NAME_OVERRIDE", ""):
            for number in range(1, 13):
                self.assertIn(f"6기 {number}회차", SESSION_NAMES)
            first = DUE_DATES[SESSION_NAMES.index("6기 1회차")]
            twelfth = DUE_DATES[SESSION_NAMES.index("6기 12회차")]
            self.assertEqual(twelfth - first, datetime.timedelta(weeks=11))
            # 1회차 마감 직후에는 2회차가 열려 있어야 한다.
            just_after = first + datetime.timedelta(minutes=1)
            self.assertEqual(get_current_session_info(just_after)[1], "6기 2회차")
            self.assertTrue(get_current_session_info(just_after)[3])

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
