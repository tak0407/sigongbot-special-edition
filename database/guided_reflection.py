import asyncio
import json
import secrets
from typing import Any

from database.sqlite import get_connection


async def create_guided_reflection(
    *, user_id: str, slack_channel: str, session_name: str, questions: list[dict]
) -> dict[str, Any]:
    flow_id = secrets.token_urlsafe(12)

    def create() -> dict[str, Any]:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO guided_reflections (
                    flow_id, user_id, slack_channel, session_name, questions_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    user_id,
                    slack_channel,
                    session_name,
                    json.dumps(questions, ensure_ascii=False),
                ),
            )
        return {
            "flow_id": flow_id,
            "user_id": user_id,
            "slack_channel": slack_channel,
            "session_name": session_name,
            "questions": questions,
            "answers": [],
            "current_index": 0,
        }

    return await asyncio.to_thread(create)


def _decode(row) -> dict[str, Any]:
    result = dict(row)
    result["questions"] = json.loads(result.pop("questions_json"))
    result["answers"] = json.loads(result.pop("answers_json"))
    formatted_json = result.pop("formatted_json", None)
    result["formatted"] = json.loads(formatted_json) if formatted_json else None
    return result


async def get_guided_reflection(flow_id: str) -> dict[str, Any] | None:
    def select() -> dict[str, Any] | None:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM guided_reflections WHERE flow_id = ?", (flow_id,)
            ).fetchone()
            return _decode(row) if row else None

    return await asyncio.to_thread(select)


async def save_guided_answer(
    *, flow_id: str, user_id: str, answer: str
) -> dict[str, Any]:
    def save() -> dict[str, Any]:
        connection = get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM guided_reflections
                 WHERE flow_id = ? AND user_id = ?
                """,
                (flow_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("진행 중인 질문형 회고를 찾지 못했습니다.")

            flow = _decode(row)
            index = int(flow["current_index"])
            answers = list(flow["answers"])
            if index < len(answers):
                answers[index] = answer
            else:
                answers.append(answer)
            next_index = index + 1
            connection.execute(
                """
                UPDATE guided_reflections
                   SET answers_json = ?, current_index = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE flow_id = ?
                """,
                (json.dumps(answers, ensure_ascii=False), next_index, flow_id),
            )
            connection.commit()
            flow["answers"] = answers
            flow["current_index"] = next_index
            return flow
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return await asyncio.to_thread(save)


async def go_to_previous_question(
    *, flow_id: str, user_id: str, current_answer: str | None = None
) -> dict[str, Any]:
    def move() -> dict[str, Any]:
        connection = get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM guided_reflections
                 WHERE flow_id = ? AND user_id = ?
                """,
                (flow_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("진행 중인 질문형 회고를 찾지 못했습니다.")

            flow = _decode(row)
            index = int(flow["current_index"])
            answers = list(flow["answers"])
            if current_answer:
                if index < len(answers):
                    answers[index] = current_answer
                else:
                    answers.append(current_answer)
            previous_index = max(0, index - 1)
            connection.execute(
                """
                UPDATE guided_reflections
                   SET answers_json = ?, current_index = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE flow_id = ?
                """,
                (
                    json.dumps(answers, ensure_ascii=False),
                    previous_index,
                    flow_id,
                ),
            )
            connection.commit()
            flow["answers"] = answers
            flow["current_index"] = previous_index
            return flow
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return await asyncio.to_thread(move)


async def save_guided_format(
    *, flow_id: str, formatted: dict[str, str | bool]
) -> dict[str, Any]:
    def save() -> dict[str, Any]:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE guided_reflections
                   SET formatted_json = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE flow_id = ?
                """,
                (json.dumps(formatted, ensure_ascii=False), flow_id),
            )
            row = connection.execute(
                "SELECT * FROM guided_reflections WHERE flow_id = ?", (flow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("질문형 회고를 찾지 못했습니다.")
            return _decode(row)

    return await asyncio.to_thread(save)


async def delete_guided_reflection(flow_id: str) -> None:
    def delete() -> None:
        with get_connection() as connection:
            connection.execute(
                "DELETE FROM guided_reflections WHERE flow_id = ?", (flow_id,)
            )

    await asyncio.to_thread(delete)
