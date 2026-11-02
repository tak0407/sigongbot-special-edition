from slack.types import MessageBodyType
from slack.support_notifications import notify_support_recipients
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

    # 문의 채널의 새 사용자 글만 지정된 수신자에게 개별 DM으로 알린다.
    if (
        event["channel"] == settings.SUPPORT_CHANNEL
        and event["type"] == "message"
        and not event.get("subtype")
        and not event.get("bot_id")
        and not thread_ts
    ):
        text = f"👋 <@{event['user']}> 님이 <#{event['channel']}>에 문의를 남겼어요."
        await notify_support_recipients(
            client,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": (event.get("text") or "첨부된 문의를 채널에서 확인해주세요.")[:3000],
                    },
                },
            ],
        )
