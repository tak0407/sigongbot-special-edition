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
