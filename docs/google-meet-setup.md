# Google Calendar · Meet 연동 준비

팀별 온라인 회고 시간을 확정하면 시공봇이 Google Calendar 이벤트와 전용 Google
Meet 링크를 만든다. 이 문서는 운영자가 미니 PC에서 한 번만 해 두면 되는 인증
준비 절차를 정리한다.

> 인증정보(OAuth 클라이언트 ID/시크릿, refresh token)는 절대 Git에 넣지 않는다.
> 운영 미니 PC의 `.env`(권한 `600`)나 Git 비추적 인증 파일에만 둔다.

## 왜 이 방식인가

| 선택지 | 판단 |
| --- | --- |
| **Calendar API `events.insert` + `conferenceData`** | **채택.** 공식 문서·에러 계약이 명확하고, 이벤트 ID로 멱등성을 보장할 수 있다. |
| Meet REST API `spaces.create` 단독 | 회의 공간이 일정과 묶이지 않고, `meetingCode`는 공식 문서가 "장기 저장하지 말라"고 명시한다. 입장 정책 설정에만 보조로 쓴다. |
| 서비스 계정 | 2020-03-02 이후 만든 서비스 계정은 도메인 전체 위임 없이는 게스트를 초대할 수 없고, 도메인 전체 위임은 Workspace 도메인이 필요하다. 개인 Google 계정 운영이면 쓸 수 없다. |
| 외부 CLI(`gws` 등) | 컨테이너 안에 별도 바이너리와 인증 상태를 더 들여야 한다. 운영 안정성이 떨어져 쓰지 않는다. |
| `agy`(Antigravity) | Google Workspace 인증이 아니다. 회고 AI 정리에만 쓴다. |

## 1. Google Cloud 프로젝트와 API 사용 설정

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트를 만든다.
2. **API 및 서비스 → 라이브러리**에서 아래를 사용 설정한다.
   - Google Calendar API (필수)
   - Google Meet API (선택. Meet 입장 정책을 API로 바꿀 때만 필요)

## 2. OAuth 동의 화면

동의 화면은 사용자에게 "이 앱이 Google 캘린더에 접근하려고 합니다"라고 묻는 허락
창이다. 여기에 무엇을 띄울지 등록해야 토큰을 받을 수 있다.

> 콘솔 메뉴 이름이 **Google 인증 플랫폼(Google Auth Platform)** 으로 바뀌었다.
> 예전 안내에 나오는 `API 및 서비스 → OAuth 동의 화면` 경로는 더 이상 보이지 않는다.

1. 콘솔 왼쪽 메뉴 → **Google 인증 플랫폼** → **시작하기**. 네 단계를 채운다.

   | 단계 | 입력값 |
   | --- | --- |
   | 브랜딩 | 앱 이름(허락 창에 표시된다), 사용자 지원 이메일 |
   | 대상 | **내부(Internal)** 또는 **외부(External)** |
   | 연락처 정보 | 프로젝트 알림을 받을 이메일 |
   | 완료 | 사용자 데이터 정책 동의 |

   Google Workspace 도메인 계정이면 **내부**를 고른다. 게시 상태 제약이 없다.
   개인 Google 계정이면 **외부**만 선택할 수 있다.

2. **데이터 액세스 → 범위 추가 또는 삭제**에서 아래 범위를 추가한다. 검색창에
   범위 이름을 붙여넣으면 찾을 수 있다. 목록에 없으면 1단계의 API 사용 설정을
   먼저 확인한다.

   | scope | 용도 | 필수 |
   | --- | --- | --- |
   | `https://www.googleapis.com/auth/calendar.events` | 이벤트 생성·수정·취소, Meet 회의 생성 | ✅ |
   | `https://www.googleapis.com/auth/meetings.space.settings` | Meet 공간의 `accessType` 변경 | 선택 |

3. 게시 상태를 정한다. 외부 유형은 여기서 갈린다.

   | | 프로덕션 | 테스트 |
   | --- | --- | --- |
   | refresh token 수명 | 만료 없음 | **7일** |
   | 사전 준비 | 홈페이지·개인정보처리방침·약관 URL + 소유 확인된 승인 도메인 | 테스트 사용자에 운영자 계정 추가 |
   | 운영 부담 | 없음 | 7일마다 재인증 |

   `프로덕션`이 정석이다. `대상` 페이지의 `앱 게시` 버튼을 쓰며, Branding 페이지에
   세 URL과 승인된 도메인이 채워져 있어야 버튼이 활성화된다. 게시용 정적 페이지는
   `site/` 폴더에 있다. 게시(publish)와 Google 심사(verification)는 다르다. 심사를
   받지 않아도 "확인되지 않은 앱" 경고만 뜬 채 100명까지 정상 동작한다.

   커뮤니티 내부 운영만 하고 도메인 준비가 부담이면 `테스트` 상태로 두고 `대상` →
   `테스트 사용자`에 운영자 계정을 추가해도 된다. 대신 refresh token이 7일마다
   만료되므로 아래 4장의 재발급 절차를 주기적으로 실행해야 한다.

## 3. OAuth 클라이언트와 refresh token 발급

1. **Google 인증 플랫폼 → 클라이언트 → 클라이언트 만들기**에서 애플리케이션
   유형을 **데스크톱 앱**으로 만든다. 클라이언트 ID와 시크릿을 받는다.
2. 미니 PC가 아닌 **브라우저를 쓸 수 있는 PC**에서 아래를 실행해 refresh token을
   받는다. 저장소에 스크립트를 두지 않는 이유는, 인증 과정에서 시크릿이 셸
   히스토리와 파일에 남기 때문이다. 받은 값만 미니 PC로 옮긴다.

   ```bash
   # 1) 브라우저에서 아래 주소를 연다. CLIENT_ID는 위에서 받은 값으로 바꾼다.
   #    access_type=offline과 prompt=consent가 있어야 refresh token이 내려온다.
   https://accounts.google.com/o/oauth2/v2/auth?client_id=CLIENT_ID&redirect_uri=http://localhost:8765&response_type=code&access_type=offline&prompt=consent&scope=https%3A//www.googleapis.com/auth/calendar.events%20https%3A//www.googleapis.com/auth/meetings.space.settings

   # 2) 동의 후 리다이렉트된 주소의 code= 값을 복사해 교환한다.
   curl -s https://oauth2.googleapis.com/token \
     -d client_id=CLIENT_ID \
     -d client_secret=CLIENT_SECRET \
     -d code=붙여넣은_코드 \
     -d grant_type=authorization_code \
     -d redirect_uri=http://localhost:8765
   ```

   응답의 `refresh_token` 값을 보관한다. 이 값은 한 번만 내려오므로 놓치면
   `prompt=consent`로 다시 받아야 한다.

3. 미니 PC의 `.env`에 값을 넣는다.

   ```bash
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   GOOGLE_OAUTH_REFRESH_TOKEN=...
   GOOGLE_CALENDAR_ID=primary
   GOOGLE_CALENDAR_TIMEZONE=Asia/Seoul
   GOOGLE_MEET_ACCESS_TYPE=OPEN
   ```

   `.env` 대신 Git 비추적 파일을 쓰려면 `GOOGLE_OAUTH_TOKEN_FILE`에 경로를 주고
   `{"client_id": "...", "client_secret": "...", "refresh_token": "..."}` 형태로
   저장한다. 컨테이너에서 읽으려면 파일이 Docker 볼륨 안에 있어야 한다.
   `.env` 값이 있으면 `.env`가 먼저다.

## 4. 토큰 유지와 재발급

- refresh token은 `.env`나 인증 파일에 남으므로 **컨테이너를 재시작해도 다시
  인증할 필요가 없다.** 액세스 토큰만 프로세스 메모리에 캐시하고 만료 2분 전에
  자동 갱신한다.
- 재발급은 전용 CLI로 한다. 브라우저 주소를 출력하고, 되돌아온 주소를 붙여넣으면
  토큰을 교환해 인증 파일에 저장한다. 클라이언트 시크릿과 인증 코드는 숨김
  입력으로만 받아 셸 기록에 남지 않는다.

  ```bash
  docker compose exec sigongbot python scripts/google_oauth_setup.py
  ```

- **게시 상태가 `테스트`라면 이 명령을 7일마다 실행한다.** 이때 refresh token을
  `.env`가 아니라 인증 파일로 관리하면 재시작이 필요 없다. 봇이 호출할 때마다
  파일을 다시 읽기 때문이다. 운영 `.env`는 이렇게 둔다.

  ```dotenv
  GOOGLE_OAUTH_CLIENT_ID=...        # 바뀌지 않으므로 .env에 둔다
  GOOGLE_OAUTH_CLIENT_SECRET=...    # 바뀌지 않으므로 .env에 둔다
  GOOGLE_OAUTH_REFRESH_TOKEN=       # 비워 둔다. 값이 있으면 파일보다 먼저 쓰인다
  GOOGLE_OAUTH_TOKEN_FILE=data/google_oauth_token.json
  ```

  `data/`는 `database-data` 볼륨이라 재빌드에도 인증 파일이 남는다. 파일 권한은
  CLI가 `600`으로 만든다.
- refresh token이 끊기는 경우는 공식 문서 기준 다음과 같다. 이때는 3단계를 다시
  한다.
  - 사용자가 앱 접근 권한을 철회했다
  - 6개월 동안 한 번도 쓰이지 않았다
  - 외부 유형 + 게시 상태 `테스트`라서 7일이 지났다
  - 한 계정에 발급된 refresh token이 100개를 넘어 가장 오래된 것이 밀려났다
- 갱신이 실패하면 확정 요청은 `failed`로 남고 관리자 웹에 사유가 그대로 뜬다.
  Slack에는 공지가 올라가지 않는다.

## 5. 운영자가 없어도 팀원이 입장하게 하기

Meet 입장 정책은 공식 문서 기준으로 세 가지다.

| accessType | 누가 노크 없이 들어오는가 |
| --- | --- |
| `OPEN` | "Anyone with a meeting link can join your meetings. No one has to knock." |
| `TRUSTED` | 주최자 조직 구성원 + Calendar로 초대된 외부인 |
| `RESTRICTED` | Calendar 일정으로 초대됐거나 회의 안에서 호스트가 부른 사람만 |

노크는 **주최자만 승인할 수 있다.** 운영자가 들어오지 않는 회고라면 팀원이
노크 단계에 걸리면 안 된다.

### 개인 Google 계정에서 확인한 사실 (2026-09-16)

Calendar API로 만든 Meet의 기본 설정은 **`accessType: TRUSTED`, `moderation: ON`**
이었다. 그리고 이 설정을 API로 바꾸는 것은 **거부됐다.**

| 호출 | 결과 |
| --- | --- |
| `spaces.get` (meetingCode로 조회) | `200` — 설정을 읽을 수 있다 |
| `spaces.patch` (`config.accessType`) | `403 PERMISSION_DENIED: Permission denied on resource Space` |

`meetings.space.settings` 스코프를 동의받았는데도 쓰기만 막힌다. 이 스코프를
Calendar가 만든 공간에 쓸 수 있다고 공지된 범위는 auto-artifacts였고,
`accessType`까지 보장한 적은 없다. Workspace 계정에서는 다를 수 있으나
개인 계정에서는 기대하지 않는 편이 안전하다.

### 그래서 어떻게 입장시키나

`TRUSTED`의 공식 정의가 그대로 해법이다. "Anyone outside the organization, but
invited through a Google Calendar event, can join without knocking." 개인 계정은
조직이 없으므로, **Calendar 참석자로 초대된 사람만 노크 없이 들어온다.**

1. **`ONLINE_RETRO_TEAM_ATTENDEES`로 팀원을 Calendar 참석자로 초대** (권장)
   팀 채널 ID별 이메일 목록을 JSON으로 준다. 한 번 설정하면 이후 확정마다
   자동으로 초대된다. 실제 이메일은 운영 `.env`에만 둔다.

   ```bash
   ONLINE_RETRO_TEAM_ATTENDEES='{"C_REPLACE_ME":["member@example.com"]}'
   ```

2. **확정 후 캘린더에서 직접 `열림`으로 바꾸기**
   이메일을 모으기 어려우면, 확정된 일정을 Google 캘린더에서 열어 Meet 액세스를
   `열림`으로 바꾼다. 링크만 있으면 누구나 들어온다. 팀당 회차마다 한 번 해야 한다.

참석자 초대는 입장만 허용한다. 호스트 권한이 넘어가지는 않으므로 초대된 사람이
다른 사람의 노크를 승인할 수는 없다. 회고 진행은 Slack 버튼이 끌고 가므로 호스트
권한은 필요하지 않다.

`GOOGLE_MEET_ACCESS_TYPE`은 `spaces.patch`가 허용되는 계정에서만 의미가 있다.
값을 넣으면 확정할 때 그 값으로 바꾸려 시도하고, 실패해도 이벤트와 Meet 링크는
그대로 쓸 수 있으며 관리자 웹과 로그에 경고만 남는다. 개인 계정이라면 비워 두어
실패가 확정된 호출을 매번 하지 않게 한다.

또 하나 확인할 것: Meet 설정의 **"Host must join before anyone else can join"**
(호스트 우선 입장) 체크는 꺼 두어야 한다. 이 옵션이 켜져 있으면 accessType과
무관하게 주최자가 들어오기 전까지 아무도 입장하지 못한다. Workspace 관리자가
도메인 기본값을 정할 수 있으므로, 조직 계정이라면 관리 콘솔의 Meet 접근 설정도
함께 확인한다.


## 6. 중복 생성·재시도·시간 변경

- 확정 요청은 Google을 부르기 **전에** DB에 `pending`으로 먼저 남는다. 성공하면
  이벤트 ID와 Meet 주소를 채우고, 실패하면 `failed`와 사유를 남긴다.
- 이벤트 ID는 `(meeting_date, team_channel, is_test)`로 결정되는 고정값이다. 같은
  팀·같은 날짜면 몇 번을 눌러도 같은 ID를 쓴다. DB 기록이 사라진 상태에서 다시
  눌러도 Calendar가 409 "The requested identifier already exists"를 돌려주고,
  봇은 기존 이벤트를 읽어 갱신으로 넘어간다.
- `online_retro_confirmed_meetings`에는 `poll_id` UNIQUE와
  `(meeting_date, team_channel, is_test)` UNIQUE가 함께 걸려 있다.
- 시간을 바꾸면 새 이벤트를 만들지 않고 기존 이벤트를 `events.patch`로 갱신한다.
  Meet 링크는 그대로 유지되고 Slack 확정 공지는 수정된다.
- 확정을 취소하면 이벤트를 지우지 않고 `status: cancelled`로 바꾼다. 같은 ID로
  다시 확정할 수 있다.

## 7. 할당량과 실패 안내

- Calendar API 한도는 프로젝트당 분당 10,000회, 사용자당 분당 600회다. 이번
  기능은 팀 수만큼만 호출하므로 여유가 크다.
- `429`, `403 rateLimitExceeded`/`userRateLimitExceeded`, `5xx`는 공식 권고대로
  지수 백오프(최대 32초)로 최대 5회까지 다시 시도한다.
- 끝내 실패하면 Slack 공지는 올리지 않고, 관리자 웹의 `온라인 회고 확정` 탭에
  상태 `실패`와 오류 본문이 남는다. 같은 화면에서 그대로 다시 시도하면 된다.

## 8. 확인 순서

1. 관리자 웹 → `온라인 회고 확정` 탭에 팀별 투표 결과가 보이는지
2. 시간을 고르고 `시간 확정 및 Meet 생성`을 눌렀을 때 Meet 링크가 생기는지
3. 같은 버튼을 다시 눌러도 Calendar에 이벤트가 하나만 있는지
4. 팀 채널에 확정 공지와 `Google Meet 입장` · `출석 체크` 버튼이 올라갔는지
5. 운영자가 아닌 계정으로 Meet 링크에 들어가 노크 없이 입장되는지
6. 컨테이너를 재시작한 뒤에도 확정 정보와 Meet 링크가 남아 있는지
