import asyncio
from aiohttp import web
from loguru import logger
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from config import settings
from ai_review.antigravity import validate_antigravity_runtime
from dashboard import register_dashboard_routes
from database.sqlite import initialize_database
from slack.event_handler import app as slack_app
from slack.events.test_announcement import run_sixth_first_announcement_scheduler

async def health_check(request):
    return web.Response(text="OK", status=200)


async def main():
    initialize_database()

    try:
        executable = validate_antigravity_runtime()
        logger.info("질문형 회고 AI CLI 준비 완료 - executable={}", executable)
    except RuntimeError as error:
        logger.warning("질문형 회고 AI 런타임을 사용할 수 없습니다 - {}", error)

    # HTTP 서버 설정
    app = web.Application()
    app.router.add_get("/", health_check)
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
    
    announcement_task = asyncio.create_task(
        run_sixth_first_announcement_scheduler(slack_app.client)
    ) if settings.ENV == "prod" else None

    try:
        # HTTP 서버 시작
        await site.start()
        logger.info("Health check server started on port 8000")
        
        # Slack 연결 시작
        await handler.start_async()
        logger.info("Slack Socket Mode started")
        
    finally:
        tasks = []
        if announcement_task:
            announcement_task.cancel()
            tasks.append(announcement_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await handler.close_async()
        await runner.cleanup()
        logger.info("서버가 종료되었습니다.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n프로그램을 종료합니다...")
