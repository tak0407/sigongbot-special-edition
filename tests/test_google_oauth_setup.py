"""refresh token 발급 CLI의 순수 함수 검증."""

import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from scripts.google_oauth_setup import (
    DEFAULT_SCOPES,
    authorization_url,
    normalize_code,
    write_token_file,
)


class AuthorizationUrlTest(unittest.TestCase):
    def test_url_requests_offline_access_and_forces_consent(self):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(authorization_url("client-123", DEFAULT_SCOPES)).query
        )
        # 이 두 개가 빠지면 refresh token이 내려오지 않는다.
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], [" ".join(DEFAULT_SCOPES)])

    def test_scopes_cover_calendar_and_meet_settings(self):
        self.assertIn("https://www.googleapis.com/auth/calendar.events", DEFAULT_SCOPES)
        self.assertIn(
            "https://www.googleapis.com/auth/meetings.space.settings", DEFAULT_SCOPES
        )


class NormalizeCodeTest(unittest.TestCase):
    def test_accepts_whole_redirect_url(self):
        pasted = "http://localhost:8765/?code=4%2F0AX4XfWh-abc&scope=https://x"
        self.assertEqual(normalize_code(pasted), "4/0AX4XfWh-abc")

    def test_accepts_bare_percent_encoded_code(self):
        self.assertEqual(normalize_code(" 4%2F0AX4XfWh-abc "), "4/0AX4XfWh-abc")

    def test_accepts_already_decoded_code(self):
        self.assertEqual(normalize_code("4/0AX4XfWh-abc"), "4/0AX4XfWh-abc")

    def test_rejects_empty_and_codeless_input(self):
        with self.assertRaises(ValueError):
            normalize_code("   ")
        with self.assertRaises(ValueError):
            normalize_code("http://localhost:8765/?error=access_denied&code=")


class TokenFileTest(unittest.TestCase):
    def test_file_is_written_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "google_oauth_token.json"
            write_token_file(path, {"refresh_token": "1//secret"})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["refresh_token"], "1//secret"
            )
            # 인증 파일은 소유자만 읽을 수 있어야 한다.
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_rewrite_replaces_previous_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            write_token_file(path, {"refresh_token": "old", "client_id": "a"})
            write_token_file(path, {"refresh_token": "new", "client_id": "a"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["refresh_token"], "new")


if __name__ == "__main__":
    unittest.main()
