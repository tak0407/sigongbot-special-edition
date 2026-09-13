"""이슈 #5 검토용 회고 이력 분석 스크립트.

Google Sheets에서 내려받은 `retrospectives` CSV를 정규화하고,
개인별 맥락 피드백의 구현 가능성 판단에 필요한 집계를 출력한다.

사용법:
    python scripts/analyze_retrospective_history.py <csv 경로>

시트 ID와 원문, 실명은 저장소에 두지 않는다. CSV는 내려받아 저장소 밖에 둔다.
CSV는 다음으로 받을 수 있다.
    curl -sL "https://docs.google.com/spreadsheets/d/<시트ID>/gviz/tq?tqx=out:csv&sheet=retrospectives" -o retrospectives.csv
"""

import csv
import math
import random
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

COLUMNS = [
    "id", "user_id", "good_points", "improvements", "learnings", "action_item",
    "emotion_score", "emotion_reason", "session_name", "slack_channel",
    "slack_ts", "created_at", "updated_at",
]
TEXT_FIELDS = ["good_points", "improvements", "learnings", "action_item"]
USER_ID = re.compile(r"^U[A-Z0-9]{6,}$")
CHARS_PER_TOKEN = 1.7  # 한국어 대략치


def load(path: str) -> list[dict[str, str]]:
    """두 가지 컬럼 배치가 섞인 export를 정규화한다.

    구버전 행은 2번째 칸에 실명이 있고, 신버전 행은 실명 칸이 없어 필드가
    한 칸씩 밀려 있다. 헤더만 믿고 파싱하면 user_id 자리에 본문이 들어간다.
    """
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.reader(handle))[1:]

    records = []
    for row in rows:
        if USER_ID.match(row[1].strip()):  # 실명 칸 없음 - 한 칸씩 밀림
            name, values = "", row[0:11] + [row[12], row[13]]
        else:  # 실명 칸 있음
            name, values = row[1], [row[0]] + row[2:14]
        record = dict(zip(COLUMNS, values))
        record["name"] = name
        records.append(record)
    return records


def cohort(record: dict[str, str]) -> str:
    match = re.match(r"^(\d)기", record["session_name"].strip())
    return match.group(1) + "기" if match else "무표기"


def body_length(record: dict[str, str]) -> int:
    return sum(len(record[field]) for field in TEXT_FIELDS)


def grams(text: str, size: int = 2) -> list[str]:
    text = re.sub(r"\s+", "", text)
    return [text[i:i + size] for i in range(len(text) - size + 1)]


def build_tfidf(histories: dict[str, list[dict[str, str]]]):
    documents = [
        Counter(grams(" ".join(r[f] for f in TEXT_FIELDS)))
        for rows in histories.values() for r in rows
    ]
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(set(document))
    total = len(documents)
    idf = {gram: math.log(total / (1 + freq)) for gram, freq in document_frequency.items()}

    def vectorize(record: dict[str, str]) -> dict[str, float]:
        counts = Counter(grams(" ".join(record[f] for f in TEXT_FIELDS)))
        weights = {g: (1 + math.log(c)) * idf.get(g, 0) for g, c in counts.items()}
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1
        return {g: w / norm for g, w in weights.items()}

    return vectorize


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(gram, 0) for gram, weight in left.items())


def report_overview(records: list[dict[str, str]]) -> None:
    print("[개요]")
    print(f"  행 {len(records)} / 사용자 {len({r['user_id'] for r in records})}")
    dates = sorted(r["created_at"][:10] for r in records)
    print(f"  기간 {dates[0]} ~ {dates[-1]} / 채널 {len({r['slack_channel'] for r in records})}")
    print(f"  기수 구간 {dict(Counter(cohort(r) for r in records))}")
    invalid = [r["id"] for r in records if not USER_ID.match(r["user_id"].strip())]
    print(f"  user_id 형식 오류 {len(invalid)}건")
    for field in ("emotion_score", "emotion_reason", "updated_at"):
        print(f"  {field} 결측 {sum(1 for r in records if not r[field].strip())}건")
    duplicates = Counter((r["user_id"], r["session_name"]) for r in records)
    print(f"  동일 사용자+회차 중복 {sum(v - 1 for v in duplicates.values() if v > 1)}행")


def report_identity(records: list[dict[str, str]]) -> None:
    print("\n[동일 사용자 식별]")
    named = [r for r in records if r["name"].strip()]
    name_to_id, id_to_name = defaultdict(set), defaultdict(set)
    for record in named:
        name_to_id[record["name"]].add(record["user_id"])
        id_to_name[record["user_id"]].add(record["name"])
    print(f"  실명 보유 행 {len(named)}/{len(records)}")
    print(f"  이름→user_id 충돌 {sum(1 for v in name_to_id.values() if len(v) > 1)}건")
    print(f"  user_id→이름 충돌 {sum(1 for v in id_to_name.values() if len(v) > 1)}건")
    cohorts = defaultdict(set)
    for record in records:
        cohorts[record["user_id"]].add(cohort(record))
    multi = sum(1 for v in cohorts.values() if len(v) > 1)
    print(f"  2개 이상 기수 참여 {multi}/{len(cohorts)}명 (user_id로 기수 넘어 연결 가능)")


def report_history(histories: dict[str, list[dict[str, str]]]) -> None:
    print("\n[이력 깊이]")
    counts = [len(rows) for rows in histories.values()]
    print(f"  사용자당 제출 med={st.median(counts):.0f} mean={st.mean(counts):.1f} max={max(counts)}")
    depths = [i for rows in histories.values() for i in range(len(rows))]
    for threshold in (1, 3, 7):
        ratio = sum(1 for d in depths if d >= threshold) / len(depths) * 100
        print(f"  제출 시점에 과거 {threshold}건 이상 보유: {ratio:.0f}%")
    gaps = []
    for rows in histories.values():
        stamps = [datetime.fromisoformat(r["created_at"]) for r in rows]
        gaps += [(stamps[i] - stamps[i - 1]).days for i in range(1, len(stamps))]
    gaps = [g for g in gaps if 0 <= g < 400]
    print(f"  제출 간격 med={st.median(gaps):.0f}일 / 8일 이내 {sum(1 for g in gaps if g <= 8) / len(gaps) * 100:.0f}%")


def report_volume(records: list[dict[str, str]]) -> None:
    print("\n[분량과 토큰]")
    lengths = sorted(body_length(r) for r in records)
    median, p90 = st.median(lengths), lengths[int(len(lengths) * 0.9)]
    print(f"  본문 문자수 med={median:.0f} p90={p90} max={max(lengths)}")
    for weeks in (4, 12):
        print(f"  과거 {weeks:2d}회 원문 ≈ {median * weeks / CHARS_PER_TOKEN:,.0f} tok (med)"
              f" / {p90 * weeks / CHARS_PER_TOKEN:,.0f} tok (p90)")


def report_retrieval(histories: dict[str, list[dict[str, str]]]) -> None:
    """최근 N회 창으로 충분한지, 과거 검색이 필요한지 측정한다."""
    print("\n[최근 4회 창 vs 과거 검색]")
    random.seed(0)  # 무작위 기대값 재현성
    vectorize = build_tfidf(histories)
    ratios, actual, expected = [], [], []
    for rows in histories.values():
        if len(rows) < 8:
            continue
        vectors = [vectorize(r) for r in rows]
        for i in range(6, len(rows)):
            similarities = [cosine(vectors[i], vectors[j]) for j in range(i)]
            best = max(similarities)
            if best <= 0:
                continue
            ratios.append(max(similarities[-4:]) / best)
            actual.append(i - similarities.index(best))
            expected.append(i - random.randrange(i))
    print(f"  대상 {len(ratios)}건")
    print(f"  최근4회 최고 / 전체 최고 유사도: med={st.median(ratios):.2f} mean={st.mean(ratios):.2f}")
    print(f"  0.9 이상(창으로 충분) {sum(1 for r in ratios if r >= 0.9) / len(ratios) * 100:.0f}%"
          f" / 0.7 미만(검색 유리) {sum(1 for r in ratios if r < 0.7) / len(ratios) * 100:.0f}%")
    print(f"  최적 매칭 거리 실제 med={st.median(actual):.0f} vs 무작위 기대 med={st.median(expected):.0f}")
    print("  주의: 문자 2-gram 기반이라 어휘가 다른 동일 주제는 잡지 못한다."
          " '검색 유리' 비율은 하한으로 읽어야 한다.")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    records = load(sys.argv[1])
    histories = defaultdict(list)
    for record in sorted(records, key=lambda r: (r["user_id"], r["created_at"])):
        if body_length(record) >= 50:  # 테스트·빈 행 제외
            histories[record["user_id"]].append(record)

    report_overview(records)
    report_identity(records)
    report_history(histories)
    report_volume(records)
    report_retrieval(histories)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
