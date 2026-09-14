#!/usr/bin/env python3
"""SQLite 운영 DB의 온라인 백업과 검증 도구.

표준 라이브러리만 사용하므로 컨테이너 안에서도, 미니 PC 호스트에서도 실행된다.
운영 중 파일을 그대로 복사하면 WAL 때문에 깨진 사본이 생길 수 있으므로
SQLite 온라인 백업 API를 사용한다.

사용 예시:
    python scripts/backup_database.py create --output -            # gzip 스트림을 stdout으로
    python scripts/backup_database.py create --output backup.db.gz
    python scripts/backup_database.py verify backup.db.gz          # 백업 파일 검증
    python scripts/backup_database.py verify data/sigongbot.db     # 운영 DB 검증
"""

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

GZIP_MAGIC = b"\x1f\x8b"
TABLES = (
    "retrospectives",
    "guided_reflections",
    "ai_review_jobs",
    "scheduled_announcements",
    "users",
    "online_retro_attendance",
    "online_retro_time_polls",
    "online_retro_time_votes",
)


def default_source() -> str:
    return os.environ.get("DATABASE_PATH", "data/sigongbot.db")


def inspect(database_path: Path) -> dict:
    """무결성 검사와 테이블별 행 수를 읽기 전용으로 수집한다."""
    uri = f"file:{database_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
            if table in existing
        }
        migrations = (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if "schema_migrations" in existing
            else None
        )
    return {
        "path": str(database_path),
        "bytes": database_path.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "schema_version": migrations,
        "counts": counts,
    }


def create_backup(source: Path, output: str) -> dict:
    if not source.exists():
        raise SystemExit(f"원본 DB를 찾을 수 없습니다: {source}")

    with tempfile.TemporaryDirectory() as workspace:
        snapshot = Path(workspace) / "snapshot.db"
        source_uri = f"file:{source}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as origin:
            with sqlite3.connect(snapshot, timeout=30) as copy:
                origin.backup(copy)
        summary = inspect(snapshot)

        if summary["integrity_check"] != "ok":
            raise SystemExit(f"백업 사본 무결성 검사 실패: {summary['integrity_check']}")

        if output == "-":
            with gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", mtime=0) as archive:
                with snapshot.open("rb") as data:
                    shutil.copyfileobj(data, archive)
            sys.stdout.buffer.flush()
        else:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            with snapshot.open("rb") as data, gzip.open(partial, "wb") as archive:
                shutil.copyfileobj(data, archive)
            os.chmod(partial, 0o600)
            partial.replace(target)
            summary["archive"] = str(target)
            summary["archive_bytes"] = target.stat().st_size

    summary["source"] = str(source)
    summary["path"] = str(source)
    return summary


def verify(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"검증할 파일을 찾을 수 없습니다: {path}")
    with path.open("rb") as handle:
        compressed = handle.read(2) == GZIP_MAGIC
    if not compressed:
        return inspect(path)
    with tempfile.TemporaryDirectory() as workspace:
        extracted = Path(workspace) / "restored.db"
        with gzip.open(path, "rb") as archive, extracted.open("wb") as data:
            shutil.copyfileobj(archive, data)
        summary = inspect(extracted)
    summary["path"] = str(path)
    summary["compressed"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="온라인 백업 생성")
    create.add_argument("--source", default=default_source(), help="원본 DB 경로")
    create.add_argument("--output", required=True, help="출력 경로 또는 '-' (stdout)")

    check = commands.add_parser("verify", help="백업 또는 운영 DB 검증")
    check.add_argument("path", nargs="?", default=default_source(), help="검증 대상 경로")

    arguments = parser.parse_args()
    if arguments.command == "create":
        summary = create_backup(Path(arguments.source).expanduser(), arguments.output)
    else:
        summary = verify(Path(arguments.path).expanduser())

    # stdout은 백업 스트림 전용이므로 요약은 항상 stderr로 보낸다.
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    return 0 if summary["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
