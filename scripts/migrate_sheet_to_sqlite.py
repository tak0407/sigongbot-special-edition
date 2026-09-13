"""Google Sheets에 남아 있던 1~5기 회고와 사용자 명단을 운영 SQLite로 옮긴다.

시공봇이 SQLite로 넘어오기 전의 회고는 시트에만 있고 DB에는 6기부터 쌓인다.
이 스크립트는 시트 export CSV 두 개를 읽어 `retrospectives`와 `users`에 넣는다.
한 트랜잭션으로 처리하며, 이미 들어간 id는 건너뛰므로 여러 번 실행해도 안전하다.

사용법:
    python scripts/migrate_sheet_to_sqlite.py \
        --retrospectives <회고 CSV> --users <명단 CSV> [--db <경로>] [--dry-run]

시트 ID와 원문, 실명은 저장소에 두지 않는다. CSV는 내려받아 저장소 밖에 둔다.
CSV는 다음으로 받을 수 있다.
    curl -sL "https://docs.google.com/spreadsheets/d/<시트ID>/gviz/tq?tqx=out:csv&gid=<탭 gid>" -o out.csv

회고 CSV는 행 레이아웃이 두 가지로 섞여 있다. 구버전 행은 2번째 칸에 실명이
있고, 신버전 행은 실명 칸이 없어 필드가 한 칸씩 밀려 있다. 헤더만 믿고
파싱하면 user_id 자리에 회고 본문이 들어가므로 행마다 판별한다.
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from database.sqlite import initialize_database  # noqa: E402

RETROSPECTIVE_COLUMNS = [
    "id", "user_id", "good_points", "improvements", "learnings", "action_item",
    "emotion_score", "emotion_reason", "session_name", "slack_channel",
    "slack_ts", "created_at", "updated_at",
]
TEXT_FIELDS = ["good_points", "improvements", "learnings", "action_item"]
USER_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")

# 운영 데이터가 아니라 봇 점검 중 남은 행. 본문이 "테스트", "ㅇㅇㅇ"뿐이다.
# 길이 기준으로 거르면 짧게 쓴 실제 회고까지 걸려서 id로 지정한다.
EXCLUDED_RETROSPECTIVE_IDS = frozenset({1028, 1029, 1031})


def parse_retrospectives(path: Path) -> list[dict[str, str]]:
    """섞여 있는 두 레이아웃을 한 형태로 정규화한다."""
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.reader(handle))[1:]

    records = []
    for line, row in enumerate(rows, start=2):
        if USER_ID.match(row[1].strip()):  # 실명 칸 없음 - 한 칸씩 밀림
            values = row[0:11] + [row[12], row[13]]
        else:  # 실명 칸 있음
            values = [row[0]] + row[2:14]
        record = dict(zip(RETROSPECTIVE_COLUMNS, values))
        record["_line"] = str(line)
        records.append(record)
    return records


def to_sqlite_timestamp(value: str) -> str:
    """시트의 ISO8601을 봇이 쓰는 CURRENT_TIMESTAMP 형식(UTC)에 맞춘다.

    두 형식이 섞이면 created_at 문자열 정렬이 같은 날짜 안에서 어긋난다.
    """
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def build_retrospective_rows(records: list[dict[str, str]]) -> list[tuple]:
    rows = []
    for record in records:
        identifier = int(record["id"])
        if identifier in EXCLUDED_RETROSPECTIVE_IDS:
            continue

        user_id = record["user_id"].strip()
        if not USER_ID.match(user_id):
            raise ValueError(
                f"{record['_line']}행: user_id 형식이 올바르지 않습니다. "
                "레이아웃 판별이 어긋났는지 확인하세요."
            )
        for field in TEXT_FIELDS:
            if not record[field].strip():
                raise ValueError(f"{record['_line']}행: {field}가 비어 있습니다.")

        score = record["emotion_score"].strip()
        reason = record["emotion_reason"].strip()
        created_at = to_sqlite_timestamp(record["created_at"])
        # 신버전 행은 updated_at이 비어 있는데 스키마가 NOT NULL이다.
        updated_at = record["updated_at"].strip()

        rows.append((
            identifier,
            user_id,
            record["session_name"].strip(),
            record["slack_channel"].strip(),
            record["slack_ts"].strip(),
            record["good_points"],
            record["improvements"],
            record["learnings"],
            record["action_item"],
            int(score) if score else None,
            reason or None,
            created_at,
            to_sqlite_timestamp(updated_at) if updated_at else created_at,
        ))
    return rows


def build_user_rows(path: Path) -> list[tuple]:
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.reader(handle))[1:]

    users = []
    for line, row in enumerate(rows, start=2):
        user_id = row[0].strip()
        if not user_id:
            continue
        if not USER_ID.match(user_id):
            raise ValueError(f"{line}행: ID 형식이 올바르지 않습니다: {user_id!r}")
        real_name = row[1].strip()
        if not real_name:
            raise ValueError(f"{line}행: Real Name이 비어 있습니다.")
        # 앱 계정은 이메일 칸이 비어 있다.
        users.append((user_id, real_name, row[2].strip() or None))
    return users


def insert_retrospectives(
    connection: sqlite3.Connection, rows: list[tuple]
) -> tuple[int, list[int]]:
    existing = {
        row[0] for row in connection.execute("SELECT id FROM retrospectives")
    }
    conflicts = []
    fresh = []
    for row in rows:
        if row[0] in existing:
            conflicts.append(row[0])
        else:
            fresh.append(row)

    connection.executemany(
        """
        INSERT INTO retrospectives (
            id, user_id, session_name, slack_channel, slack_ts,
            good_points, improvements, learnings, action_item,
            emotion_score, emotion_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        fresh,
    )
    return len(fresh), conflicts


def insert_users(connection: sqlite3.Connection, rows: list[tuple]) -> tuple[int, int]:
    existing = {row[0] for row in connection.execute("SELECT user_id FROM users")}
    fresh = [row for row in rows if row[0] not in existing]
    connection.executemany(
        "INSERT INTO users (user_id, real_name, email) VALUES (?, ?, ?)", fresh
    )
    return len(fresh), len(rows) - len(fresh)


def sync_autoincrement(connection: sqlite3.Connection) -> int:
    """옮긴 id 다음 번호부터 발급되도록 AUTOINCREMENT 시퀀스를 맞춘다."""
    largest = connection.execute("SELECT MAX(id) FROM retrospectives").fetchone()[0]
    if largest is None:
        return 0
    connection.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'retrospectives' AND seq < ?",
        (largest, largest),
    )
    return largest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrospectives", required=True, type=Path)
    parser.add_argument("--users", type=Path)
    parser.add_argument("--db", type=Path, default=Path(settings.DATABASE_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    records = parse_retrospectives(args.retrospectives)
    retrospective_rows = build_retrospective_rows(records)
    user_rows = build_user_rows(args.users) if args.users else []

    skipped = len(records) - len(retrospective_rows)
    print(f"회고 CSV {len(records)}행 - 테스트 {skipped}행 제외 = {len(retrospective_rows)}행")
    if user_rows:
        print(f"명단 CSV {len(user_rows)}행")

    if args.dry_run:
        print("--dry-run 이므로 DB를 수정하지 않습니다.")
        return 0

    # initialize_database()는 settings를 보므로 --db를 반영한 뒤 스키마를 맞춘다.
    settings.DATABASE_PATH = str(args.db)
    initialize_database()
    connection = sqlite3.connect(args.db, timeout=10)
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        with connection:  # 한 트랜잭션으로 처리해 실패 시 전부 되돌린다.
            inserted, conflicts = insert_retrospectives(connection, retrospective_rows)
            user_inserted, user_skipped = insert_users(connection, user_rows)
            largest = sync_autoincrement(connection)
    finally:
        connection.close()

    print(f"회고 {inserted}행 추가, 이미 있던 id {len(conflicts)}행 건너뜀")
    if conflicts:
        print(f"  건너뛴 id: {sorted(conflicts)[:20]}{' ...' if len(conflicts) > 20 else ''}")
    if user_rows:
        print(f"명단 {user_inserted}행 추가, 이미 있던 {user_skipped}행 건너뜀")
    print(f"다음 회고 id는 {largest + 1}부터 발급됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
