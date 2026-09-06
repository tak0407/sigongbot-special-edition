import tempfile
from pathlib import Path

from ai_review.antigravity import run_antigravity


SCHEMA = {
    "type": "object",
    "properties": {
        "good_points": {"type": "string"},
        "improvements": {"type": "string"},
        "learnings": {"type": "string"},
        "action_item": {"type": "string"},
    },
    "required": ["good_points", "improvements", "learnings", "action_item"],
    "additionalProperties": False,
}


async def format_guided_answers(responses: list[dict[str, str]]) -> dict[str, str]:
    transcript = "\n\n".join(
        f"[{item['label']} / {item.get('stage', '')}]\n질문: {item['question']}\n답변: {item['answer']}"
        for item in responses
        if item["answer"].strip()
    )
    prompt = f"""
다음은 사용자가 주간 회고의 항목별 질문에 답한 내용입니다.
사용자가 말하지 않은 사실을 추가하거나 과장하지 말고, 사용자의 말투를 최대한 유지하세요.
파일 탐색, 검색, 터미널 명령, 파일 작성은 하지 마세요.
답변을 질문 순서대로 복사하지 말고, 답변 사이의 연결과 반복되는 패턴을 찾아 하나의 회고로 종합하세요.
stage가 good_points, improvements, learnings, action_item이면 해당 항목 안에서만 답변을 종합하세요.
답변하지 않은 항목은 빈 문자열로 두세요. 건너뛴 질문의 답이나 새로운 원인·교훈·계획을 만들어내지 마세요.
good_points, improvements, learnings, action_item 각 항목은 450자 이내로 작성하고 주어진 JSON 스키마로만 답하세요.

{transcript}
""".strip()

    with tempfile.TemporaryDirectory(prefix="sigongbot-format-") as temp:
        payload = await run_antigravity(
            prompt=prompt,
            working_directory=Path(temp),
            schema=SCHEMA,
        )

    result = payload.get("structured_output")
    if not isinstance(result, dict):
        raise RuntimeError("Antigravity 구조화 회고 결과가 없습니다.")
    if any(not isinstance(result.get(key), str) for key in SCHEMA["required"]):
        raise RuntimeError("Antigravity 회고 결과의 항목 형식이 올바르지 않습니다.")
    formatted = {key: result[key].strip() for key in SCHEMA["required"]}
    grouped = all(item.get("stage") in SCHEMA["required"] for item in responses)
    answered = {item.get("stage") for item in responses if item["answer"].strip()}
    for key in formatted:
        if grouped and key not in answered:
            formatted[key] = ""
        elif not formatted[key] or len(formatted[key]) > 500:
            raise RuntimeError("Antigravity 회고 결과가 비어 있거나 너무 깁니다.")
    return formatted
