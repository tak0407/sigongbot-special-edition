"""이번 회차 회고에서 자주 나온 낱말.

형태소 분석기를 쓰지 않는다. 운영 이미지에 konlpy나 mecab을 넣으면 Java나 C
빌드 의존이 따라오는데, 이 화면 하나를 위해 감당할 무게가 아니다. 대신 조사와
어미를 잘라 내는 규칙으로 근사한다. 명사만 정확히 고르지는 못하므로 이 화면은
"이번 주 화제를 훑는 용도"이고, 집계 수치로 쓰지 않는다.

그림 대신 글자 크기로 보여 준다. 이미지로 그리려면 PIL과 한글 폰트가 필요한데
CSS 크기 조절만으로 같은 목적을 채운다.
"""

import re
from collections import Counter
from html import escape

# 회고에서 낱말을 모을 본문 칸. 감정 점수처럼 숫자인 칸은 넣지 않는다.
WORD_FIELDS = (
    "good_points",
    "improvements",
    "learnings",
    "action_item",
    "emotion_reason",
)

MAX_WORDS = 30
# 한 사람만 되풀이한 낱말은 회차의 화제가 아니라 그 사람의 사정이다. 개인의
# 사정이 이름표 없이 대시보드 맨 앞에 걸리지 않도록 두 명 이상만 남긴다.
MIN_WRITERS = 2
MIN_FONT, MAX_FONT = 13, 34

TOKEN = re.compile(r"[가-힣]+|[A-Za-z][A-Za-z0-9+#.]{1,}")

# 긴 조사부터 잘라야 "으로서"가 "으로"로 잘리지 않는다.
JOSA = (
    "으로써", "으로서", "이라고", "에서는", "에게는", "으로는", "이라는", "라는",
    "에서", "에게", "한테", "까지", "부터", "보다", "처럼", "마다", "이나", "라도",
    "으로", "들의", "들이", "들을",
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "도", "만", "로", "랑",
)

# 용언 활용형. 이 어미로 끝나면 명사가 아니라 서술어다.
#
# 끝 글자 하나로 판단하지 않는다. `서`만 보고 자르면 이력서와 독서가, `요`만
# 보고 자르면 필요와 내용이 함께 날아간다. 앞 글자까지 묶어 활용형일 때만 뺀다.
ENDING = re.compile(
    r"(했|었|았|였|겠|봤|왔|갔|났|줬|썼|졌|샀|탔|컸|셨|뒀)(다|고|어|지|나|는데)?$"
    r"|(하|되|이|같|싶|있|없|롭|스럽|좋|많|적|크|작|높|낮|길|쉽|바쁘|힘들)"
    r"(다|고|는|며|면|지|서|자|니)$"
    r"|(어서|아서|여서|라서|면서|는데)$"
    r"|다$"
)

# 한 글자라도 화제가 되는 명사. 이게 없으면 "일은"과 "일이"가 서로 다른
# 낱말로 따로 걸리고, 정작 "일"은 한 글자라 버려진다.
# `등`은 "회고, 운동 등"처럼 열거하는 말로 훨씬 자주 쓰여 넣지 않는다.
SHORT_NOUNS = frozenset("일 잠 책 글 몸 돈 집 밥 술 옷 길 눈 비 배 발 손 맘 삶 꿈 짐".split())

STOP = frozenset(
    """
    그리고 그래서 하지만 그러나 그런데 그러면 때문 그냥 조금 정말 진짜 너무 계속
    다시 아직 이번 다음 지난 이런 저런 그런 무엇 어떤 사람 부분 같은 같아 있는
    있어 없는 없어 하고 많이 약간 살짝 여러 매우 아주 거의 자주 항상 이제 오늘
    어제 내일 이번주 다음주 지난주 정도 경우 상황 나는 내가 나의 저는 제가 우리
    함께 대한 위해 통해 대해 관련 그것 이것 말고 하기 보고 남은 많은 좋은 싶은
    대신 조금씩 여기 거기 자체 이상 이하 이후 이전 나도 뭔가 당장 조금더 그때
    않고 가고 가서 먹고 오고 있고 없고 되고 하니 가니 오니 보니 되니 밀린 남긴
    가장 다른 전에 제대 아무 어느 무슨 다들 서로 좀더 하나 라서 오랜 어차피 역시
    """.split()
)


def normalize(raw: str) -> str:
    """낱말 하나에서 조사를 떼어 낸다.

    자르고 난 몸통이 한 글자면 `SHORT_NOUNS`에 있을 때만 자른다. 그러지 않으면
    "밀린"에서 "린"을 떼는 식으로 엉뚱한 말이 만들어진다.
    """
    word = raw.lower() if raw[0].isascii() else raw
    if len(word) < 2:
        return word
    for josa in JOSA:
        if not word.endswith(josa):
            continue
        stem = word[: -len(josa)]
        if len(stem) >= 2 or stem in SHORT_NOUNS:
            return stem
        break
    return word


def count_words(texts) -> list[dict]:
    """회고 본문들에서 (낱말, 쓴 사람 수, 나온 횟수)를 뽑는다.

    `texts`는 회고 한 건이 문자열 하나인 목록이다. 무게는 나온 횟수가 아니라
    몇 명이 썼는지로 잡는다. 한 사람이 같은 말을 열 번 되풀이해도 이번 주에
    그 이야기를 한 사람이 늘어난 건 아니기 때문이다.
    """
    writers: Counter = Counter()
    total: Counter = Counter()
    for text in texts:
        found = set()
        for raw in TOKEN.findall(text or ""):
            word = normalize(raw)
            if word in STOP or ENDING.search(word):
                continue
            if len(word) < 2 and word not in SHORT_NOUNS:
                continue
            total[word] += 1
            found.add(word)
        for word in found:
            writers[word] += 1

    shared = [
        {"word": word, "writers": count, "total": total[word]}
        for word, count in writers.items()
        if count >= MIN_WRITERS
    ]
    shared.sort(key=lambda row: (-row["writers"], -row["total"], row["word"]))
    return shared[:MAX_WORDS]


def _font(writers: int, low: int, high: int) -> int:
    if high == low:
        return MAX_FONT
    ratio = (writers - low) / (high - low)
    return round(MIN_FONT + (MAX_FONT - MIN_FONT) * ratio)


def render(words: list[dict]) -> str:
    if not words:
        return (
            "<p><small>두 명 이상이 함께 쓴 낱말이 아직 없습니다. "
            "회고가 더 쌓이면 나타납니다.</small></p>"
        )
    high = words[0]["writers"]
    low = words[-1]["writers"]
    items = []
    for index, row in enumerate(words):
        size = _font(row["writers"], low, high)
        # 위쪽 1/3은 진하게 둬서 크기만으로 구분하지 않게 한다.
        tone = " strong" if index < max(1, len(words) // 3) else ""
        label = f"{row['word']} · {row['writers']}명이 {row['total']}번"
        items.append(
            f'<li class="word{tone}" style="font-size:{size}px">'
            f'<span title="{escape(label)}">{escape(row["word"])}</span>'
            f'<small class="count">{row["writers"]}</small></li>'
        )
    return f'<ul class="cloud">{"".join(items)}</ul>'
