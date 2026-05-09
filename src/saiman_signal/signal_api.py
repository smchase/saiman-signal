import logging
import re
from pathlib import Path

import httpx

from saiman_signal import config

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(base_url=config.SIGNAL_API_URL, timeout=30.0)

_APPROX_TILDE = re.compile(r"(?<!\\)~(?=\d)")


async def send_message(recipient: str, text: str) -> int | None:
    text = _APPROX_TILDE.sub(r"\~", text)
    response = await _client.post(
        "/v2/send",
        json={
            "message": text,
            "number": config.BOT_PHONE_NUMBER,
            "recipients": [recipient],
            "text_mode": "styled",
        },
    )
    if response.status_code == 201:
        ts = response.json().get("timestamp")
        return int(ts) if ts else None
    logger.error(f"Send failed ({response.status_code}): {response.text}")
    return None


async def send_typing(recipient: str) -> None:
    await _client.put(
        f"/v1/typing-indicator/{config.BOT_PHONE_NUMBER}",
        json={"recipient": recipient},
    )


async def send_read_receipt(recipient: str, timestamp: int) -> None:
    await _client.post(
        f"/v1/receipts/{config.BOT_PHONE_NUMBER}",
        json={"receipt_type": "read", "recipient": recipient, "timestamp": timestamp},
    )


async def react(recipient: str, target_author: str, timestamp: int, emoji: str) -> None:
    await _client.post(
        f"/v1/reactions/{config.BOT_PHONE_NUMBER}",
        json={
            "recipient": recipient,
            "reaction": emoji,
            "target_author": target_author,
            "timestamp": timestamp,
        },
    )


async def download_attachment(attachment_id: str) -> Path | None:
    response = await _client.get(f"/v1/attachments/{attachment_id}")
    if response.status_code != 200:
        return None
    config.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.ATTACHMENTS_DIR / attachment_id
    path.write_bytes(response.content)
    return path
