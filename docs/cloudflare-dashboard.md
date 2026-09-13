# Cloudflare 관리자 대시보드 배포

이 구성은 공개 인바운드 포트 없이 Cloudflare Tunnel로 `/admin`을 전달하고,
Cloudflare Access와 앱 로그인을 두 겹의 인증으로 사용한다. 원본 서버는 호스트의
`127.0.0.1:8000`과 Compose 네트워크에서만 접근할 수 있다.

## 배포 전 확인

실제 Cloudflare 변경 전에 사용자에게 다음을 확인한다.

- 대시보드에 사용할 전용 호스트명(권장: `admin.sigonglife.link`)
- Access에서 허용할 관리자 이메일

이 확인과 승인 없이 DNS, Tunnel, Access 정책을 생성하거나 변경하지 않는다.

## 1. 백업과 환경변수

스키마 6이 관리자 계정과 세션 테이블을 추가하므로 먼저 DB를 백업한다.

```bash
scripts/backup_database.sh
```

운영 미니 PC의 `.env`에만 다음을 설정하고 `chmod 600 .env`를 유지한다.

```dotenv
DASHBOARD_SESSION_SECRET=<32자 이상 임의 값>
DASHBOARD_SESSION_HOURS=12
CLOUDFLARE_TUNNEL_TOKEN=<Tunnel 토큰>
```

임의 값은 `openssl rand -hex 32`로 생성할 수 있다. 값을 터미널 기록,
로그, 문서에 복사하지 않는다. `DASHBOARD_PASSWORD`는 더 이상 사용되지 않으므로
아래 계정 이관을 끝낸 뒤 `.env`에서 제거한다.

## 2. 관리자 계정

비밀번호는 명령 인자나 환경변수로 넘기지 않고 숨김 입력으로만 입력한다.

```bash
docker compose run --rm sigongbot python -m dashboard.admin_cli create admin
docker compose run --rm sigongbot python -m dashboard.admin_cli password admin
docker compose run --rm sigongbot python -m dashboard.admin_cli disable admin
docker compose run --rm sigongbot python -m dashboard.admin_cli enable admin
docker compose run --rm sigongbot python -m dashboard.admin_cli list
```

생성과 변경 비밀번호는 12자 이상이어야 하며 DB에는 scrypt 해시만 저장된다.
비밀번호 변경과 계정 비활성화는 해당 계정의 기존 세션을 모두 종료한다.

## 3. Tunnel과 Access

Cloudflare Zero Trust에서 다음과 같이 설정한다.

1. 미니 PC 전용 Tunnel을 생성하고 Docker 커넥터 토큰을 `.env`에 저장한다.
2. Public Hostname을 확인된 전용 호스트명으로 생성하고 Service URL을
   `http://sigongbot:8000`으로 설정한다.
3. Access 앱을 같은 호스트 전체(`admin.sigonglife.link/*`)에 연결한다.
4. Allow 정책에는 확인된 관리자 이메일만 추가하고, 너무 넓은 도메인·모두
   허용·Bypass 정책은 두지 않는다.
5. 같은 Tunnel에 `/admin`을 우회하는 다른 호스트명이나 더 넓은 공개 경로가 없는지
   확인한다.

## 4. 배포와 검증

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
docker compose config -q
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health
```

- 로컬 `/admin`이 로그인으로 이동하고 올바른 계정만 통과하는지 확인한다.
- 외부 URL에서 비허용 이메일은 Access에서 차단되고 허용 이메일만 앱
  로그인에 도달하는지 확인한다.
- 잘못된 비밀번호 5회 후 5분 잠김되고, CSRF 토큰 없는 재시도와
  로그아웃이 403으로 거부되는지 확인한다.
- 세션 쿠키에 `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/admin`이 있는지 확인한다.
- `sigongbot`과 `cloudflared`의 restart 정책이 `unless-stopped`인지, 상태와 로그가
  정상인지 확인한다. 토큰 값은 로그에 출력하지 않는다.
- Slack 공지, 직접 작성, 질문형 회고, 제출 채널 게시와 이미지 첨부를 재검증한다.
- `.env`, SQLite DB, 인증서, `.cloudflared/`가 Git 추적 대상이 아닌지 확인한다.

운영 데이터 보존을 위해 `docker compose down -v`는 실행하지 않는다.
