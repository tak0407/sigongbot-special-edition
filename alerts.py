"""운영 중 발생한 오류를 알림 워크스페이스로 보낸다.

`.env`의 `SUBMISSION_TEAMS`가 깨져 설정 로딩 자체가 실패하는 부팅 오류까지 알리려면
이 모듈이 `config`보다 먼저, 그리고 `config` 없이도 import되어야 한다.
그래서 설정과 시각 헬퍼를 모듈 수준에서 가져오지 않고 전송 시점에 지연 로딩한다.
"""

import asyncio
import datetime
import json
import logging
import os
import threading
import time
import traceback as traceback_module
import urllib.error
import urllib.request
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from loguru import logger

ALERT_TIMEOUT_SECONDS = 10
# 같은 오류가 반복될 때 알림이 쏟아지지 않도록 억제하는 간격이다.
ALERT_DEDUP_SECONDS = 600
# 서로 다른 오류가 한꺼번에 터져도 이 창 안에서는 정해진 건수만 보낸다.
ALERT_FLOOD_WINDOW_SECONDS = 600
ALERT_FLOOD_LIMIT = 12
# 알림 채널이 읽을 수 있도록 오류 본문과 트레이스백 길이를 제한한다.
MAX_ERROR_CHARS = 500
MAX_DETAIL_CHARS = 1200
FLOOD_NOTICE = f"\n\n(알림이 몰려 {ALERT_FLOOD_WINDOW_SECONDS // 60}분 동안 추가 알림을 억제합니다.)"

_throttle_lock = threading.Lock()
_last_sent_at: dict[str, float] = {}
_window_started_at = 0.0
_window_count = 0
_pending_tasks: set[asyncio.Task] = set()
_sink_id: int | None = None


def _setting(name: str, default: str = "") -> str:
    """설정을 읽되, 설정 로딩이 실패한 상황에서는 환경변수로 되돌아간다."""
    try:
        from config import settings

        return str(getattr(settings, name, "") or "")
    except Exception:
        return os.getenv(name, default).strip()


def _env() -> str:
    return _setting("ENV", "dev") or "dev"


def _now_text() -> str:
    return datetime.datetime.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")


def _describe_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    return text[:MAX_ERROR_CHARS]


def build_alert(
    title: str,
    fields: Mapping[str, Any] | None = None,
    *,
    error: BaseException | str | None = None,
    detail: str | None = None,
    icon: str = "🚨",
) -> str:
    """알림 워크스페이스는 봇 워크스페이스와 다를 수 있으므로
    멘션이나 채널 링크 대신 원본 ID를 그대로 적는다."""
    lines = [f"{icon} {title}", f"• 환경: {_env()}", f"• 시각: {_now_text()}"]
    for label, value in (fields or {}).items():
        if value is None or value == "":
            continue
        lines.append(f"• {label}: {value}")
    if error is not None:
        lines.append(f"• 오류: {_describe_error(error)}")
    message = "\n".join(lines)
    if detail:
        message += "\n```" + detail.strip()[-MAX_DETAIL_CHARS:] + "```"
    return message


def build_ai_review_failure_alert(job: dict, error: str) -> str:
    return build_alert(
        "질문형 회고 AI 처리 실패",
        {
            "Job": f"{job['id']} (시도 {job['attempts']}회)",
            "사용자": job["user_id"],
            "채널": job["slack_channel"],
            "메시지 ts": job["slack_ts"],
        },
        error=error,
    )


def build_startup_alert(problems: list[str]) -> str:
    if not problems:
        return build_alert("시공봇이 시작되었습니다.", icon="✅")
    return build_alert(
        "시공봇이 시작됐지만 확인이 필요한 설정이 있습니다.",
        icon="⚠️",
        detail="\n".join(f"- {problem}" for problem in problems),
    )


def _reserve(dedup_key: str | None) -> str | None:
    """전송을 허용하면 메시지 뒤에 붙일 안내를, 억제하면 None을 반환한다."""
    global _window_started_at, _window_count

    now = time.monotonic()
    with _throttle_lock:
        if dedup_key is not None:
            last_sent = _last_sent_at.get(dedup_key)
            if last_sent is not None and now - last_sent < ALERT_DEDUP_SECONDS:
                return None

        if now - _window_started_at >= ALERT_FLOOD_WINDOW_SECONDS:
            _window_started_at = now
            _window_count = 0
        if _window_count >= ALERT_FLOOD_LIMIT:
            return None
        _window_count += 1

        if dedup_key is not None:
            _last_sent_at[dedup_key] = now
            for key, sent_at in list(_last_sent_at.items()):
                if now - sent_at >= ALERT_DEDUP_SECONDS:
                    del _last_sent_at[key]

        return FLOOD_NOTICE if _window_count == ALERT_FLOOD_LIMIT else ""


def reset_alert_throttle() -> None:
    """테스트에서 억제 상태를 초기화한다."""
    global _window_started_at, _window_count

    with _throttle_lock:
        _last_sent_at.clear()
        _window_started_at = 0.0
        _window_count = 0


def _post_webhook(url: str, text: str) -> bool:
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=ALERT_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").strip()
        status = error.code
    except Exception as error:
        # 알림 전송 실패가 다시 알림을 부르지 않도록 ERROR 미만으로만 남긴다.
        logger.bind(alert=False).warning("알림 전송 중 오류 - {}", error)
        return False

    if status != 200 or body != "ok":
        logger.bind(alert=False).warning(
            "알림 전송 실패 - status={} body={}", status, body[:200]
        )
        return False
    return True


def send_alert_blocking(text: str, *, dedup_key: str | None = None) -> bool:
    """이벤트 루프가 없는 부팅·종료 구간에서 알림을 보낸다.

    오류 처리 경로에서 호출하므로 어떤 경우에도 예외를 올리지 않는다.
    """
    url = _setting("ALERT_WEBHOOK_URL")
    if not url:
        logger.bind(alert=False).debug("ALERT_WEBHOOK_URL이 없어 알림 전송을 건너뜁니다.")
        return False

    notice = _reserve(dedup_key)
    if notice is None:
        return False

    try:
        return _post_webhook(url, text + notice)
    except Exception as error:
        logger.bind(alert=False).warning("알림 전송 중 오류 - {}", error)
        return False


async def send_alert(text: str, *, dedup_key: str | None = None) -> bool:
    """운영 알림 워크스페이스로 메시지를 보낸다.

    오류 처리 경로에서 호출하므로 어떤 경우에도 예외를 올리지 않는다.
    """
    try:
        return await asyncio.to_thread(send_alert_blocking, text, dedup_key=dedup_key)
    except Exception as error:
        logger.bind(alert=False).warning("알림 전송 중 오류 - {}", error)
        return False


def dispatch_alert(text: str, *, dedup_key: str | None = None) -> None:
    """루프가 돌고 있으면 백그라운드로, 아니면 그 자리에서 알림을 보낸다."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        send_alert_blocking(text, dedup_key=dedup_key)
        return

    task = loop.create_task(send_alert(text, dedup_key=dedup_key))
    # 태스크가 가비지 컬렉션으로 사라지지 않도록 완료까지 참조를 유지한다.
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def _alert_sink(message: Any) -> None:
    """ERROR 이상으로 남긴 모든 로그를 알림으로 바꾼다."""
    record = message.record
    # 더 자세한 알림을 직접 보내는 자리에서는 logger.bind(alert=False)로 끈다.
    if not record["extra"].get("alert", True):
        return
    if record["name"] == __name__:
        return

    try:
        exception = record["exception"]
        detail = None
        if exception is not None:
            # loguru의 diagnose 출력은 지역 변수(회고 본문 등)를 담을 수 있으므로
            # 표준 트레이스백만 보낸다.
            detail = "".join(
                traceback_module.format_exception(
                    exception.type, exception.value, exception.traceback
                )
            )
        text = build_alert(
            "시공봇 오류",
            {
                "위치": f"{record['name']}:{record['line']} ({record['function']})",
                "내용": str(record["message"])[:MAX_ERROR_CHARS],
            },
            error=exception.value if exception is not None else None,
            detail=detail,
        )
        error_name = type(exception.value).__name__ if exception is not None else "-"
        dispatch_alert(
            text, dedup_key=f"log:{record['name']}:{record['line']}:{error_name}"
        )
    except Exception:
        # 알림을 만들다 실패해도 로깅 자체는 계속되어야 한다.
        pass


def install_error_alert_sink() -> None:
    """ERROR 이상 로그를 알림 채널로 보내는 loguru 싱크를 설치한다."""
    global _sink_id

    if _sink_id is not None:
        return
    _sink_id = logger.add(_alert_sink, level="ERROR", format="{message}")


def uninstall_error_alert_sink() -> None:
    """테스트에서 싱크를 떼어낸다."""
    global _sink_id

    if _sink_id is None:
        return
    logger.remove(_sink_id)
    _sink_id = None


class _StdlibAlertHandler(logging.Handler):
    """slack_bolt·aiohttp처럼 표준 logging을 쓰는 라이브러리 오류도 알린다."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail = None
            if record.exc_info:
                detail = "".join(traceback_module.format_exception(*record.exc_info))
            text = build_alert(
                "라이브러리 오류",
                {
                    "로거": record.name,
                    "위치": f"{record.module}:{record.lineno}",
                    "내용": record.getMessage()[:MAX_ERROR_CHARS],
                },
                error=record.exc_info[1] if record.exc_info else None,
                detail=detail,
            )
            dispatch_alert(text, dedup_key=f"stdlib:{record.name}:{record.lineno}")
        except Exception:
            pass


def install_stdlib_error_alerts() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, _StdlibAlertHandler) for handler in root.handlers):
        return
    # 핸들러가 하나도 없으면 logging이 stderr 폴백을 쓰는데,
    # 알림 핸들러만 붙이면 그 폴백이 사라져 컨테이너 로그에서 오류가 보이지 않는다.
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.addHandler(_StdlibAlertHandler(level=logging.ERROR))
