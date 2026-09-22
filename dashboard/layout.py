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
    ("/suggestions", "봇 개선 제안", "사용자 의견과 처리 상태"),
    ("/guided", "진행 중 회고", "질문형 회고 이탈 추적"),
    ("/schedule", "회차 일정", "마감일과 남은 회차"),
    ("/attendance", "온라인 모임 출석", "회차별 출석 현황"),
    ("/members", "멤버", "참여 이력과 이탈 징후"),
    ("/online-retro", "온라인 회고 확정", "시간 투표와 Meet 생성"),
]

STYLE = """
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;color:#202124;background:#fff}
.shell{display:flex;align-items:flex-start;min-height:100vh}
.side{flex:0 0 210px;background:#f7f8fa;border-right:1px solid #e4e7ec;padding:20px 0;position:sticky;top:0;align-self:flex-start;height:100vh;overflow-y:auto;overscroll-behavior:contain}
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
.pill.in_progress{background:#e8edfb;color:#1b3fa8}
.pill.completed{background:#e7f6ec;color:#087443}
.pill.failed{background:#fdecea;color:#b42318}
.pill.now{background:#e8edfb;color:#1b3fa8;font-weight:600}
tr.rest td{background:#f7f8fa;color:#667085;font-size:13px}
.field{margin-top:14px}
.field>small{display:block;margin-bottom:4px}
.actions{display:flex;gap:6px;flex-wrap:wrap}
button.danger{border-color:#b42318;color:#b42318}
button.primary{background:#2b5ce6;border-color:#2b5ce6;color:#fff}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
dialog{border:0;border-radius:12px;padding:18px 22px 22px;width:min(560px,calc(100% - 32px));box-shadow:0 12px 40px rgba(16,24,40,.25)}
dialog::backdrop{background:rgba(16,24,40,.45)}
.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
.dialog-head h2{margin:0}
.dialog-head button{border:0;background:none;font-size:18px;color:#667085}
button.link{border:0;background:none;padding:0;min-height:0;text-align:left;cursor:pointer}
dialog .body-field .text{max-height:220px;overflow:auto}
.cloud{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 16px;list-style:none;margin:14px 0;padding:14px;background:#f7f8fa;border-radius:12px}
.cloud .word{line-height:1.25;color:#667085}
.cloud .word.strong{color:#1b3fa8;font-weight:600}
.cloud .count{font-size:11px;color:#98a2b3;margin-left:3px;vertical-align:super}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}
select,button{font:inherit;padding:6px 10px;border:1px solid #d0d5dd;border-radius:8px;background:#fff}
textarea{font:inherit;width:100%;max-width:760px;padding:10px 12px;border:1px solid #d0d5dd;border-radius:8px;line-height:1.5;resize:vertical;margin-top:8px}
code{font-family:ui-monospace,monospace;font-size:12px;background:#f0f2f5;padding:1px 5px;border-radius:4px}
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

/* Keep wide lists scrollable without moving the entire page. */
.table-scroll{max-width:100%;overflow-x:auto;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;scrollbar-width:thin;scrollbar-color:#98a2b3 #f2f4f7}
.table-scroll table{min-width:600px}
.table-scroll th,.table-scroll td{overflow-wrap:anywhere}
.table-scroll td{max-width:360px}
.table-hint{display:none}
.detail-table{table-layout:fixed}
.detail-table th{width:100px}
body{overflow-wrap:anywhere}
input,select,textarea{min-width:0;max-width:100%}
input:not([type=hidden]){font:inherit;padding:6px 10px;border:1px solid #d0d5dd;border-radius:8px}
form.inline,.pager{flex-wrap:wrap}
:focus-visible{outline:3px solid #2b5ce6;outline-offset:3px}
.menu summary{display:none}
@media(max-width:760px){
 .shell{display:block}
 .side{position:static;height:auto;overflow:visible;border-right:0;border-bottom:1px solid #e4e7ec;padding:12px 16px}
 .brand{padding:0 0 8px}
 .menu summary{display:flex;align-items:center;min-height:44px;cursor:pointer;color:#1b3fa8;font-weight:600}
 .menu summary::before{content:"☰";margin-right:10px}
 .menu summary::after{content:"⌄";margin-left:auto;font-size:18px;transition:transform .15s ease}
 .menu[open] summary::after{transform:rotate(180deg)}
 .menu:not([open])>.menu-content{display:none}
 .menu-links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
 .side a{display:flex;align-items:center;padding:10px 8px;min-height:44px;background:#fff;border:1px solid #e4e7ec;border-left:3px solid transparent;border-radius:8px;line-height:1.3}
 .side a.on{border-color:#c7d2fe;border-left-color:#2b5ce6}
 .side .hint{display:none}
 .account{padding:12px 0 0;display:flex;align-items:center;justify-content:space-between;gap:12px}
 .account form{margin:0}
 main{padding:20px 16px 40px}
 header{display:block}
 header small{display:block;margin-top:6px;line-height:1.5}
 h2{margin-top:26px}
 .cards{grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
 .card{padding:14px;min-width:0}
 .number{font-size:22px;line-height:1.3;overflow-wrap:anywhere}
 .table-hint{display:block;margin:8px 0;font-size:12px;color:#667085}
 th,td{padding:10px 8px}
 .table-scroll th,.table-scroll td{min-width:90px}
 .table-scroll form.inline{min-width:250px}
 button,select,input:not([type=hidden]),textarea,form.inline input,form.inline button,.account button{font-size:16px;min-height:44px}
 .filters{align-items:stretch}
 .filters>input:not([type=hidden]),.filters>select{flex:1 1 100%;width:100%}
 .filters>button{flex:1 1 auto}
 form.inline{gap:8px}
 form.inline input{width:100%}
 .filters a,.pager a,main>p a{display:inline-flex;align-items:center;min-height:44px;padding:0 8px}
 .table-scroll a,.detail-table a{display:inline;min-height:0;padding:0}
 .pager{gap:8px}
 .body-field .text{overflow-wrap:anywhere}
}

/* Shared visual system for the admin console. */
:root{color-scheme:light;--ink:#182230;--muted:#667085;--line:#e4e7ec;--soft:#f8fafc;--page:#f3f6fb;--brand:#3157d5;--brand-dark:#2444b4;--nav:#101828;--nav-hover:#1d2939;--shadow:0 8px 24px #1822300a;--radius:14px}
body{font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--page)}
.side{display:flex;flex-direction:column;flex-basis:248px;background:var(--nav);color:#d0d5dd;border:0;padding:18px 0}
.brand{display:flex;align-items:center;gap:10px;padding:4px 18px 20px;color:#fff;font-size:15px}
.brand::before{content:"시";display:grid;place-items:center;width:32px;height:32px;flex:0 0 32px;border-radius:10px;background:var(--brand);color:#fff;font-size:14px;box-shadow:0 4px 12px #3157d544}
.menu-content{display:flex;min-height:calc(100vh - 82px);flex-direction:column}
.menu-links{display:grid;gap:4px;padding:0 10px}
.side a{padding:10px 12px;color:#d0d5dd;border:1px solid transparent;border-left:3px solid transparent;border-radius:10px;transition:background .15s ease,color .15s ease,border-color .15s ease}
.side a:hover{background:var(--nav-hover);color:#fff}
.side a.on{background:#1d2939;border-color:#344054;border-left-color:#8da8ff;color:#fff}
.side .hint{color:#98a2b3}
main{max-width:1480px;margin:0 auto;padding:36px clamp(24px,4vw,58px) 72px}
header{align-items:flex-end;padding-bottom:18px;margin-bottom:24px;border-bottom:1px solid #dfe4ec}
h1{font-size:27px;line-height:1.25;letter-spacing:-.025em;color:#101828}
header small{max-width:620px;text-align:right;line-height:1.5}
h2{margin:32px 0 14px;font-size:18px;line-height:1.35;letter-spacing:-.01em;color:#25324a}
main>section:not(.cards){margin:20px 0;padding:20px;background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
main>section:not(.cards) h2{margin-top:0}
.cards{grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:14px;margin:0 0 26px}
.card{min-height:112px;padding:18px 20px;background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);color:var(--muted);font-size:13px;font-weight:600;line-height:1.45}
.card.alert{background:#fff8f3;border-color:#fed7aa;color:#9a3412}
.number{font-size:28px;line-height:1.25;letter-spacing:-.025em;color:var(--ink)}
.card.alert .number{color:#9a3412}
table{border-collapse:separate;border-spacing:0;background:#fff}
th,td{padding:12px 14px;border-bottom-color:#edf0f4}
th{font-size:12px;line-height:1.4;color:#475467;font-weight:650;background:#f8fafc;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#f8fafc}
small{color:var(--muted)}
a{color:var(--brand-dark);text-underline-offset:3px}
main a:hover{color:#183894}
.done{font-weight:600}
.bar span{height:9px;border-radius:99px;background:linear-gradient(90deg,#5578ee,var(--brand))}
.dots{gap:4px}
.dot{background:#4f6fe2}
.dot.miss{background:#d0d5dd}
.pill{display:inline-flex;align-items:center;min-height:24px;padding:3px 9px;border-radius:999px;font-weight:600;white-space:nowrap}
.pill.pending{background:#fff2d8;color:#854d0e}
.pill.processing,.pill.in_progress,.pill.now{background:#eaf0ff;color:#2444b4}
.pill.completed{background:#e7f6ec;color:#087443}
.pill.failed{background:#feeceb;color:#b42318}
tr.rest td{background:#f2f4f7}
.manage summary,.section summary{color:var(--brand-dark);font-weight:650}
.section{margin-top:22px;padding:16px 18px;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
.section summary{font-size:15px;list-style:none}
.section summary::-webkit-details-marker{display:none}
.section summary::after{content:"＋";float:right;color:#667085;font-weight:400}
.section[open] summary::after{content:"−"}
button.danger{border-color:#f0b4b0;color:#b42318;background:#fff}
.toolbar{align-items:center;margin:18px 0}
dialog{border:1px solid var(--line);border-radius:16px;padding:22px 24px;background:#fff;color:var(--ink);box-shadow:0 20px 60px #10182830}
dialog::backdrop{background:#10182880;backdrop-filter:blur(2px)}
.dialog-head{margin-bottom:12px}
.dialog-head button{min-width:40px;border-radius:9px}
.cloud{gap:8px 16px;margin:14px 0 24px;padding:18px;background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.cloud .word.strong{color:#2444b4}
.filters{gap:10px}
select,button{min-height:40px;padding:8px 12px;border-color:#d0d5dd;border-radius:9px;color:var(--ink)}
textarea{padding:11px 13px;border-color:#d0d5dd;border-radius:10px;line-height:1.55;background:#fff;color:var(--ink)}
code{background:#eef1f5;padding:2px 6px;border-radius:5px;color:#344054}
button{font-weight:600;transition:background .15s ease,border-color .15s ease,color .15s ease,box-shadow .15s ease}
button:hover{border-color:#98a2b3;background:#f8fafc}
.filters>button,form:not(.inline)>button,button.primary{background:var(--brand);border-color:var(--brand);color:#fff;box-shadow:0 2px 5px #3157d522}
.filters>button:hover,form:not(.inline)>button:hover,button.primary:hover{background:var(--brand-dark);border-color:var(--brand-dark)}
button.retry{border-color:#b8c8ff;color:var(--brand-dark);background:#f5f7ff}
form.inline input{padding:7px 9px;border-color:#d0d5dd;border-radius:8px;background:#fff;color:var(--ink)}
form.inline button{padding:6px 10px}
.account{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:20px 10px 0;border-top-color:#344054;color:#d0d5dd}
.account form{margin:0}
.account button{min-height:36px;padding:6px 10px;border-color:#475467;background:transparent;color:#f2f4f7;box-shadow:none}
.account button:hover{background:#1d2939}
.pager a{display:inline-flex;align-items:center;min-height:40px;padding:0 12px;border:1px solid var(--line);border-radius:9px;background:#fff;text-decoration:none}
.pager a:hover{background:#eef2ff;border-color:#c7d2fe}
.body-field .label{font-weight:650;color:#667085}
.body-field .text{border:1px solid var(--line);border-radius:10px;background:#fff;padding:14px;line-height:1.6}
.warn{background:#fff8eb;border:1px solid #f4d28f;border-left:4px solid #d98a11;border-radius:11px;padding:14px 16px;color:#71430c;line-height:1.5}
.table-scroll{background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);margin:12px 0 22px}
.table-scroll table{min-width:640px}
.table-scroll tbody tr:hover td{background:#f6f8fc}
.detail-table{border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.detail-table th{width:112px}
.detail-table th,.detail-table td{padding:12px 14px}
@media(max-width:980px){.side{flex-basis:224px}main{padding:30px 24px 56px}}
@media(max-width:760px){
 .side{position:static;height:auto;overflow:visible;border:0;border-radius:0 0 16px 16px;padding:12px 16px 14px;box-shadow:0 6px 18px #10182816}
 .brand{padding:0 0 8px}
 .brand::before{width:30px;height:30px;flex-basis:30px}
 .menu-content{min-height:0}
 .menu summary{display:flex;align-items:center;min-height:44px;list-style:none;color:#e5edff;font-weight:650}
 .menu summary::-webkit-details-marker{display:none}
 .menu-links{gap:6px}
 .side a{display:flex;align-items:center;min-height:44px;padding:10px 8px;border-color:#344054;border-left-color:transparent;border-radius:9px;background:#18243a;line-height:1.3}
 .side a.on{border-color:#526da8;border-left-color:#8da8ff;background:#253858;color:#fff}
 .side .hint{display:none}
 .account{margin:12px 0 0;padding:12px 0 0;border-top-color:#344054}
 main{padding:24px 16px 44px}
 header{display:block;padding-bottom:15px;margin-bottom:20px}
 h1{font-size:24px}
 header small{display:block;margin-top:6px;text-align:left;line-height:1.5}
 h2{margin-top:27px;font-size:17px}
 .cards{grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
 .card{min-height:100px;padding:15px}
 .number{font-size:23px;line-height:1.3}
 main>section:not(.cards){margin:16px 0;padding:16px}
 .section{padding:14px}
 .table-hint{display:block;margin:8px 0;font-size:12px;color:#667085}
 th,td{padding:10px 9px}
 .table-scroll th,.table-scroll td{min-width:90px}
 .table-scroll form.inline{min-width:250px}
 button,select,input:not([type=hidden]),textarea,form.inline input,form.inline button,.account button{font-size:16px;min-height:44px}
 .filters{align-items:stretch}
 .filters>input:not([type=hidden]),.filters>select{flex:1 1 100%;width:100%}
 .filters>button{flex:1 1 auto}
 form.inline{gap:8px}
 form.inline input{width:100%}
 .filters a,main>p a{display:inline-flex;align-items:center;min-height:44px;padding:0 8px}
 .table-scroll a,.detail-table a{display:inline;min-height:0;padding:0}
 .pager a{min-height:44px}
 .pager{gap:8px}
 .body-field .text{overflow-wrap:anywhere}
}
@media(max-width:380px){main{padding-right:14px;padding-left:14px}.menu-links{gap:5px}.side a{font-size:13px}.card{padding:13px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition-duration:.01ms!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
"""


def _nav(active: str) -> str:
    links = []
    for path, label, hint in NAV:
        cls = ' class="on" aria-current="page"' if path == active else ""
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
<nav class="side" aria-label="관리자 메뉴"><div class="brand">시공삶 관리자</div>
<details class="menu" open><summary>메뉴 · {escape(next((label for path, label, _ in NAV if path == active), "관리자"))}</summary>
<div class="menu-content"><div class="menu-links">{_nav(active)}</div>{account}</div></details></nav>
<script>
(() => {{
 const menu = document.querySelector('.menu');
 const mobile = window.matchMedia('(max-width:760px)');
 const syncMenu = () => {{ menu.open = !mobile.matches; }};
 syncMenu();
 mobile.addEventListener('change', syncMenu);
}})();
</script>
<main><header><h1>{escape(heading)}</h1><small>{subtitle}</small></header>
{body}
</main></div></body></html>"""
