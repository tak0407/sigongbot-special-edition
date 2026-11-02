# 문의 알림 개별 DM 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 문의·개선 제안이 접수되면 사용자가 지정한 두 운영자에게 각각 DM을 보낸다.

**Architecture:** 관리자 권한과 별개인 `SUPPORT_NOTIFICATION_USER_IDS` 설정으로 수신자를 관리한다. 공통 알림 함수가 수신자마다 DM을 열고 발송하며, 한 명에게 실패해도 나머지 발송을 계속한다. 개선 제안의 SQLite 저장 순서는 유지한다.

**Tech Stack:** Python 3.11, Slack SDK, unittest, Docker Compose.

**Spec:** 사용자 요청: 정현님과 사용자 본인에게 문의 알림을 각각 개인 메시지로 보내고 작업 완료 후 운영 배포한다. 두 계정은 Slack 사용자 조회로 확인했으며 실제 ID는 Git 비추적 `.env`에만 저장한다.

## Global Constraints

- 운영 DB, 인증정보, Docker 볼륨을 보존한다. 스키마는 변경하지 않는다.
- 개선 제안은 DB 저장 후 알리며, 알림 실패로 저장된 내용을 잃지 않는다.
- DM 수신자 추가로 관리자 권한을 부여하지 않는다.
- 테스트는 네트워크가 차단된 별도 컨테이너와 임시 SQLite를 사용한다.
- 기존 체크아웃에서 작업하며 운영 봇을 중복 실행하지 않는다.

## Review Focus

- 첫 수신자의 DM 열기 또는 발송 실패가 두 번째 수신자 알림을 막지 않는다.
- 중복 수신자는 한 번만 알리고, 수신자가 비어 있으면 성공으로 보고하지 않는다.
- 문의 채널 스레드 댓글·수정·봇 메시지는 새 문의로 알리지 않는다.
- 제안 저장 실패 시 DM을 보내지 않으며, 일부 DM 실패 시 저장된 제안과 안내를 유지한다.
- 긴 문의와 멘션 문법은 원문을 일반 텍스트 블록으로 안전하게 전달한다.

## Task 1: 문의·제안 DM 알림 및 운영 반영

**Files:** `config.py`, `.env.example`, `slack/support_notifications.py`, `slack/events/message.py`, `slack/events/command_suggestion.py`, `tests/test_support_notifications.py`, `tests/test_suggestions.py`, `README.md`, `docs/ubuntu-deployment.md`.

**Interfaces:** `notify_support_recipients(client, *, text: str, blocks: list[dict] | None = None) -> bool`은 모든 고유 수신자에게 발송했을 때만 `True`를 반환한다.

- [x] 기존 전체 테스트를 실행해 기준 상태를 확인한다. 354개 실행, 선택적 브라우저 테스트 1개 제외, 실패 없음.
- [x] 두 명의 개별 DM, 수신자별 실패 격리, 중복·빈 수신자, 문의 필터와 제안 저장 순서를 검증하는 테스트를 먼저 작성하고 예상한 실패를 확인한다.
- [x] 공통 발송 함수와 설정을 추가하고 기존 문의·제안 경로에 연결한다. 관련 테스트 19개 통과.
- [x] 관련 테스트와 전체 `ENV=prod python -m unittest discover -s tests -v`를 실행한다. 365개 실행, 선택적 브라우저 테스트 1개 제외, 실패 없음. 별도 읽기 전용 코드 검토에서 배포 차단 사항 없음.
- [x] 문서를 갱신하고 `.env`에 확인한 두 수신자를 설정하며 권한 `600`과 Git 제외를 확인한다. 기존 `.env` 백업 후 수신자 설정만 추가했다.
- [x] DB 백업을 검증하고 Python 컴파일·Compose 설정을 확인한다. 무결성 정상, 외래 키 위반 없음, 스키마 18 유지, 회고 1,322건 보존.
- [x] `docker compose up -d --build --wait --wait-timeout 120` 배포 성공. 컨테이너 healthy, HTTP health `OK`, 재시작 횟수 0, `unless-stopped` 유지. Slack 인증·DM 권한, 봇 프로세스의 TLS 연결, 시작 로그에 오류 없음, 실제 수신자 두 명과 관리자 권한 불변을 확인했다. 배포 코드 해시가 테스트한 소스와 일치하며 배포 후 DB 무결성과 회고 1,322건도 확인했다.

배포 후 사용자가 직접 테스트 제안을 제출해 실제 DM 수신과 정상 동작을 확인했다. 이후 사용자의 후속 요청에 따라 문의 알림 관련 변경을 커밋하고 현재 원격 브랜치로 푸시한다.

Ruling: 실제 문의 채널은 별도 지정되지 않았으므로 기존 `SUPPORT_CHANNEL`은 변경하지 않는다. `/제안`·개선 제안 버튼의 접수 알림은 문의 채널 설정과 무관하게 두 운영자에게 전달한다.
