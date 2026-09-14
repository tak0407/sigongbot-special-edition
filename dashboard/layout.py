"""사이드탭 공통 레이아웃."""

from html import escape

from dashboard.common import REFRESH_SECONDS
from dashboard.auth import AUTH_CONTEXT, csrf_token

# /ai-jobs는 이미지 AI 리뷰가 내려가 있는 동안 탭에서 뺀다. 큐에 작업이 들어올
# 경로가 없어 빈 화면만 보이고, 재시도를 눌러도 가져갈 작업자가 없다.
# 기능을 되살릴 때 이 줄과 main.py의 작업자 기동을 함께 복구한다.
NAV = [
    ("/", "대시보드", "이번 회차 제출 현황"),
    ("/retrospectives", "회고 열람", "제출된 회고 본문"),
    ("/guided", "진행 중 회고", "질문형 회고 이탈 추적"),
    ("/schedule", "회차 일정", "마감일과 남은 회차"),
    ("/attendance", "온라인 모임 출석", "회차별 출석 현황"),
    ("/members", "멤버", "참여 이력과 이탈 징후"),
]

STYLE = """
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;color:#202124;background:#fff}
.shell{display:flex;align-items:flex-start;min-height:100vh}
.side{flex:0 0 210px;background:#f7f8fa;border-right:1px solid #e4e7ec;padding:20px 0;min-height:100vh}
.brand{font-weight:700;font-size:15px;padding:0 18px 14px}
.side a{display:block;padding:9px 18px;color:#344054;text-decoration:none;font-size:14px;border-left:3px solid transparent}
.side a:hover{background:#eef0f4}
.side a.on{background:#e8edfb;border-left-color:#2b5ce6;color:#1b3fa8;font-weight:600}
.side .hint{display:block;font-size:11px;color:#98a2b3;font-weight:400;margin-top:2px}
main{flex:1;min-width:0;padding:28px 32px 60px;max-width:1100px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:22px;margin:0}
h2{margin-top:32px;font-size:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:22px 0}
.card{background:#f5f7fa;border-radius:12px;padding:18px}
.card.alert{background:#fdecea}
.number{font-size:26px;font-weight:700;margin-top:8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e4e7ec;vertical-align:top}
th{font-size:12px;color:#667085;font-weight:600}
small{color:#667085}
a{color:#2b5ce6}
.mentions{font-family:ui-monospace,monospace;font-size:12px;color:#b42318;word-break:break-all;margin-top:4px}
.sub{font-family:ui-monospace,monospace;font-size:11px;color:#98a2b3;margin-top:2px}
.done{color:#087443}
.bar{width:180px}
.bar span{display:block;height:10px;border-radius:5px;background:#4c6ef5;min-width:2px}
.dots{display:inline-flex;gap:3px}
.dot{width:9px;height:9px;border-radius:50%;background:#4c6ef5;display:inline-block}
.dot.miss{background:#e4e7ec}
.error{font-family:ui-monospace,monospace;font-size:12px;color:#b42318;white-space:pre-wrap;word-break:break-all}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eef0f4;color:#344054}
.pill.pending{background:#fff5e6;color:#93500b}
.pill.processing{background:#e8edfb;color:#1b3fa8}
.pill.completed{background:#e7f6ec;color:#087443}
.pill.failed{background:#fdecea;color:#b42318}
.pill.now{background:#e8edfb;color:#1b3fa8;font-weight:600}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}
select,button{font:inherit;padding:6px 10px;border:1px solid #d0d5dd;border-radius:8px;background:#fff}
button{cursor:pointer}
button.retry{border-color:#2b5ce6;color:#2b5ce6}
form.inline{display:flex;gap:6px;align-items:center}
form.inline input{font:inherit;padding:5px 8px;border:1px solid #d0d5dd;border-radius:8px}
form.inline button{padding:5px 9px;font-size:13px}
.account{padding:18px;border-top:1px solid #e4e7ec;margin-top:14px;font-size:12px;color:#667085}
.account form{margin-top:8px}.account button{font-size:12px;padding:5px 9px}
.pager{display:flex;gap:10px;align-items:center;margin-top:16px;font-size:14px}
.body-field{margin:14px 0}
.body-field .label{font-size:12px;color:#667085;margin-bottom:4px}
.body-field .text{white-space:pre-wrap;background:#f7f8fa;border-radius:8px;padding:12px}
.warn{background:#fff5e6;border:1px solid #f5c77e;border-radius:10px;padding:14px;margin:18px 0}
@media(max-width:760px){.shell{display:block}.side{min-height:0;border-right:0;border-bottom:1px solid #e4e7ec}main{padding:20px 16px 40px}}
"""


def _nav(active: str) -> str:
    links = []
    for path, label, hint in NAV:
        cls = ' class="on"' if path == active else ""
        links.append(
            f'<a href="{path}"{cls}>{escape(label)}'
            f'<span class="hint">{escape(hint)}</span></a>'
        )
    return "".join(links)


def render(
    *,
    title: str,
    active: str,
    heading: str,
    subtitle: str,
    body: str,
    refresh: bool = False,
    request=None,
) -> str:
    meta_refresh = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if refresh else ""
    )
    account = ""
    if request is not None and request.get(AUTH_CONTEXT):
        account = (
            f'<div class="account">{escape(request[AUTH_CONTEXT].username)}'
            '<form method="post" action="/logout">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">'
            '<button type="submit">로그아웃</button></form></div>'
        )
    return f"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}
<title>{escape(title)}</title>
<style>{STYLE}</style>
<body><div class="shell">
<nav class="side"><div class="brand">시공삶 관리자</div>{_nav(active)}{account}</nav>
<main><header><h1>{escape(heading)}</h1><small>{subtitle}</small></header>
{body}
</main></div></body></html>"""
