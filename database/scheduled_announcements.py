import asyncio

from database.sqlite import get_connection


async def announcement_sent(announcement_key: str) -> bool:
    def select() -> bool:
        with get_connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM scheduled_announcements WHERE announcement_key = ?",
                    (announcement_key,),
                ).fetchone()
                is not None
            )

    return await asyncio.to_thread(select)


async def mark_announcement_sent(announcement_key: str) -> None:
    def insert() -> None:
        with get_connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO scheduled_announcements (announcement_key) VALUES (?)",
                (announcement_key,),
            )

    await asyncio.to_thread(insert)


async def sent_keys(prefix: str) -> list[str]:
    """prefix로 시작하는 발송 기록 키. LIKE 와일드카드에 걸리지 않게 앞부분을 그대로 비교한다."""

    def select() -> list[str]:
        with get_connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT announcement_key FROM scheduled_announcements"
                    " WHERE substr(announcement_key, 1, ?) = ?",
                    (len(prefix), prefix),
                )
            ]

    return await asyncio.to_thread(select)
