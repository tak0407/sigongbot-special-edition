"""회고 입력 형식과 길이에 관한 공통 규칙."""

STRUCTURED_TEXT_MAX_LENGTH = 1000
ESSAY_MIN_LENGTH = 100
ESSAY_MAX_LENGTH = 3000
ESSAY_TITLE_MAX_LENGTH = 150


def validate_essay(title: str | None, body: str | None) -> tuple[str | None, str]:
    """제목을 정규화하고, 검증한 본문은 입력 원문 그대로 반환한다."""
    if body is None or not body.strip():
        raise ValueError("에세이 본문을 입력해주세요.")
    if len(body.strip()) < ESSAY_MIN_LENGTH:
        raise ValueError(
            f"에세이 본문은 앞뒤 공백을 제외하고 {ESSAY_MIN_LENGTH}자 이상 입력해주세요."
        )
    if len(body) > ESSAY_MAX_LENGTH:
        raise ValueError(f"에세이 본문은 {ESSAY_MAX_LENGTH:,}자 이하로 입력해주세요.")
    if title is not None and len(title) > ESSAY_TITLE_MAX_LENGTH:
        raise ValueError(f"에세이 제목은 {ESSAY_TITLE_MAX_LENGTH}자 이하로 입력해주세요.")
    return (title.strip() or None) if title is not None else None, body
