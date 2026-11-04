"""회고 작성 형식에 맞는 Slack 본문 블록을 만든다."""

from collections.abc import Mapping

from slack_sdk.models.blocks import ContextBlock, DividerBlock, SectionBlock


STRUCTURED_FIELDS = (
    ("good_points", "잘했고 좋았던 점", "🌟"),
    ("improvements", "아쉽고 개선하고 싶은 점", "🔧"),
    ("learnings", "새롭게 배운 점", "💡"),
    ("action_item", "해볼만한 액션 아이템", "🚀"),
)


def build_retrospective_blocks(record: Mapping, *, heading: str | None = None) -> list:
    blocks = [SectionBlock(text=heading), DividerBlock()] if heading else []
    if record.get("content_format", "structured") == "essay":
        blocks.append(ContextBlock(elements=[{"type": "mrkdwn", "text": "*자유 에세이* ✍️"}]))
        if record.get("essay_title"):
            blocks.append(SectionBlock(text={"type": "plain_text", "text": record["essay_title"]}))
        blocks.append(SectionBlock(text={"type": "plain_text", "text": record["essay_body"]}))
    else:
        for index, (field, label, icon) in enumerate(STRUCTURED_FIELDS):
            if index:
                blocks.append(DividerBlock())
            blocks.extend([
                ContextBlock(elements=[{"type": "mrkdwn", "text": f"*{label}* {icon}"}]),
                SectionBlock(text=record.get(field) or "작성되지 않음"),
            ])

    if record.get("emotion_score"):
        blocks.extend([
            DividerBlock(),
            SectionBlock(text=f"*오늘의 감정점수* :bar_chart: {record['emotion_score']}/10"),
        ])
        if record.get("emotion_reason"):
            blocks.append(SectionBlock(text=record["emotion_reason"]))
    return blocks
