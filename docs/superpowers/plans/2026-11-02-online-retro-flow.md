# Online Retrospective Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 입장 클릭으로 출석하고, 무작위 이끌이를 알리며, 전원 제출 시 공유 준비를 안내한다.

**Architecture:** 기존 Slack URL 버튼과 출석 저장소를 확장한다. 이끌이는 시작 시각을 포함한 모임별 DB 레코드로 보존하고, 단계 공지의 기존 중복 방지 키를 재사용한다. 제출 성공 후 확인과 정기 스케줄러를 함께 사용한다.

**Tech Stack:** Python 3.11, SQLite migrations, slack_bolt/slack_sdk, unittest, Docker.

**Spec:** `docs/superpowers/specs/2026-11-02-online-retro-flow-design.md`

## Global Constraints

- 운영 비밀값·DB·인증정보를 Git에 넣지 않는다.
- 새 스키마는 새 마이그레이션으로만 추가하고 운영 적용 전 백업을 검증한다.
- 다른 작업의 19번 마이그레이션과 충돌하지 않도록 이 기능은 20번을 사용한다.
- 현재 작업에서는 운영 배포를 실행하지 않는다.

## Review Focus

- 오래된 URL 버튼에 metadata가 없거나 동일 URL이 여러 모임에 쓰인 경우 오출석을 기록하지 않는다.
- 정각 이후 늦게 들어온 사람을 정각 출석자가 있는 추첨에 섞지 않는다.
- Slack 게시 실패나 재시작 후 이끌이가 다시 추첨되지 않는다.
- 출석자 0명·일부 미제출·Slack 게시 pending을 전원 완료로 보지 않는다.
- 공유 시작 뒤 작성 공지가 재전송되지 않고 모든 출석자의 진행 권한이 유지된다.

### Task 1: 입장과 출석 통합

**Files:** `slack/events/online_retro_meeting.py`, `slack/events/online_retro_confirmation.py`, `database/online_retro_attendance.py`, 관련 두 테스트 파일.

**Interfaces:** `handle_open_online_retro_meeting(ack, body, client)`가 출석을 기록한다. 모든 URL 버튼은 회차·팀 JSON value를 갖는다. 호환용 출석 핸들러는 유지한다.

- [x] UI 버튼·중복 클릭·다른 팀·시작 전·이전 URL payload 테스트를 먼저 작성하고 실패를 확인한다.
- [x] 별도 버튼을 제거하고 입장 클릭에서 기존 권한/시각 검증과 최초 출석 기록을 재사용한다.
- [x] 두 관련 테스트 파일을 실행해 통과를 확인한다.
- [x] 관련 변경만 커밋한다.

### Task 2: 이끌이 선택과 공지

**Files:** `database/migrations.py`, `database/online_retro_leader.py`, `slack/events/online_retro_meeting.py`, `tests/test_online_retro_progress.py`.

**Interfaces:** `choose_meeting_leader(session_name, team_channel, starts_at, now) -> str | None`; `post_meeting_leader(client, meeting, now=None)`.

- [x] 시작 전·정각 후보·출석자 0명·늦은 첫 입장·반복/동시 추첨 테스트를 쓰고 실패를 확인한다.
- [x] 20번 마이그레이션과 트랜잭션으로 추첨 결과를 보존하는 저장소를 구현한다.
- [x] 시작 시각 스케줄러에서 추첨·공지하고 출석자 누구나 진행하는 권한을 유지한다.
- [x] 해당 테스트와 마이그레이션 회귀 테스트를 실행해 통과를 확인하고 커밋한다.

### Task 3: 전원 제출 안내

**Files:** `database/retrospective.py`, `slack/events/online_retro_meeting.py`, `slack/events/view_retrospective_submit.py`, `tests/test_online_retro_progress.py`.

**Interfaces:** `get_submitted_user_ids(..., posted_only=False)`; `notify_all_writing_complete(client, meeting, now=None) -> bool`; `notify_submission_progress(client, session_name, team_channel)`.

- [x] 전원 제출·일부/0명·pending/테스트 제출·공유 시작·중복/실패 재시도 테스트를 쓰고 실패를 확인한다.
- [x] 실제 게시 성공 기록만 세고 완료 메시지와 공유 버튼을 한 번 보낸다. 조기 완료 후 시간 종료는 생략한다.
- [x] 제출 성공 경로에서 실패가 제출 성공을 뒤집지 않는 확인 hook을 연결하고 스케줄러에서도 확인한다.
- [x] 전체 unittest, Python 컴파일, Compose 설정, 운영 health/log 읽기 검증을 수행한다.
- [x] README와 배포 기능 점검 안내를 최신 동작에 맞추고 커밋한다.

### Task 4: 변경 검토와 인계

- [x] 새 검토자에게 변경 전체를 읽기 전용 검토하게 하고 중요한 문제를 재현 테스트와 함께 수정한다.
- [x] 검증 결과와 미배포 상태를 보고하고 분리한 작업 공간을 보존한다.
