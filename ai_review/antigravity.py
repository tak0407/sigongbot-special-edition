import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from config import settings
from ai_review.runtime import AI_SEMAPHORE


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "status" in payload:
            return payload
    raise RuntimeError("Antigravity JSON 결과를 찾지 못했습니다.")


async def run_antigravity(
    *,
    prompt: str,
    working_directory: Path,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    executable = shutil.which(settings.ANTIGRAVITY_COMMAND)
    if executable is None:
        raise RuntimeError("Antigravity CLI(agy)를 찾지 못했습니다.")

    command = [
        executable,
        "--sandbox",
        "--mode",
        "plan",
        "--model",
        settings.ANTIGRAVITY_MODEL,
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--print-timeout",
        f"{settings.AI_REVIEW_TIMEOUT_SECONDS}s",
    ]
    if schema is not None:
        schema_path = working_directory / "output-schema.json"
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False), encoding="utf-8"
        )
        command.extend(["--json-schema", str(schema_path)])
    command.append(f"--print={prompt}")

    async with AI_SEMAPHORE:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.AI_REVIEW_TIMEOUT_SECONDS + 30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("Antigravity 실행 시간이 초과되었습니다.")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(stderr[-1500:] or "Antigravity 실행에 실패했습니다.")

    payload = _parse_result(stdout)
    if payload.get("status") != "SUCCESS":
        error = payload.get("error") or stderr or "Antigravity 응답이 실패했습니다."
        raise RuntimeError(str(error)[-1500:])
    return payload
