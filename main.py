import asyncio
from aiohttp import web
from loguru import logger
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from config import settings
from database.sqlite import initialize_database
from ai_review import run_ai_review_worker
from slack.event_handler import app as slack_app

async def health_check(request):
    return web.Response(text="OK", status=200)


async def main():
    initialize_database()

    # HTTP 서버 설정
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    
    # Slack 핸들러 설정
    handler = AsyncSocketModeHandler(
        app=slack_app,
        app_token=settings.SLACK_APP_TOKEN,
    )
    
    worker_task = asyncio.create_task(run_ai_review_worker(slack_app.client))

    try:
        # HTTP 서버 시작
        await site.start()
        logger.info("Health check server started on port 8000")
        
        # Slack 연결 시작
        await handler.start_async()
        logger.info("Slack Socket Mode started")
        
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await handler.close_async()
        await runner.cleanup()
        logger.info("서버가 종료되었습니다.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n프로그램을 종료합니다...")
