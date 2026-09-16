# 시공봇 Special Edition

`Daco2020/sigongbot-mini`를 기반으로 개인 Slack 워크스페이스에서 운영하는 시공봇 파생판입니다. Ubuntu 미니 PC에서 Docker Compose로 상시 실행합니다.

## 주요 기능

- `/공유`: 회고 작성, 선택 이미지 첨부, Slack 게시, SQLite 저장
- `/내회고`: 내 회고 목록 및 상세 조회
- `/제안`: 분류와 내용을 입력해 시공봇 개선 제안 제출
- `/관리자`: 테스트 공지 발송, 회고 수정·삭제, 사용자 채널 초대
- 공지의 `회고 제출하기` 버튼에서 직접 작성 또는 질문형 회고 선택
- 시간 기록 이미지를 회고와 함께 게시
- 문의 채널 새 글을 관리자에게 알림
- 새 공개 채널 자동 참여
- 온라인 회고 모임 예약 공지, 출석 체크, 관리자 출석 현황
- 마감 직전 미제출자에게 본인에게만 보이는 개인 리마인더 DM

## Slack App 설정

앱 설정의 정본은 `slack-app-manifest.yaml`입니다. Slack API의 **App Manifest** 화면에서 이 파일 내용을 적용합니다.

적용 후에는 다음을 확인합니다.

1. **Socket Mode**가 켜져 있는지 확인합니다.
2. `connections:write` 권한의 App-Level Token(`xapp-...`)을 발급합니다.
3. 앱을 워크스페이스에 설치하거나, 권한이 변경됐다면 다시 설치합니다.
4. Bot User OAuth Token(`xoxb-...`)을 확인합니다.
5. 테스트할 공개 채널에 `시공봇 Special Edition`을 초대합니다.
6. 토큰은 Slack 화면에서 복사해 로컬 `.env`에만 입력합니다.

개선 제안 기능은 매니페스트의 `/제안` slash command와 Interactivity를 사용합니다.
기존 앱에 매니페스트 변경을 적용한 뒤 워크스페이스에 다시 설치해 명령을
활성화하세요. 추가 OAuth scope나 Request URL은 필요하지 않으며 Socket Mode로
명령과 모달 제출을 받습니다. 사용자가 `/제안`을 실행한 채널 ID가 제출 채널로
기록되고, 제안은 SQLite에 먼저 저장된 뒤 기존 `ADMIN_CHANNEL`로 알림이 전송됩니다.
알림이 실패해도 저장된 제안은 유지됩니다.

Slack 이미지 첨부에는 `files:read` 권한이 필요합니다. 매니페스트는 이미지 기능을 구현하기 전부터 이 권한을 포함합니다.

> 질문형 회고 정리는 Docker 이미지에 설치된 Antigravity CLI(`agy`)와 Google 계정 로그인을 사용합니다. 별도 Gemini API 키는 필요하지 않습니다. AI 호출이 실패해도 일반 회고 작성과 조회는 동작하며, 질문형 회고는 기본 매핑으로 처리됩니다.

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

관리자 현황 화면은 `http://127.0.0.1:8000/`입니다. 항상 DB 관리자
계정 로그인이 필요하며, 비밀번호는 SQLite에 scrypt 해시로만 저장됩니다.
`DASHBOARD_SESSION_SECRET`는 32자 이상의 임의 값으로 설정하고 `.env`에만 둡니다.
외부 접근은 Cloudflare Tunnel과 Access를 사용하며 상세 절차는
[`docs/cloudflare-dashboard.md`](docs/cloudflare-dashboard.md)를 따릅니다.
관리자 웹의 `봇 개선 제안` 탭에서는 제안 목록과 상세 내용을 열람하고 상태를
`접수`, `처리 중`, `완료`로 변경할 수 있습니다. 상태 변경은 기존 로그인 세션과
CSRF 보호를 동일하게 적용합니다.

테스트 공지와 제출 채널을 분리하려면 `.env`의 `TEST_SUBMISSION_CHANNEL`에 단일 테스트 제출 채널 ID를 지정합니다.

공지에는 제출 버튼 하나만 게시합니다. `.env`의 `SUBMISSION_TEAMS`에 팀별 채널 ID와 Slack 멤버 ID 목록을 JSON으로 설정하면 직접 작성·질문형 회고·`/공유` 모두 작성자의 배정된 팀 채널로 제출됩니다. 채널 가입 여부나 버튼을 누른 채널로 팀을 추측하지 않습니다.

```dotenv
# 아래 ID는 형식 설명용 가상 값입니다. 실제 값은 미니 PC .env에만 입력합니다.
SUBMISSION_TEAMS='{"C11111111":["U11111111","U22222222"],"C22222222":["U33333333"],"C33333333":["U44444444"]}'
```

설정이 비어 있거나 미등록 인원이 제출하면 입력창에 안내하고 게시하지 않습니다. 중복 멤버 배정과 잘못된 ID 형식은 시작 시 오류로 처리합니다. `TEST_SUBMISSION_CHANNEL`은 테스트 회차에만 우선 적용되며, 팀 분배를 테스트할 때는 비워둡니다. 본문, SQLite 기록, 첨부 이미지는 같은 팀 채널을 사용합니다. 질문형 회고의 개인용 결과 확인 알림은 시작한 공지 채널에 표시됩니다.

온라인 회고 모임은 `.env`의 `ONLINE_RETRO_MEETINGS`에 JSON 배열로 예약합니다. 실제 Slack 채널 ID와 모임 링크는 운영 `.env`에만 두며 문서나 커밋에 넣지 않습니다. `notify_at`부터 공지를 시도하고, 컨테이너 재시작에 대비해 모임 시작 후 1시간까지 미발송 공지를 복구합니다. 한 번 성공한 공지는 DB 기록으로 중복 발송을 막습니다.
공지를 받을 채널에는 봇을 미리 초대해야 합니다.

```dotenv
ONLINE_RETRO_MEETINGS='[{"session_name":"6기 1회차","starts_at":"2026-09-14T22:00:00+09:00","notify_at":"2026-09-14T21:50:00+09:00","writing_minutes":20,"channel":"C_REPLACE_ME","url":"https://replace.example/meeting"}]'
```

`writing_minutes`는 15~20분으로 설정합니다. 시작 시각이 되면 기존 직접 작성·질문형 회고 화면으로 연결되는 버튼을 보내고, 작성 시간이 끝나면 공유 시작 버튼을 제공합니다. 공유 순서는 출석 체크 순서와 회고 작성 완료 여부를 함께 표시합니다. 공유가 끝나면 참여자가 인증샷 단계로 넘기고, 이미지 업로드 버튼으로 채널에 인증샷을 게시할 수 있습니다.

공지의 `출석 체크`는 사용자별 첫 클릭 시각만 저장합니다. 같은 회차에서 다시 누르면 기존 기록을 유지합니다. 공유·인증샷 단계 전환은 출석 체크한 참여자라면 누구나 할 수 있으며 회차마다 한 번만 실행됩니다. 따라서 운영자가 모임에 직접 들어오지 않아도 참여자들이 진행할 수 있습니다. 출석 현황은 관리자 웹의 `온라인 모임 출석` 탭에서만 확인합니다. 인증샷을 찍기 전에는 참여자의 화면 공개 동의를 확인합니다. 일정을 운영하지 않을 때는 `ONLINE_RETRO_MEETINGS=[]`로 둡니다.

매회차 제출 공지는 `ANNOUNCEMENT_CHANNEL`에 회차마다 한 번 게시합니다. 공지 시각의 기본값은 마감 3일 10시간 전(화요일 05:00 마감 기준 금요일 19:00)이며, 회차별 공지 시각과 문구는 관리자 웹의 `회차 일정` 탭에서 고칩니다. 기본 문구가 모든 회차에 쓰이고, 특정 회차만 다르게 보내려면 그 회차 문구만 덮어씁니다. `{회차}`와 `{마감}`은 발송 시점에 치환됩니다. 이미 나갔거나 마감이 지난 회차의 공지는 고칠 수 없고, 봇이 멈춘 동안 마감까지 지난 회차의 공지는 뒤늦게 나가지 않습니다.

6기 1회차 마감 하루 전 리마인더는 아직 회차별로 일반화하지 않았습니다. 운영 환경(`ENV=prod`)에서만 동작하며, `SUBMISSION_TEAMS`가 설정돼 있어야 합니다.

회차 마감 8시간 전부터는 아직 회고를 내지 않은 사람에게만 개인 리마인더를 보냅니다. 대상은 발송 직전에 `SUBMISSION_TEAMS` 멤버에서 해당 회차 제출자를 빼서 정하며, 테스트 제출은 제출로 세지 않습니다. 특정 회차에 고정되지 않고 그때 열려 있는 회차를 따라갑니다.

알림은 봇과의 1:1 DM으로만 보냅니다. 공개 채널에는 누가 미제출인지 드러나지 않으며, 미제출자 명단은 로그와 운영 알림 어디에도 남기지 않습니다. DM이 실패하면 본인 팀 채널에 본인에게만 보이는 임시 메시지로 되돌리고, 실패와 폴백은 오류 코드와 인원 수만 담아 `ALERT_WEBHOOK_URL`로 한 건 알립니다. 한 사람에게 회차당 한 번만 보내며, 마감 직전 일괄 발송이라 한 명씩 간격을 두고 보냅니다. 운영 환경(`ENV=prod`)에서 `SUBMISSION_TEAMS`가 설정돼 있어야 동작합니다.

토큰과 키는 `.env`에만 저장합니다. `.env`는 Git에서 제외되어 있습니다.

질문형 회고의 AI 정리는 별도 Gemini API 키 대신 `agy`의 Google 계정 인증을 사용합니다. 인증정보는 전용 `antigravity-keyring` Docker 볼륨에 저장되며 호스트 사용자 키링과 공유하지 않습니다. 운영 미니 PC의 `.env` 권한은 `600`으로 유지합니다.

### 4. 실행과 확인

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
```

최초 배포 후 한 번만 컨테이너의 `agy`에 로그인합니다.

```bash
docker compose exec sigongbot agy
```

표시되는 URL을 브라우저에서 열어 로그인하고 인증 코드를 터미널에 붙여 넣습니다. 로그인 확인 후 `Ctrl+C`로 종료해도 인증은 전용 Docker 볼륨에 유지됩니다. 다음 명령이 모델 목록을 출력하면 비대화형 인증도 정상입니다.

```bash
docker compose exec sigongbot agy models
```

이후 컨테이너를 재시작하고 시작 로그의 `질문형 회고 AI CLI 준비 완료`와 실제 질문형 회고 제출을 확인합니다.

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

DB 백업은 다음 명령으로 만들고, 만들자마자 무결성과 행 수를 검증합니다.

```bash
scripts/backup_database.sh
scripts/restore_database.sh ~/sigongbot-backups/<백업파일>.db.gz
```

자동 백업 등록, 검증, 복원 훈련 절차는 [docs/backup-and-restore.md](docs/backup-and-restore.md)에 있습니다. 스키마 변경은 `database/migrations.py`의 마이그레이션으로만 적용되며 적용 이력은 DB의 `schema_migrations` 테이블에 남습니다.

## 로컬 개발

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

`ENV=dev`에서는 루트의 `.env`를 읽습니다.

팀 제출 라우팅 검증(Slack 게시 없이 가상 19명과 임시 SQLite 사용):

```bash
ENV=prod python -m unittest discover -s tests -v
python -m compileall -q config.py slack ai_review database main.py tests
```

실제 멤버 ID 배정, Slack 게시 권한, Socket Mode 연결, 컨테이너 healthy 여부와 `agy` 실행은 미니 PC에서 별도로 확인해야 합니다.
