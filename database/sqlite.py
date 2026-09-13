import os
import sqlite3
from pathlib import Path

from config import settings
from database.migrations import run_migrations


def get_connection() -> sqlite3.Connection:
    database_path = Path(settings.DATABASE_PATH).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize_database(*, recover_processing_jobs: bool = True) -> None:
    database_path = Path(settings.DATABASE_PATH).expanduser()
    with get_connection() as connection:
        run_migrations(connection)
        if recover_processing_jobs:
            connection.execute(
                """
                UPDATE ai_review_jobs
                   SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                 WHERE status = 'processing'
                """
            )

    os.chmod(database_path.parent, 0o700)
    os.chmod(database_path, 0o600)
