"""Ephemeral 메시지에 공통 닫기 동작을 제공한다."""

import copy

import aiohttp
from loguru import logger
from slack_bolt.async_app import AsyncAck


DISMISS_EPHEMERAL_ACTION_ID = "dismiss_ephemeral"
_DELETE_PAYLOAD = {"delete_original": True}
# Slack section 블록의 텍스트 상한.
_SECTION_TEXT_LIMIT = 3000


def add_dismiss_button(blocks: list[dict] | None) -> list[dict]:
    """Ephemeral blocks에 메시지를 닫는 버튼을 추가한다.

    전달받은 blocks를 복사하므로 호출자가 재사용하는 원본 blocks는 바뀌지 않는다.
    기존 actions 블록이 있으면 그 안에 버튼을 추가하고, 없으면 새 actions 블록을
    만든다.
    """
    result = copy.deepcopy(blocks or [])
    button = {
        "type": "button",
        "action_id": DISMISS_EPHEMERAL_ACTION_ID,
        "text": {"type": "plain_text", "text": "닫기"},
        "value": "dismiss",
    }

    for block in reversed(result):
        if block.get("type") != "actions":
            continue
        elements = block.setdefault("elements", [])
        if not any(
            element.get("action_id") == DISMISS_EPHEMERAL_ACTION_ID
            for element in elements
        ):
            elements.append(button)
        return result

    result.append({"type": "actions", "elements": [button]})
    return result


def _body_blocks(text: str, blocks: list[dict] | None) -> list[dict] | None:
    """게시할 blocks를 고른다.

    blocks를 함께 보내면 Slack은 text를 본문이 아니라 알림 미리보기로만 쓴다.
    호출자가 blocks를 주지 않았는데 닫기 버튼만 붙이면 본문이 빈 메시지가 되므로,
    text를 section 블록으로 만들어 본문을 유지한다.
    """
    if blocks:
        return add_dismiss_button(blocks)
    if text and len(text) <= _SECTION_TEXT_LIMIT:
        return add_dismiss_button(
            [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        )
    # section 한도를 넘는 안내는 blocks 없이 보낸다. 닫기 버튼을 잃더라도 본문이
    # 잘리거나 사라지는 것보다 낫다.
    return None


async def post_ephemeral(
    client,
    *,
    channel: str,
    user: str,
    text: str,
    blocks: list[dict] | None = None,
    **kwargs,
):
    """닫기 버튼을 포함한 Ephemeral 메시지를 게시한다."""
    return await client.chat_postEphemeral(
        channel=channel,
        user=user,
        text=text,
        blocks=_body_blocks(text, blocks),
        **kwargs,
    )


async def delete_ephemeral(response_url: str | None) -> bool:
    """response_url로 원본 Ephemeral을 삭제한다.

    response_url은 Slack이 인터랙션마다 발급하는 일회성 경로이므로, 이 함수는
    별도의 Slack Web API 토큰이나 chat.delete를 사용하지 않는다. 실패는 사용자
    경험이나 공개 메시지에 영향을 주지 않도록 호출자에게 전파하지 않는다.
    """
    if not response_url:
        logger.bind(alert=False).debug(
            "Ephemeral 닫기 요청에 response_url이 없어 삭제를 건너뜁니다."
        )
        return False

    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(response_url, json=_DELETE_PAYLOAD) as response:
                if response.status < 200 or response.status >= 300:
                    logger.bind(alert=False).warning(
                        "Ephemeral 닫기 요청이 거절되었습니다 - status={}",
                        response.status,
                    )
                    return False
        return True
    except Exception as error:
        # response_url 실패는 닫기 동작 자체의 실패일 뿐, 봇 기능 오류가 아니다.
        # alert=False를 명시해 관리자 채널에 공개 알림을 만들지 않는다.
        logger.bind(alert=False).warning(
            "Ephemeral 닫기 요청에 실패했습니다 - error_type={}",
            type(error).__name__,
        )
        return False


async def handle_dismiss_ephemeral(ack: AsyncAck, body: dict) -> None:
    """닫기 버튼을 빠르게 ack하고 해당 인터랙션의 원본만 삭제한다."""
    await ack()
    await delete_ephemeral(body.get("response_url"))
