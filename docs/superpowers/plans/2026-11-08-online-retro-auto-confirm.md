# 온라인 회고 자동 확정 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 전원 응답한 조를 1시간 후 확정하고 공지 채널의 원본 투표 메시지를 갱신한다.

**Architecture:** 투표 완료 시각과 공지 갱신 대기를 SQLite에 저장한다. 기존 확정 기능을 재사용하는
주기 작업이 조별 확정을 실행하고 공지 메시지를 통합 렌더링한다.

**Tech Stack:** Python 3.11, SQLite, asyncio, Slack Bolt, unittest.

**Spec:** `docs/superpowers/specs/2026-11-08-online-retro-auto-confirm-design.md`

## Global Constraints

- 기존 마이그레이션 수정 금지. migration 21 추가.
- DB 기록 후 Slack 갱신. 운영 데이터·인증정보·실제 API로 테스트하지 않는다.
- 최초 범위는 구현과 검증이며, 2026-11-09 사용자가 검증 후 운영 배포를 추가 승인했다.

## Review Focus

- 재시작 및 기존 전원 응답 투표에 1시간 유예가 보존되는가.
- 여러 조의 동시 투표와 수동 확정이 서로의 공지를 덮어쓰지 않는가.
- 확정 직전 열린 투표 모달과 DM 버튼으로 마감 뒤 결과가 바뀌지 않는가.
- Google 실패와 Slack 실패가 각각 올바르게 재시도되며 새 모임을 중복 생성하지 않는가.
- 모두 불참·과거 시간·테스트 투표·수동 취소가 자동 확정되지 않는가.

### Task 1: 투표 완료 시각과 응답 마감 저장

**Files:** `database/migrations.py`, `database/online_retro_poll.py`, `tests/test_online_retro_auto_confirm.py`

**Interfaces:** `save_vote(..., now=None) -> bool`, `sync_poll_completion(poll_id, now=...) -> dict`.

- [x] 임시 DB에서 마지막 응답 시각, 수정 시 유예 유지, 미응답·불참·비팀원, 확정 뒤 차단 테스트를 작성하고 실패를 확인한다.
- [x] migration 21과 원자적인 투표 저장·완료 시각 갱신을 구현한다.
- [x] 해당 테스트와 기존 투표 테스트를 실행한다.

### Task 2: 원본 공지 메시지의 조별 상태 갱신

**Files:** `slack/events/online_retro_poll.py`, `slack/events/online_retro_confirmation.py`, `dashboard/online_retro.py`, `tests/test_online_retro_auto_confirm.py`

**Interfaces:** `build_poll_blocks(polls, now=None)`, `_update_poll_message(client, poll)`, `confirm_meeting_time(..., announce=True)`.

- [x] 카운트다운, 일부 조 확정과 다른 조 투표 유지, 참석자 비공개, 동일 메시지 갱신, 늦은 모달 제출 테스트를 작성하고 실패를 확인한다.
- [x] 공유 메시지 렌더링과 확정 공지·투표 핸들러 연계를 구현한다.
- [x] 기존 확정·투표 UI 테스트를 함께 실행한다.

### Task 3: 자동 확정 스케줄러와 장애 복구

**Files:** `slack/events/online_retro_auto_confirm.py`, `main.py`, `README.md`, `tests/test_online_retro_auto_confirm.py`

**Interfaces:** `process_auto_confirmations(client, now=None)`, `run_online_retro_auto_confirm_scheduler(client)`.

- [x] 59분59초/60분 경계, 재시작, 최신 응답 기준 동률 선택, 조별 독립 확정, Google/Slack 실패 복구, 과거 시간·전원 불참·취소·테스트 제외 테스트를 작성하고 실패를 확인한다.
- [x] 공유 잠금·재시도 간격·기존 멱등 확정을 사용해 구현하고 main에 등록한다.
- [x] 관련 테스트, 전체 unittest, Python 컴파일, Compose 설정 검증을 실행한다.
- [x] 변경 범위를 리뷰하고 결과와 운영 적용 전 필요한 사항을 보고한다.

## 검증 결과 (2026-11-09)

- 네트워크 없는 테스트 컨테이너에서 최신 원격 main과 통합한 전체 테스트: 481개 실행, 480개 통과, 1개 건너뜀.
- Python 컴파일과 Compose 설정 검증 통과.
- 별도 읽기 전용 코드 리뷰: Critical 없음. Important 1건(자동 확정의 Meet 입장 설정 경고 유실)을 보완.
- 경고 저장·관리자 화면 표시·알림 재시도 테스트 3개를 실패부터 확인하고 수정 후 통과.
- 최종 운영 반영은 cpd 및 docs/ubuntu-deployment.md 절차에 따라 백업 검증 후 수행한다.
