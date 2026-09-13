#!/usr/bin/env bash
# 백업 아카이브로 운영 SQLite를 복원한다.
#
#   scripts/restore_database.sh ~/sigongbot-backups/sigongbot-20260913-040000.db.gz
#
# 복원은 현재 운영 데이터를 덮어쓰므로 실행 전에 현재 DB를 먼저 백업하고,
# 사용자 확인을 받은 뒤에만 진행한다. `--yes`를 주면 확인을 건너뛴다.
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_DIR"

SERVICE=${SIGONGBOT_SERVICE:-sigongbot}
DB_PATH=${SIGONGBOT_CONTAINER_DB_PATH:-/app/data/sigongbot.db}

archive=${1:-}
assume_yes=${2:-}
if [[ -z "$archive" ]]; then
    echo "사용법: scripts/restore_database.sh <백업파일.db.gz> [--yes]" >&2
    exit 2
fi
if [[ ! -f "$archive" ]]; then
    echo "백업 파일을 찾을 수 없습니다: $archive" >&2
    exit 2
fi

echo "복원할 백업을 검증합니다: $archive"
python3 scripts/backup_database.py verify "$archive"

if [[ "$assume_yes" != "--yes" ]]; then
    read -r -p "현재 운영 DB를 위 백업으로 덮어씁니다. 계속하려면 yes 입력: " answer
    [[ "$answer" == "yes" ]] || { echo "복원을 취소했습니다."; exit 1; }
fi

echo "복원 전 현재 DB를 먼저 백업합니다."
scripts/backup_database.sh

echo "컨테이너를 중지합니다."
docker compose stop "$SERVICE"

# WAL/SHM이 남아 있으면 복원한 파일과 섞여 손상될 수 있으므로 함께 제거한다.
echo "백업 파일을 볼륨에 씁니다."
gzip -dc "$archive" | docker compose run --rm --no-deps -T --entrypoint sh "$SERVICE" -c \
    "rm -f '$DB_PATH' '$DB_PATH-wal' '$DB_PATH-shm' && cat > '$DB_PATH' && chmod 600 '$DB_PATH'"

echo "컨테이너를 다시 시작합니다."
docker compose up -d "$SERVICE"

echo "복원 결과를 확인합니다."
for attempt in $(seq 1 30); do
    if docker compose exec -T "$SERVICE" python scripts/backup_database.py verify; then
        echo "복원 완료: $archive"
        exit 0
    fi
    sleep 2
done

echo "복원 후 검증에 실패했습니다. docker compose logs --tail=200 $SERVICE 를 확인하세요." >&2
exit 1
