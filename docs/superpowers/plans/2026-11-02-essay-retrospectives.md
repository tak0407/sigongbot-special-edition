# 자유 에세이 회고 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 자유 에세이 제출을 추가하고 기존 항목별 입력을 1,000자로 늘린다.

**Architecture:** 기존 회고 테이블에 형식·제목·본문을 추가하고 공통 본문 렌더러로 Slack 게시와 열람·수정을 연결한다. DB와 관리자 웹은 명시된 인터페이스로 분리해 병렬 작업하고 Slack 흐름은 주 에이전트가 구현한다.

**Tech Stack:** Python 3.11, SQLite migrations, Slack Bolt/SDK, aiohttp, unittest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-02-essay-retrospectives-design.md`.

## Global Constraints

- 기존 마이그레이션은 수정하지 않는다. 운영 DB와 비밀값·볼륨을 보존한다.
- 에세이는 100~3,000자, 제목은 선택·최대 150자, 기존 항목은 최대 1,000자다.
- DB 저장이 Slack 게시보다 먼저다. 기존 중복 방지·팀 배정·테스트 제출 규칙을 유지한다.
- 테스트는 실제 Slack·운영 DB에 접속하지 않는다.

## Review Focus

- 기존 기록 마이그레이션·미게시 재시도·형식 전환 시 데이터 보존과 이전 본문 제거.
- 최소 길이·공백·최대 길이와 제목 검증을 UI뿐 아니라 서버에서도 적용.
- Slack 멘션 문법과 HTML을 포함한 원문, 이미지 첨부와 실패 시 임시 저장.
- 기존 포맷·질문형·관리자 수정의 길이 한도와 출력 일관성.
- 두 형식의 제출 집계·상세·수정·대시보드·워드클라우드 연결.

### Task 1: 저장 계층

**Files:** `retrospective_content.py`, `database/migrations.py`, `database/retrospective.py`, `tests/test_essay_database.py`.
**Interfaces:** `retrospective_content.py`는 `STRUCTURED_TEXT_MAX_LENGTH=1000`, `ESSAY_MIN_LENGTH=100`, `ESSAY_MAX_LENGTH=3000`, `ESSAY_TITLE_MAX_LENGTH=150`, `validate_essay(title, body) -> tuple[str | None, str]`를 제공한다. 기존 저장 API는 호환되는 선택 키워드 `content_format='structured'`, `essay_title=None`, `essay_body=None`를 받는다.

- [x] 회귀 테스트를 먼저 작성하고 실패를 확인한다.
- [x] 새 마이그레이션과 저장·재시도·수정 검증을 구현하고 관련 테스트를 통과시킨다.

### Task 2: Slack 흐름과 표시

**Files:** `slack/events/command_retrospective.py`, `slack/events/test_announcement.py`, `slack/events/view_retrospective_submit.py`, `slack/events/action_view_retrospective_detail.py`, `slack/events/view_admin_menu.py`, 새 `slack/retrospective_blocks.py`, `ai_review/formatter.py`, 관련 테스트.
**Interfaces:** `build_retrospective_view(..., content_format='structured')`로 에세이 폼을 만든다. `build_retrospective_blocks(record, *, heading=None)`는 해당 형식의 내용·감정을 반환하며 첨부 이미지는 호출자가 추가한다.

- [x] 방식 선택·길이 검증·DB 우선 저장·이미지·조회·수정·실패 시 복구 테스트를 먼저 작성하고 실패를 확인한다.
- [x] 세 번째 작성 방식, 에세이 폼·제출, 공통 렌더러와 조회·수정 연결을 구현한다.
- [x] 기존 포맷·질문형과 AI 정리의 한도를 1,000자로 맞추고 관련 테스트를 실행한다.

### Task 3: 관리자 웹과 통합 검증

**Files:** `dashboard/retrospectives.py`, `dashboard/wordcloud.py`, 필요 시 `dashboard/overview.py`, `tests/test_essay_dashboard.py`, `README.md`, 배포 문서.

- [x] 대시보드 목록·상세·원문 이스케이프·워드클라우드 테스트를 먼저 작성하고 실패를 확인한다.
- [x] 에세이 형식의 표시와 집계를 연결하고 문서를 갱신한다.
- [x] 전체 unittest, Python 컴파일, Compose 설정, diff·비밀값 제외를 검증한다. 최종 421개 실행, 선택적 브라우저 테스트 1개 제외, 실패 없음.
- [x] 독립 코드 검토 후 필요한 수정과 검증을 완료한다. SQLite 날짜 형식으로 `/내회고` 목록이 실패하던 기존 문제를 회귀 테스트로 재현하고 수정했다. 이미지 열람 범위를 잘못 설명한 문구도 바로잡았다.

### 운영 반영

- [x] 사용자의 앞선 배포 요청과 진행 지시에 따라 배포 전 `scripts/backup_database.sh`를 실행했다. 백업 무결성 정상, 외래 키 위반 없음, 스키마 18, 기존 회고 1,322건을 확인했다.
- [x] `docker compose up -d --build --wait --wait-timeout 120` 배포 성공. 마이그레이션 19 적용, SQLite 무결성 정상·외래 키 위반 없음, 최종 회고 1,323건을 확인했다. 컨테이너 healthy·재시작 0·`unless-stopped`, HTTP health `OK`, Slack 인증·기존 권한과 시작 로그를 확인했다. 배포된 13개 구현 파일의 해시가 검증한 소스와 일치하고 실제 컨테이너에서 세 방식 선택 및 에세이 입력 한도의 Slack View 검증을 통과했다.

실제 Slack 사용자의 자유 에세이 제출은 자동 실행하지 않았다. 테스트는 임시 SQLite와 Slack API 경계 대역으로 수행했다.
