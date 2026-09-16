import datetime
from zoneinfo import ZoneInfo

MAX_PASS_COUNT = 2

SIXTH_FIRST_SESSION_START = datetime.datetime(
    2026, 9, 11, 19, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")
)

# 고정된 마감일 목록
DUE_DATES = [
    datetime.datetime(
        2025, 5, 1, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")
    ),  # 준비회차 (한국시간 05:00)
    datetime.datetime(2025, 5, 13, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 0회차
    datetime.datetime(2025, 5, 20, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 1회차
    datetime.datetime(2025, 5, 27, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 2회차
    datetime.datetime(2025, 6, 3, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3회차
    datetime.datetime(2025, 6, 10, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4회차
    datetime.datetime(2025, 6, 17, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5회차
    datetime.datetime(2025, 6, 24, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6회차
    datetime.datetime(2025, 7, 1, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 7회차
    datetime.datetime(2025, 7, 8, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 8회차
    datetime.datetime(2025, 7, 15, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 9회차
    datetime.datetime(2025, 7, 22, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 10회차
    datetime.datetime(2025, 7, 29, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 11회차
    datetime.datetime(2025, 8, 5, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 12회차
    datetime.datetime(2025, 8, 12, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 추가1회차
    datetime.datetime(2025, 8, 19, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 추가2회차
    datetime.datetime(2025, 8, 26, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 추가3회차
    datetime.datetime(2025, 9, 2, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 추가4회차
    datetime.datetime(2025, 9, 9, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 1회차
    datetime.datetime(2025, 9, 16, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 2회차
    datetime.datetime(2025, 9, 23, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 3회차
    datetime.datetime(2025, 9, 30, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 4회차
    datetime.datetime(2025, 10, 7, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 5회차
    datetime.datetime(2025, 10, 14, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 6회차
    datetime.datetime(2025, 10, 21, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 7회차
    datetime.datetime(2025, 10, 28, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 8회차
    datetime.datetime(2025, 11, 4, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 9회차
    datetime.datetime(2025, 11, 11, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 10회차
    datetime.datetime(2025, 11, 18, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 11회차
    datetime.datetime(2025, 11, 25, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 12회차
    datetime.datetime(2025, 12, 2, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 추가1회차
    datetime.datetime(2025, 12, 9, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 추가2회차
    datetime.datetime(2025, 12, 16, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 추가3회차
    datetime.datetime(2025, 12, 23, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 3기 추가4회차
    datetime.datetime(2026, 1, 13, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 1회차
    datetime.datetime(2026, 1, 20, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 2회차
    datetime.datetime(2026, 1, 27, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 3회차
    datetime.datetime(2026, 2, 3, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 4회차
    datetime.datetime(2026, 2, 10, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 5회차
    datetime.datetime(2026, 2, 17, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 6회차
    datetime.datetime(2026, 2, 24, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 7회차
    datetime.datetime(2026, 3, 3, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 8회차
    datetime.datetime(2026, 3, 10, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 9회차
    datetime.datetime(2026, 3, 17, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 10회차
    datetime.datetime(2026, 3, 24, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 11회차
    datetime.datetime(2026, 4, 7, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 12회차
    datetime.datetime(2026, 4, 14, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 추가1회차
    datetime.datetime(2026, 4, 21, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 추가2회차
    datetime.datetime(2026, 4, 28, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 4기 추가3회차
    datetime.datetime(2026, 5, 12, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 0회차
    datetime.datetime(2026, 5, 19, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 1회차
    datetime.datetime(2026, 5, 26, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 2회차
    datetime.datetime(2026, 6, 2, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 3회차
    datetime.datetime(2026, 6, 9, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 4회차
    datetime.datetime(2026, 6, 16, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 5회차
    datetime.datetime(2026, 6, 23, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 6회차
    datetime.datetime(2026, 6, 30, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 7회차
    datetime.datetime(2026, 7, 7, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 8회차
    datetime.datetime(2026, 7, 14, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 9회차
    datetime.datetime(2026, 7, 21, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 10회차
    datetime.datetime(2026, 7, 28, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 11회차
    datetime.datetime(2026, 8, 4, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 12회차
    datetime.datetime(2026, 8, 11, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 추가1회차
    datetime.datetime(2026, 8, 18, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 추가2회차
    datetime.datetime(2026, 8, 25, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 5기 추가3회차
    datetime.datetime(2026, 9, 15, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 1회차
    datetime.datetime(2026, 9, 22, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 2회차
    datetime.datetime(2026, 9, 29, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 3회차
    datetime.datetime(2026, 10, 6, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 4회차
    datetime.datetime(2026, 10, 13, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 5회차
    datetime.datetime(2026, 10, 20, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 6회차
    datetime.datetime(2026, 10, 27, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 7회차
    datetime.datetime(2026, 11, 3, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 8회차
    datetime.datetime(2026, 11, 10, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 9회차
    datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 10회차
    datetime.datetime(2026, 11, 24, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 11회차
    datetime.datetime(2026, 12, 1, 5, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),  # 6기 12회차
]

# 각 마감일의 설명
# 세션 표기가 같으면 이미 제출한 것으로 처리하므로 겹치지 않게 주의할 것.
SESSION_NAMES = [
    "준비회차",
    "0회차",
    "1회차",
    "2회차",
    "3회차",
    "4회차",
    "5회차",
    "6회차",
    "7회차",
    "8회차",
    "9회차",
    "10회차",
    "11회차",
    "12회차",
    "추가1회차",
    "추가2회차",
    "추가3회차",
    "추가4회차",
    "3기 1회차",
    "3기 2회차",
    "3기 3회차",
    "3기 4회차",
    "3기 5회차",
    "3기 6회차",
    "3기 7회차",
    "3기 8회차",
    "3기 9회차",
    "3기 10회차",
    "3기 11회차",
    "3기 12회차",
    "3기 추가1회차",
    "3기 추가2회차",
    "3기 추가3회차",
    "3기 추가4회차",
    "4기 1회차",
    "4기 2회차",
    "4기 3회차",
    "4기 4회차",
    "4기 5회차",
    "4기 6회차",
    "4기 7회차",
    "4기 8회차",
    "4기 9회차",
    "4기 10회차",
    "4기 11회차",
    "4기 12회차",
    "4기 추가1회차",
    "4기 추가2회차",
    "4기 추가3회차",
    "5기 0회차",
    "5기 1회차",
    "5기 2회차",
    "5기 3회차",
    "5기 4회차",
    "5기 5회차",
    "5기 6회차",
    "5기 7회차",
    "5기 8회차",
    "5기 9회차",
    "5기 10회차",
    "5기 11회차",
    "5기 12회차",
    "5기 추가1회차",
    "5기 추가2회차",
    "5기 추가3회차",
    "6기 1회차",
    "6기 2회차",
    "6기 3회차",
    "6기 4회차",
    "6기 5회차",
    "6기 6회차",
    "6기 7회차",
    "6기 8회차",
    "6기 9회차",
    "6기 10회차",
    "6기 11회차",
    "6기 12회차",
]

# 회차 목록 뒤에 새 회차를 붙여도 깨지지 않도록 위치(DUE_DATES[-1] 등)가 아니라
# 이름으로 앵커를 잡는다.
SIXTH_FIRST_SESSION_NAME = "6기 1회차"
_SIXTH_FIRST_INDEX = SESSION_NAMES.index(SIXTH_FIRST_SESSION_NAME)
SIXTH_FIRST_SESSION_DUE = DUE_DATES[_SIXTH_FIRST_INDEX]
# 5기 마지막 마감. 이 시점부터 6기 시작 전까지는 진행 중인 회차가 없다.
SIXTH_FIRST_PREVIOUS_DUE = DUE_DATES[_SIXTH_FIRST_INDEX - 1]

# 제출 공지를 마감 며칠 전에 띄울지. 6기 1회차(금 19:00 공지 → 화 05:00 마감)에서
# 실제로 쓰던 간격이고, 회차를 추가할 때 공지 시각의 기본값으로 쓴다.
ANNOUNCE_LEAD = datetime.timedelta(days=3, hours=10)

# 매회차 제출 공지의 기본 문구. 마이그레이션 10의 시드 원본이고, 이후 편집은
# 관리자 웹에서 한다. `{회차}`와 `{마감}`은 발송 시점에 치환된다.
DEFAULT_SUBMISSION_ANNOUNCEMENT = (
    "<!here>\n\n"
    "*{회차} 회고를 제출해 주세요* 🌱\n"
    "아래 버튼에서 회고를 작성하면 본인이 속한 팀 채널에 자동으로 공유됩니다.\n"
    "마감은 {마감}입니다. 이번 주를 살아낸 나에게 다음 한 주를 위한 힌트를 남겨주세요."
)
