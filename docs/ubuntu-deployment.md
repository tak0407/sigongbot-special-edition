# Ubuntu 미니 PC 배포 인계

이 문서는 Ubuntu 미니 PC에서 Codex가 시공봇을 배포할 때 따르는 실행 체크리스트다.

## 0. 시작 조건

- 작업 위치는 Ubuntu 미니 PC다.
- 인터넷과 GitHub 접속이 가능하다.
- 사용자가 `sudo`를 승인할 수 있다.
- Slack Bot Token과 App Token은 사용자가 직접 준비한다.
- 맥북에서 실행 중인 동일 Slack App 봇은 전환 직전에 중지한다.

비밀값을 터미널 출력, 대화, Git, 문서에 복사하지 않는다. `.env` 내용 전체를 출력하지 않는다.

## 1. 시스템 점검

다음을 읽기 전용으로 확인한다.

```bash
uname -a
cat /etc/os-release
df -h
free -h
docker --version
docker compose version
git --version
```

Docker가 없다면 현재 Ubuntu 버전에 맞는 Docker 공식 설치 문서를 확인해 Docker Engine과 Compose 플러그인을 설치한다. 설치 후 일반 사용자로 `docker ps`를 실행할 수 있는지 확인한다. 그룹 변경 후에는 재로그인이 필요할 수 있다.

## 2. 저장소 준비

기본 설치 경로는 `~/apps/sigongbot-special-edition`이다. 기존 디렉터리가 있으면 덮어쓰거나 삭제하지 말고 Git 상태를 먼저 확인한다.

```bash
mkdir -p ~/apps
cd ~/apps
git clone https://github.com/tak0407/sigongbot-special-edition.git
cd sigongbot-special-edition
git status --short
git log -1 --oneline
```

## 3. 환경변수 준비

Google Drive의 비공개 폴더 `sigongbot-special-edition 미니PC 이전`에서 `.env`를 미니 PC로 내려받는다. 브라우저의 기본 다운로드 폴더에 저장됐다면 저장소 루트로 복사하고, 원본 다운로드 파일은 정상 복사를 확인한 뒤 안전하게 삭제한다.

```bash
cp ~/Downloads/.env ~/apps/sigongbot-special-edition/.env
chmod 600 .env
```

다운로드 파일명이 브라우저에 의해 바뀌었다면 실제 파일명을 먼저 확인하고 정확한 경로를 사용한다. 기존 `.env`가 있으면 덮어쓰기 전에 별도 백업하고 사용자에게 알린다. Drive에서 파일을 받을 수 없을 때만 `.env.example`을 복사해 사용자가 직접 값을 입력한다.

최소 확인 항목:

- `ENV=prod`
- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `ADMIN_CHANNEL`
- `SUPPORT_CHANNEL`
- `ADMIN_IDS`
- `DATABASE_PATH=data/sigongbot.db`
- 테스트 중이면 `SESSION_NAME_OVERRIDE=테스트 회차`
- 테스트 공지와 제출 채널을 분리하면 `TEST_SUBMISSION_CHANNEL`

값 자체를 출력하지 말고 키의 존재와 빈 값 여부만 검증한다. `.env`가 Git에서 제외되는지 확인한다.

```bash
git check-ignore -v .env
```

## 4. AI 런타임 결정 게이트

현재 Dockerfile은 Antigravity CLI를 설치하지 않는다. 아래 중 하나가 검증되기 전에는 AI 기능 배포 완료로 보고하지 않는다.

1. 컨테이너 이미지에 `agy`를 설치하고 인증정보를 안전한 읽기 전용 볼륨으로 제공한다.
2. 봇을 Docker 밖의 제한된 systemd 서비스로 실행해 호스트의 인증된 `agy`를 사용한다.
3. 별도 API 기반 AI 처리기로 교체한다.

어떤 방식이든 다음을 검증한다.

- `agy` 실행 파일 탐색 성공
- 비대화형 인증 유지
- `--sandbox --mode plan` 적용
- 텍스트 구조화 출력 성공
- 실제 테스트 이미지 판독 성공
- 재부팅 후 인증과 실행 유지

AI 런타임이 준비되지 않았으면 일반 회고 기능만 먼저 배포할 수 있지만, 질문형 회고는 기본 매핑으로 대체되고 이미지 AI 리뷰는 실패할 수 있음을 사용자에게 명확히 알린다.

## 5. 전환 전 정적 검증

```bash
python3 -m compileall -q .
docker compose config --quiet
git status --short
```

실제 `.env`, `data/`, `temp/`, `runtime/`이 Git 추적 대상이 아닌지 확인한다.

## 6. 단일 실행 인스턴스 전환

동일한 Slack App Token을 사용하는 맥북 봇과 미니 PC 봇을 동시에 실행하지 않는다.

1. 맥북 봇을 중지한다.
2. 미니 PC에서 컨테이너를 시작한다.

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=200 sigongbot
curl --fail http://127.0.0.1:8000/health
```

로그나 상태 출력에 토큰이 포함되지 않았는지 확인한다.

## 7. Slack 기능 검증

테스트 공지에서 다음 순서로 확인한다.

1. `회고 제출하기` 버튼이 열린다.
2. 직접 작성 방식으로 제출된다.
3. 질문형 회고가 한 문항씩 진행되고 이전 질문 이동이 된다.
4. 질문형 AI 정리 완료 알림과 확인 버튼이 도착한다.
5. 결과가 설정된 제출 채널에 게시된다.
6. 이미지를 첨부하면 회고 스레드에 AI 리뷰가 게시된다.

실패 시 `docker compose logs --tail=300 sigongbot`으로 확인하되 `.env`나 토큰을 출력하지 않는다.

## 8. 재부팅 복구 검증

사용자 승인 후 미니 PC를 재부팅하고 다음을 확인한다.

```bash
docker compose ps
curl --fail http://127.0.0.1:8000/health
docker compose logs --tail=100 sigongbot
```

`restart: unless-stopped` 정책으로 컨테이너가 자동 복구되어야 한다.

## 9. 업데이트와 백업

업데이트 전 SQLite 볼륨을 백업하고 운영 중인 컨테이너를 확인한다. 데이터가 있는 상태에서 `docker compose down -v`를 실행하지 않는다.

코드 업데이트 기본 순서:

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health
```

백업·복원 명령은 실제 Compose 볼륨 이름을 조회한 뒤 별도 문서로 확정한다. 추정한 볼륨 이름으로 삭제·복원하지 않는다.

## 10. 완료 보고

완료 보고에는 다음만 포함한다.

- 설치 경로와 배포 커밋
- 컨테이너 상태와 health 결과
- Slack 기능별 성공·실패
- AI 런타임 방식과 재부팅 후 인증 상태
- 남은 블로커

토큰, `.env` 내용, 인증 파일 내용은 포함하지 않는다.
