"""Google Calendar/Meet 공식 REST API 연동.

운영자 Google 계정의 OAuth 2.0 refresh token으로 액세스 토큰을 갱신하고,
Calendar 이벤트에 Google Meet 회의를 붙인다. 자격증명은 `.env` 또는 Git
비추적 인증 파일에서만 읽는다.
"""
