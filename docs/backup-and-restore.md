# SQLite 백업과 복원

운영 데이터는 Docker named volume `sigongbot-special-edition_database-data`의 단일 SQLite 파일 하나(`/app/data/sigongbot.db`)에 모여 있다. 이 파일이 사라지면 회고 기록도 함께 사라지므로 자동 백업과 복원 절차를 이 문서로 고정한다.

## 1. 원칙

- 운영 중인 파일을 `cp`로 복사하지 않는다. WAL 때문에 깨진 사본이 생긴다. `scripts/backup_database.py`는 SQLite 온라인 백업 API를 사용한다.
- 백업은 만든 직후 반드시 검증한다. 검증하지 않은 백업은 백업이 아니다.
- `docker compose down -v`와 `docker volume rm`은 실행하지 않는다.
- 백업 파일에는 회고 본문이 들어 있으므로 권한 `600`, 디렉터리 권한 `700`을 유지하고 Git이나 공개 저장소에 올리지 않는다.

## 2. 수동 백업

미니 PC에서 `compose.yaml`이 있는 저장소 디렉터리로 이동해 실행한다. 아래 예시의 경로는 실제 설치 경로로 바꾼다(`docker inspect sigongbot-special-edition --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`로 확인한다).

```bash
scripts/backup_database.sh
```

동작 순서는 다음과 같다.

1. 실행 중인 컨테이너 안에서 온라인 백업을 떠 gzip으로 압축해 받는다. 컨테이너가 멈춰 있으면 같은 볼륨을 붙인 임시 컨테이너로 백업한다.
2. `~/sigongbot-backups/sigongbot-<날짜>-<시각>.db.gz`에 `600` 권한으로 저장한다.
3. 저장한 아카이브를 다시 풀어 `PRAGMA integrity_check`, foreign key 검사, 테이블별 행 수, 스키마 버전을 출력한다.
4. 최신 14개만 남기고 오래된 백업을 지운다.

환경변수로 조정한다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SIGONGBOT_BACKUP_DIR` | `~/sigongbot-backups` | 백업 저장 위치 |
| `SIGONGBOT_BACKUP_KEEP` | `14` | 보관 개수 |
| `SIGONGBOT_SERVICE` | `sigongbot` | Compose 서비스 이름 |

출력 예시(값은 상황에 따라 다르다).

```json
{"path": "data/sigongbot.db", "bytes": 90112, "integrity_check": "ok", "foreign_key_violations": 0, "schema_version": 4, "counts": {"retrospectives": 6, "guided_reflections": 10, "ai_review_jobs": 1, "scheduled_announcements": 1}}
```

`integrity_check`가 `ok`가 아니거나 `counts.retrospectives`가 예상보다 적으면 그 백업을 신뢰하지 않는다.

## 3. 자동 백업 등록

`crontab -e`에 다음 한 줄을 추가한다. 매일 새벽 4시에 백업하고 로그를 남긴다.

```cron
0 4 * * * cd ~/sigongbot-special-edition && scripts/backup_database.sh >> ~/sigongbot-backups/backup.log 2>&1
```

등록 후 확인한다.

```bash
crontab -l
scripts/backup_database.sh            # 즉시 1회 실행해 동작 확인
ls -l ~/sigongbot-backups
```

미니 PC 디스크 자체가 고장 나면 같은 디스크의 백업도 함께 사라진다. 주기적으로 최신 아카이브 하나를 외장 디스크나 Google Drive 비공개 폴더로 옮긴다.

## 4. 백업 검증

아카이브든 운영 DB든 같은 명령으로 검사한다.

```bash
python3 scripts/backup_database.py verify ~/sigongbot-backups/sigongbot-20260913-040000.db.gz
docker compose exec -T sigongbot python scripts/backup_database.py verify
```

## 5. 복원

복원은 현재 운영 데이터를 덮어쓴다. 사용자 승인 없이 실행하지 않는다.

```bash
scripts/restore_database.sh ~/sigongbot-backups/sigongbot-20260913-040000.db.gz
```

동작 순서는 다음과 같다.

1. 복원할 아카이브를 먼저 검증한다.
2. `yes`를 입력받는다. 무인 실행이 필요하면 두 번째 인자로 `--yes`를 준다.
3. 현재 DB를 먼저 백업한다. 잘못 복원해도 되돌릴 수 있게 한다.
4. 컨테이너를 중지하고, 볼륨의 `sigongbot.db`와 `-wal`, `-shm`을 지운 뒤 백업 내용을 새로 쓴다.
5. 컨테이너를 다시 올리고 컨테이너 안에서 무결성과 행 수를 확인한다.

## 6. 복원 훈련

백업이 실제로 복구 가능한지는 복원해 봐야 알 수 있다. 운영 볼륨을 건드리지 않고 훈련하려면 임시 디렉터리에 풀어 검사한다.

```bash
mkdir -p /tmp/restore-drill
gzip -dc ~/sigongbot-backups/sigongbot-20260913-040000.db.gz > /tmp/restore-drill/sigongbot.db
python3 scripts/backup_database.py verify /tmp/restore-drill/sigongbot.db
rm -rf /tmp/restore-drill
```

분기마다 한 번, 그리고 스키마 마이그레이션을 추가한 배포 직후에 훈련한다.

## 7. 스키마 마이그레이션과 백업 순서

스키마 변경이 포함된 배포는 다음 순서를 지킨다.

```bash
scripts/backup_database.sh        # 1. 변경 전 백업
git pull --ff-only                # 2. 코드 업데이트
docker compose up -d --build      # 3. 재기동 (시작 시 마이그레이션 자동 적용)
docker compose logs --tail=100 sigongbot | grep 마이그레이션
docker compose exec -T sigongbot python scripts/backup_database.py verify
```

마이그레이션 이력은 DB의 `schema_migrations` 테이블에 남는다. 적용 목록은 `database/migrations.py`의 `MIGRATIONS`와 같아야 한다. 새 스키마 변경은 이미 배포된 마이그레이션을 수정하지 말고 항상 새 버전을 끝에 추가한다.

`중복 회고가 남아 있어 UNIQUE 제약을 적용하지 못했습니다` 경고가 보이면 기동은 계속되지만 4번 마이그레이션이 보류된 상태다. 로그에 찍힌 사용자와 회차의 중복 행을 정리한 뒤 재기동하면 적용된다.
