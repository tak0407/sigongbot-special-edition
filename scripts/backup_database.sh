#!/usr/bin/env bash
# 미니 PC 호스트에서 실행하는 운영 SQLite 백업 스크립트.
#
# 실행 중인 컨테이너 안에서 SQLite 온라인 백업을 떠 gzip 아카이브로 호스트에
# 저장하고, 저장한 아카이브를 다시 열어 복원 가능한지까지 검증한다.
#
# 환경변수:
#   SIGONGBOT_BACKUP_DIR   백업 저장 위치 (기본: ~/sigongbot-backups)
#   SIGONGBOT_BACKUP_KEEP  보관할 백업 개수 (기본: 14)
#   SIGONGBOT_SERVICE      Compose 서비스 이름 (기본: sigongbot)
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_DIR"

SERVICE=${SIGONGBOT_SERVICE:-sigongbot}
BACKUP_DIR=${SIGONGBOT_BACKUP_DIR:-$HOME/sigongbot-backups}
KEEP=${SIGONGBOT_BACKUP_KEEP:-14}

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

timestamp=$(date +%Y%m%d-%H%M%S)
archive="$BACKUP_DIR/sigongbot-$timestamp.db.gz"
partial="$archive.part"
trap 'rm -f "$partial"' EXIT

if docker compose ps --services --status running 2>/dev/null | grep -qx "$SERVICE"; then
    echo "[$(date +%F' '%T)] 실행 중인 컨테이너에서 온라인 백업을 생성합니다."
    docker compose exec -T "$SERVICE" \
        python scripts/backup_database.py create --output - > "$partial"
else
    echo "[$(date +%F' '%T)] 컨테이너가 멈춰 있어 임시 컨테이너로 백업합니다."
    docker compose run --rm --no-deps -T --entrypoint python "$SERVICE" \
        scripts/backup_database.py create --output - > "$partial"
fi

mv "$partial" "$archive"
chmod 600 "$archive"
trap - EXIT

# 저장된 아카이브를 실제로 풀어서 무결성과 행 수를 확인한다.
if ! python3 scripts/backup_database.py verify "$archive"; then
    echo "백업 검증 실패: $archive" >&2
    exit 1
fi

# 오래된 백업 정리 (최신 $KEEP개 보관)
mapfile -t archives < <(ls -1t "$BACKUP_DIR"/sigongbot-*.db.gz 2>/dev/null || true)
if (( ${#archives[@]} > KEEP )); then
    for stale in "${archives[@]:KEEP}"; do
        rm -f "$stale"
        echo "오래된 백업 삭제: $stale"
    done
fi

echo "[$(date +%F' '%T)] 백업 완료: $archive"
