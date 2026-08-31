# 시공봇 Special Edition

`Daco2020/sigongbot-mini`를 기반으로 개인 Slack 워크스페이스에서 운영하는 시공봇 파생판입니다. Ubuntu 미니 PC에서 Docker Compose로 상시 실행합니다.

## 주요 기능

- `/공유`: 회고 작성, 선택 이미지 첨부, Slack 게시, SQLite 저장
- `/내회고`: 내 회고 목록 및 상세 조회
- `/관리자`: 테스트 공지 발송, 회고 수정·삭제, 사용자 채널 초대
- 공지의 `회고 제출하기` 버튼에서 직접 작성 또는 질문형 회고 선택
- 이미지 첨부 시 Antigravity CLI로 AI 시간 리뷰를 생성해 스레드에 게시
- 문의 채널 새 글을 관리자에게 알림
- 새 공개 채널 자동 참여

## Slack App 설정

앱 설정의 정본은 `slack-app-manifest.yaml`입니다. Slack API의 **App Manifest** 화면에서 이 파일 내용을 적용합니다.

적용 후에는 다음을 확인합니다.

1. **Socket Mode**가 켜져 있는지 확인합니다.
2. `connections:write` 권한의 App-Level Token(`xapp-...`)을 발급합니다.
3. 앱을 워크스페이스에 설치하거나, 권한이 변경됐다면 다시 설치합니다.
4. Bot User OAuth Token(`xoxb-...`)을 확인합니다.
5. 테스트할 공개 채널에 `시공봇 Special Edition`을 초대합니다.
6. 토큰은 Slack 화면에서 복사해 로컬 `.env`에만 입력합니다.

Slack 이미지 첨부에는 `files:read` 권한이 필요합니다. 매니페스트는 이미지 기능을 구현하기 전부터 이 권한을 포함합니다.

> AI 회고 정리와 이미지 리뷰는 실행 환경에서 인증된 Antigravity CLI(`agy`)를 찾을 수 있어야 동작합니다. 현재 Docker 이미지에는 `agy`가 포함되어 있지 않으므로, Ubuntu 배포 전에는 컨테이너용 설치·인증 방식을 추가로 구성해야 합니다. `agy`가 없어도 일반 회고 작성과 조회는 동작하지만 AI 작업은 실패 메시지를 남깁니다.

## Ubuntu 배포

### 1. Docker 설치

Ubuntu에 Docker Engine과 Compose 플러그인을 설치합니다. 이미 `docker compose version`이 정상 출력되면 건너뜁니다.

### 2. 저장소 클론

```bash
git clone https://github.com/tak0407/sigongbot-special-edition.git
cd sigongbot-special-edition
```

### 3. 환경변수 설정

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

`ADMIN_IDS`는 쉼표 구분 또는 JSON 배열을 지원합니다.

```text
ADMIN_IDS=U12345678,U87654321
```

테스트 공지 채널과 제출 채널을 분리하려면 `.env`의 `TEST_SUBMISSION_CHANNEL`에 제출 대상 채널 ID를 지정합니다. 비워두면 공지가 게시된 채널로 제출됩니다.

토큰과 키는 `.env`에만 저장합니다. `.env`는 Git에서 제외되어 있습니다.

### 4. 실행과 확인

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Slack Socket Mode를 사용하므로 공유기 포트 포워딩이나 공개 도메인이 필요하지 않습니다. 상태 확인용 HTTP 포트는 미니 PC의 `127.0.0.1:8000`에만 연결됩니다.

```bash
curl http://127.0.0.1:8000/health
```

### 5. 업데이트와 운영

```bash
git pull --ff-only
docker compose up -d --build
```

```bash
docker compose restart
docker compose down
```

컨테이너는 `restart: unless-stopped` 정책으로 Ubuntu 재부팅 뒤 자동 복구됩니다. 로그는 파일당 10MB, 최대 3개로 순환합니다. SQLite 데이터와 제출 실패 시 생기는 회고 임시 데이터는 Docker 볼륨에 보존됩니다.

## 로컬 개발

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

`ENV=dev`에서는 루트의 `.env`를 읽습니다.
