# 시공삶 공개 사이트 배포

`sigonglife.link`는 공개 소개 사이트, `admin.sigonglife.link`는 기존 관리자 웹이다.
사이트 소스는 `site/public/`이며 별도 빌드·Node.js·로그인·DB가 필요 없다.
`website` 서비스는 공개 파일과 Nginx 설정만 읽기 전용으로 마운트한다.
호스트 포트를 열지 않고 기존 Compose 네트워크의 Cloudflare Tunnel로 연결한다.

## 1. 미니 PC에서 사이트만 시작

운영 체크아웃에서 실행한다. 맥북에서 봇을 추가로 실행하지 않는다.

```bash
cd ~/sigongbot-special-edition
git status --short
git pull --ff-only
docker compose config -q
docker compose up -d --no-deps website
docker compose exec -T website nginx -t
docker compose ps website
docker compose exec -T website wget -qO- http://127.0.0.1/health
docker compose exec -T website wget -qO- http://127.0.0.1/privacy/ > /dev/null
docker compose exec -T website wget -qO- http://127.0.0.1/terms/ > /dev/null
```

컨테이너가 `healthy`이고 health 응답이 `ok`인지 확인한다.
이 명령은 `sigongbot`이나 `cloudflared`를 재시작하지 않는다. 기존 `.env`의
Tunnel 토큰을 사용하며 새로운 운영 비밀값은 필요하지 않다.
`docker compose down -v`는 실행하지 않는다.

## 2. 루트 도메인 연결

Cloudflare Zero Trust에서 **현재 운영 중인 Tunnel**의 Published application
routes / Public Hostnames에 다음 경로를 추가한다.

| 항목 | 값 |
| --- | --- |
| Subdomain | 비워 둠 |
| Domain | `sigonglife.link` |
| Path | 비워 둠 |
| Service type | `HTTP` |
| URL | `website:80` |

현재 루트 도메인은 SSL 오류 525가 확인된 상태였다(2026-11-08).
DNS의 루트(`@`)에 기존 A/AAAA/CNAME이 있으면 연결 대상을 확인하고 루트 레코드만
이 Tunnel 대상으로 교체한다. 기존 레코드 값은 되돌릴 수 있도록 별도 기록한다.
Tunnel 화면에서 DNS가 자동 생성되면 중복 레코드를 추가하지 않는다.
외부는 HTTPS, Tunnel에서 Nginx까지는 **HTTP**를 사용한다.

- `admin.sigonglife.link`의 DNS·Tunnel 경로·Access 정책은 유지한다.
- 루트 도메인에 관리자 서비스(`sigongbot:8000`)를 연결하지 않는다.
- 공개 사이트는 로그인 없이 열려야 한다. 기존 와일드카드 Access 정책이 루트까지
  보호하는 경우 관리자 호스트의 보호를 유지하면서 공개 사이트와 대상을 분리한다.
  관리자 호스트에 Bypass나 전체 허용 정책을 추가하지 않는다.
- 라우트가 같은 Compose 프로젝트의 `website`로 연결되어야 한다. 별도 Tunnel
  컨테이너를 사용 중이면 해당 네트워크 접근을 먼저 확인한다.

## 3. 공개 확인과 Google 브랜딩

로그인하지 않은 브라우저에서 세 페이지를 확인한다.

```bash
curl --fail --head https://sigonglife.link/
curl --fail --head https://sigonglife.link/privacy/
curl --fail --head https://sigonglife.link/terms/
curl --fail https://sigonglife.link/health
```

| Google 인증 플랫폼 → 브랜딩 | 값 |
| --- | --- |
| 앱 이름 | 시공삶 |
| 사용자 지원 이메일 / 개발자 연락처 | `tak9447@gmail.com` |
| 홈페이지 | `https://sigonglife.link/` |
| 개인정보처리방침 | `https://sigonglife.link/privacy/` |
| 서비스 약관 | `https://sigonglife.link/terms/` |
| 승인된 도메인 | `sigonglife.link` |
| 로고 | `assets/export/SigongLifeLogo.png` |

공개 전에 개인정보처리방침의 처리 항목, 외부 서비스, 보관·삭제 방식이 실제
운영과 일치하는지 확인한다. Google Search Console에서 요구하는 도메인 소유권
확인은 DNS TXT 방식으로 진행할 수 있다. 앱 게시와 브랜드/권한 심사는 별개이며,
로고·앱 이름 표시에는 브랜드 확인이 요구될 수 있다. 이 사이트 배포만으로 OAuth
심사가 승인되거나 이슈 #10이 완료되는 것은 아니다.

프로덕션 전환과 운영 컨테이너 재인증은 [Google 연동 안내](google-meet-setup.md)를
따른다. 새 토큰 자동 갱신·검증용 일정 및 Meet 생성·7일 이후 갱신을 별도로 확인한다.

## 4. 이후 수정 및 검증

HTML·CSS 수정은 `git pull --ff-only` 후 새로고침하면 반영된다. Nginx 설정을 바꿨다면
`docker compose exec -T website nginx -t`가 통과한 뒤 `docker compose restart website`를
실행한다. 사이트 컨테이너는 `restart: unless-stopped`로 재부팅 후 복구된다.

배포 후 기존 관리자 로그인, 봇 health 및 Slack 연결 로그도 확인한다.
운영 로그에 토큰이나 개인 회고 내용이 있으면 공유하지 않는다.

```bash
docker compose ps
docker compose exec -T sigongbot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status)"
docker compose logs --since 5m sigongbot
```

로컬 사이트 검증(봇 실행 없음):

```bash
python3 -m unittest discover -s tests -p 'test_website.py' -v
python3 -m http.server 8766 --bind 127.0.0.1 --directory site/public
```

사이트 중단은 `docker compose stop website`만 사용한다. DB 볼륨이나 봇을 내리지 않는다.

참고: [Google 브랜드 확인](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification),
[Google API 사용자 데이터 정책](https://developers.google.com/terms/api-services-user-data-policy).
