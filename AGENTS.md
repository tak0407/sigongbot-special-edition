# AGENTS.md

이 저장소는 Slack 회고 봇 `시공봇 Special Edition`의 코드 정본이다.

## 작업 목표

- Ubuntu 미니 PC에서 Docker Compose로 24시간 운영한다.
- Slack Socket Mode를 사용하며 외부 포트와 공개 도메인을 열지 않는다.
- 배포 절차는 `docs/ubuntu-deployment.md`를 따른다.

## 필수 원칙

- 실제 `.env`, Slack 토큰, Antigravity 인증정보, SQLite DB를 Git에 추가하지 않는다.
- 운영 비밀값은 미니 PC의 `.env`에만 두고 권한을 `600`으로 설정한다.
- 운영 SQLite와 임시 데이터가 담긴 Docker 볼륨을 삭제하지 않는다.
- `docker compose down -v`는 데이터 볼륨을 삭제하므로 실행하지 않는다.
- 같은 Slack App 토큰으로 맥북과 미니 PC의 봇을 동시에 실행하지 않는다.
- Socket Mode 연결에는 `SLACK_BOT_TOKEN`과 `SLACK_APP_TOKEN`을 사용한다. HTTP Request URL을 받지 않으므로 `SLACK_SIGNING_SECRET`은 현재 구성의 필수값이 아니며, 없다는 이유만으로 새 비밀값을 요구하거나 배포를 중단하지 않는다.
- 기존 사용자 변경사항과 무관한 파일을 되돌리지 않는다.
- 코드 수정 후 Python 컴파일, Compose 설정, health check, Slack 연결 로그를 검증한다.

## 현재 주의사항

- 회고 데이터는 Supabase가 아니라 로컬 SQLite를 사용한다.
- 질문형 회고 정리와 이미지 분석은 Antigravity CLI(`agy`)를 호출한다.
- 현재 Docker 이미지에는 `agy`가 포함되어 있지 않다. AI 기능까지 운영 완료로 판단하려면 컨테이너와 동일한 실행 환경에서 `agy` 설치·인증·이미지 판독을 검증해야 한다.
- `constants.py`의 고정 회차 일정은 2026-08-25에 종료됐다. `.env`의 `SESSION_NAME_OVERRIDE`는 테스트 전용이며 실제 운영 일정의 대체물이 아니다.
- 테스트 제출 채널은 `.env`의 `TEST_SUBMISSION_CHANNEL`로만 설정하며 실제 채널 ID를 문서나 커밋에 하드코딩하지 않는다.

## 완료 기준

- `docker compose up -d --build` 성공
- 컨테이너 상태가 healthy
- 재부팅 후 컨테이너 자동 복구
- Slack 공지 버튼, 직접 작성, 질문형 회고, 제출 채널 게시 확인
- 이미지가 있으면 AI 리뷰 스레드 게시 확인
- 토큰·`.env`·DB가 Git 추적 대상이 아님을 확인
