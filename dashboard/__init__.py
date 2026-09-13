"""관리자 웹. /admin 아래 사이드탭으로 구성된다."""

from aiohttp import web

from dashboard.directory import SLACK_CLIENT


def register_dashboard_routes(app: web.Application, slack_client=None) -> None:
    """slack_client를 넘기지 않으면 사용자/채널을 ID 그대로 표시한다."""
    from dashboard import ai_jobs, guided, overview, retrospectives, schedule

    app[SLACK_CLIENT] = slack_client
    app.router.add_get("/admin", overview.handle)
    app.router.add_get("/admin/retrospectives", retrospectives.handle_list)
    app.router.add_get("/admin/retrospectives/{retrospective_id}", retrospectives.handle_detail)
    app.router.add_get("/admin/ai-jobs", ai_jobs.handle_list)
    app.router.add_get("/admin/ai-jobs/{job_id}", ai_jobs.handle_detail)
    app.router.add_post("/admin/ai-jobs/{job_id}/retry", ai_jobs.handle_retry)
    app.router.add_get("/admin/guided", guided.handle)
    app.router.add_get("/admin/schedule", schedule.handle)
