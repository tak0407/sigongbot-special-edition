"""백그라운드 작업이 조용히 죽지 않게 감시한다."""

import asyncio
from typing import Awaitable, Callable

from loguru import logger

RESTART_DELAY_SECONDS = 30


async def supervise(
    name: str,
    start: Callable[[], Awaitable[None]],
    *,
    restart_delay: float = RESTART_DELAY_SECONDS,
) -> None:
    """작업이 끝나거나 예외로 죽으면 ERROR 로그를 남기고 다시 시작한다.

    공지 스케줄러나 AI 리뷰 작업자가 사라지면 봇은 살아 있는 채로 기능만 멈춘다.
    ERROR 로그가 알림 싱크를 태우므로 알림 채널에서 바로 알 수 있다.
    """
    while True:
        try:
            await start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "{} 작업이 중단되어 {}초 후 다시 시작합니다.", name, restart_delay
            )
        else:
            logger.error(
                "{} 작업이 예기치 않게 끝나 {}초 후 다시 시작합니다.", name, restart_delay
            )
        await asyncio.sleep(restart_delay)
