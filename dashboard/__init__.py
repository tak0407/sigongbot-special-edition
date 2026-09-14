"""관리자 웹. 전용 호스트의 루트에 사이드탭으로 구성된다."""

from aiohttp import web

from dashboard.auth import LEGACY_PREFIX, safe_internal_path
from dashboard.directory import SLACK_CLIENT


async def _redirect_legacy_admin(request: web.Request) -> web.StreamResponse:
    """예전 /admin 북마크를 루트 기준 경로로 넘겨준다."""
    # //evil.example.com 같은 프로토콜 상대 주소로 새어 나가지 않게 한 번 걸러 낸다.
    target = safe_internal_path("/" + request.match_info.get("tail", ""))
    if request.query_string:
        target = f"{target}?{request.query_string}"
    raise web.HTTPFound(target)


def register_dashboard_routes(app: web.Application, slack_client=None) -> None:
    """slack_client를 넘기지 않으면 사용자/채널을 ID 그대로 표시한다."""
    from dashboard import (
        ai_jobs,
        attendance,
        auth,
        guided,
        members,
        overview,
        retrospectives,
        schedule,
    )

    app[SLACK_CLIENT] = slack_client
    app.router.add_get("/login", auth.handle_login_form)
    app.router.add_post("/login", auth.handle_login)
    app.router.add_post("/logout", auth.handle_logout)
    app.router.add_get("/", overview.handle)
    app.router.add_get("/retrospectives", retrospectives.handle_list)
    app.router.add_get("/retrospectives/{retrospective_id}", retrospectives.handle_detail)
    app.router.add_get("/ai-jobs", ai_jobs.handle_list)
    app.router.add_get("/ai-jobs/{job_id}", ai_jobs.handle_detail)
    app.router.add_post("/ai-jobs/{job_id}/retry", ai_jobs.handle_retry)
    app.router.add_get("/guided", guided.handle)
    app.router.add_get("/schedule", schedule.handle)
    app.router.add_post("/schedule/add", schedule.handle_add)
    app.router.add_post("/schedule/due", schedule.handle_update_due)
    app.router.add_get("/attendance", attendance.handle)
    app.router.add_get("/members", members.handle)

    # 하위 호환: 화면 링크와 로그인 리다이렉트에는 쓰지 않고 이동만 시킨다.
    app.router.add_get(LEGACY_PREFIX, _redirect_legacy_admin)
    app.router.add_get(LEGACY_PREFIX + "/{tail:.*}", _redirect_legacy_admin)
