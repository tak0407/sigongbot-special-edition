"""투표·시간 확정과 공유 Slack 메시지 갱신을 각각 직렬화한다."""

import asyncio

_poll_locks: dict[int, asyncio.Lock] = {}
MESSAGE_LOCK = asyncio.Lock()


def poll_lock(poll_id: int) -> asyncio.Lock:
    """Google 호출을 기다리는 한 조가 다른 조의 응답을 막지 않게 한다."""
    return _poll_locks.setdefault(poll_id, asyncio.Lock())
