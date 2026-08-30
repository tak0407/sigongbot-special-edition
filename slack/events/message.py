from slack.types import MessageBodyType
from config import settings

from slack_bolt.async_app import AsyncAck, AsyncSay
from slack_sdk.web.async_client import AsyncWebClient


async def handle_message(
    ack: AsyncAck,
    body: MessageBodyType,
    say: AsyncSay,
    client: AsyncWebClient,
) -> None:
    """메시지 이벤트 처리"""
    await ack()

    event = body["event"]
    thread_ts = event.get("thread_ts")

    # 문의 채널에 메시지 남길 시 관리자 채널에 알림 유저 이름을 알림으로 준다.
    if (
        event["channel"] == settings.SUPPORT_CHANNEL
        and event["type"] == "message"
        and not event.get("subtype")
        and not thread_ts
    ):
        admin_mentions = " ".join(f"<@{admin_id}>" for admin_id in settings.ADMIN_IDS)
        await client.chat_postMessage(
            channel=settings.ADMIN_CHANNEL,
            text=f"👋 <@{body['event']['user']}> 님이 <#{settings.SUPPORT_CHANNEL}> 에 문의를 남겼어요. 👀 {admin_mentions}",
        )
