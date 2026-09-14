import asyncio

from loguru import logger

from alerts import (
    build_alert,
    build_startup_alert,
    install_error_alert_sink,
    install_stdlib_error_alerts,
    send_alert,
    send_alert_blocking,
)

# .env가 잘못돼 설정을 읽다가 죽는 부팅 오류도 알리려면 다른 import보다 먼저 설치해야 한다.
install_error_alert_sink()
install_stdlib_error_alerts()

try:
    from aiohttp import web
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

    from background import supervise
    from config import settings
    from ai_review.antigravity import validate_antigravity_runtime
    from dashboard import register_dashboard_routes
    from dashboard.auth import validate_dashboard_security
    from database.sqlite import initialize_database
    from slack.event_handler import app as slack_app
    from slack.events.test_announcement import run_sixth_first_announcement_scheduler
    from slack.events.online_retro_meeting import run_online_retro_meeting_scheduler
except Exception:
    logger.exception("시공봇 설정을 불러오지 못해 시작할 수 없습니다.")
    raise


async def health_check(request):
    return web.Response(text="OK", status=200)


def collect_startup_problems() -> list[str]:
    """운영에 필요한 설정이 빠졌는지 확인한다. 여기서 걸리는 값은 조용히 실패한다."""
    problems = []
    if not settings.SLACK_BOT_TOKEN or not settings.SLACK_APP_TOKEN:
        problems.append("SLACK_BOT_TOKEN 또는 SLACK_APP_TOKEN이 비어 있습니다.")
    if not settings.ADMIN_CHANNEL:
        problems.append("ADMIN_CHANNEL이 비어 있어 관리자 채널 알림을 보낼 수 없습니다.")
    if not settings.ANNOUNCEMENT_CHANNEL:
        problems.append("ANNOUNCEMENT_CHANNEL이 비어 있어 회차 공지를 보낼 수 없습니다.")
    if not settings.SUBMISSION_DESTINATIONS:
        problems.append("SUBMISSION_TEAMS가 비어 있어 회고를 게시할 팀 채널이 없습니다.")
    try:
        executable = validate_antigravity_runtime()
        logger.info("질문형 회고 AI CLI 준비 완료 - executable={}", executable)
    except RuntimeError as error:
        problems.append(f"질문형 회고 AI를 쓸 수 없습니다 - {error}")
    return problems


async def main():
    validate_dashboard_security()
    initialize_database()

    problems = collect_startup_problems()
    for problem in problems:
        logger.warning("시작 점검 - {}", problem)

    # HTTP 서버 설정
    app = web.Application()
    app.router.add_get("/health", health_check)
    register_dashboard_routes(app, slack_app.client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)

    # Slack 핸들러 설정
    handler = AsyncSocketModeHandler(
        app=slack_app,
        app_token=settings.SLACK_APP_TOKEN,
    )

    # 이미지 AI 리뷰 작업자는 a8bad42에서 의도적으로 내렸다. 품질 개선 전까지
    # 기동하지 않는다. 되살리려면 ai_review/worker.py와 enqueue_ai_review 호출을
    # 함께 복구해야 한다.

    # 작업이 죽으면 봇은 살아 있는 채로 기능만 멈추므로 supervise로 감싼다.
    announcement_task = asyncio.create_task(
        supervise(
            "회차 공지 스케줄러",
            lambda: run_sixth_first_announcement_scheduler(slack_app.client),
        )
    ) if settings.ENV == "prod" else None
    meeting_task = asyncio.create_task(
        supervise(
            "온라인 모임 스케줄러",
            lambda: run_online_retro_meeting_scheduler(slack_app.client),
        )
    ) if settings.ENV == "prod" and settings.ONLINE_RETRO_MEETINGS else None

    try:
        # HTTP 서버 시작
        await site.start()
        logger.info("Health check server started on port 8000")

        await send_alert(build_startup_alert(problems), dedup_key="lifecycle:start")

        # Slack 연결 시작
        await handler.start_async()
        logger.info("Slack Socket Mode started")

    finally:
        tasks = []
        if announcement_task:
            announcement_task.cancel()
            tasks.append(announcement_task)
        if meeting_task:
            meeting_task.cancel()
            tasks.append(meeting_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await handler.close_async()
        await runner.cleanup()
        # 종료가 취소로 시작되면 await는 곧바로 취소되므로 동기 전송으로 보낸다.
        send_alert_blocking(
            build_alert("시공봇이 종료되었습니다.", icon="ℹ️"),
            dedup_key="lifecycle:stop",
        )
        logger.info("서버가 종료되었습니다.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n프로그램을 종료합니다...")
    except Exception:
        logger.exception("시공봇이 비정상 종료되었습니다.")
        raise
