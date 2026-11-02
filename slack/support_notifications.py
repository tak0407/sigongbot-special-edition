"""문의·개선 제안 알림을 지정된 사용자에게 각각 DM으로 보낸다."""

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from config import settings


async def notify_support_recipients(
    client: AsyncWebClient, *, text: str, blocks: list[dict] | None = None
) -> bool:
    """모든 고유 수신자에게 발송한 경우에만 True를 반환한다."""
    recipients = list(dict.fromkeys(settings.SUPPORT_NOTIFICATION_USER_IDS))
    if not recipients:
        logger.bind(alert=False).warning("문의 알림 DM 수신자가 설정되지 않았습니다.")
        return False

    all_notified = True
    for user_id in recipients:
        try:
            response = await client.conversations_open(users=user_id)
            payload: dict = {"channel": response["channel"]["id"], "text": text}
            if blocks is not None:
                payload["blocks"] = blocks
            await client.chat_postMessage(**payload)
        except Exception as error:
            all_notified = False
            logger.bind(alert=False).warning(
                "문의 알림 DM 전송 실패 - user_id={}, error_type={}",
                user_id,
                type(error).__name__,
            )
    return all_notified
