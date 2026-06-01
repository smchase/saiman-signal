import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from saiman_signal import config, conversation
from saiman_signal.agent import EmptyResponseError
from saiman_signal.agent import run as agent_run
from saiman_signal.transcription import transcribe

logger = logging.getLogger(__name__)

_BASE_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_FILE_URL = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}"
_client = httpx.AsyncClient(timeout=60.0)

_VOICE_CONTENT_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_tasks: dict[str, asyncio.Task] = {}

USER_ID = f"tg_{config.TELEGRAM_CHAT_ID}"


def _time_prefix() -> str:
    try:
        data = json.loads(config.location_path(USER_ID).read_text())
        tz = ZoneInfo(data["timezone"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return f"[{now.strftime('%A, %B %-d, %Y at %-I:%M %p')}]\n\n"


async def send_message(chat_id: int, text: str) -> None:
    await _client.post(
        f"{_BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    )


async def _react(chat_id: int, message_id: int, emoji: str) -> None:
    await _client.post(
        f"{_BASE_URL}/setMessageReaction",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        },
    )


async def send_typing(chat_id: int) -> None:
    await _client.post(
        f"{_BASE_URL}/sendChatAction",
        json={"chat_id": chat_id, "action": "typing"},
    )


async def _download_file(file_id: str) -> bytes | None:
    resp = await _client.post(
        f"{_BASE_URL}/getFile", json={"file_id": file_id}
    )
    if resp.status_code != 200:
        return None
    file_path = resp.json().get("result", {}).get("file_path")
    if not file_path:
        return None
    dl = await _client.get(f"{_FILE_URL}/{file_path}")
    return dl.content if dl.status_code == 200 else None


async def _cancel_current() -> None:
    task = _tasks.get(USER_ID)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await conversation.rollback_incomplete_turn(USER_ID)


async def telegram_loop() -> None:
    logger.info("Telegram bot starting")
    offset = 0

    while True:
        try:
            resp = await _client.get(
                f"{_BASE_URL}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=45.0,
            )
            if resp.status_code != 200:
                await asyncio.sleep(5)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    asyncio.create_task(_handle_message(message))

        except Exception as e:
            logger.warning(f"Telegram poll error: {e}")
            await asyncio.sleep(5)


async def _handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]

    if str(chat_id) != config.TELEGRAM_CHAT_ID:
        logger.info(f"Telegram: unauthorized chat_id={chat_id}")
        await send_message(chat_id, f"unauthorized — your chat_id is {chat_id}")
        return

    text = message.get("text", "") or message.get("caption", "") or ""

    if text.strip().upper() == "CLEAR":
        await _cancel_current()
        await conversation.clear(USER_ID)
        await _react(chat_id, message["message_id"], "👍")
        logger.info(f"[{USER_ID}] Conversation cleared")
        return

    content_blocks = []

    voice = message.get("voice") or message.get("audio")
    if voice:
        data = await _download_file(voice["file_id"])
        if not data:
            await send_message(chat_id, "[failed to download voice message]")
            return
        config.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.ATTACHMENTS_DIR / f"tg_{voice['file_id']}.ogg"
        path.write_bytes(data)
        try:
            transcribed = await transcribe(path)
        except Exception as e:
            logger.error(f"[{USER_ID}] Transcription failed: {e}")
            await send_message(chat_id, f"[transcription failed: {e}]")
            return
        if not transcribed.strip():
            await send_message(chat_id, "[transcription returned empty]")
            return
        text = f"[voice message] {transcribed}" + (f"\n{text}" if text else "")

    photo = message.get("photo")
    if photo:
        largest = photo[-1]
        data = await _download_file(largest["file_id"])
        if not data:
            await send_message(chat_id, "[failed to download image]")
            return
        image_data = base64.standard_b64encode(data).decode()
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_data,
            },
        })

    if text:
        content_blocks.append({"type": "text", "text": f"{_time_prefix()}{text}"})

    if not content_blocks:
        return

    _STALE_THRESHOLD = 43200
    gap = await conversation.seconds_since_last_message(USER_ID)
    remind_clear = gap is not None and gap > _STALE_THRESHOLD

    await conversation.add_message(USER_ID, "user", content_blocks)

    await _cancel_current()
    _tasks[USER_ID] = asyncio.create_task(
        _process_and_respond(chat_id, remind_clear)
    )


_CLEAR_REMINDER = 'Reminder: send "CLEAR" to start a new conversation.'


async def _process_and_respond(chat_id: int, remind_clear: bool = False) -> None:
    start = time.monotonic()
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(chat_id, stop))

    try:
        messages = await conversation.load_all(USER_ID)
        logger.info(f"[{USER_ID}] Running agent with {len(messages)} messages in context")
        response_parts = await agent_run(messages, USER_ID)

        stop.set()
        await typing_task

        elapsed = time.monotonic() - start
        logger.info(f"[{USER_ID}] Response ready ({elapsed:.1f}s, {len(response_parts)} part(s))")

        for part in response_parts:
            await send_message(chat_id, part)

        if remind_clear:
            await send_message(chat_id, _CLEAR_REMINDER)

    except asyncio.CancelledError:
        stop.set()
        typing_task.cancel()
        raise
    except EmptyResponseError as e:
        stop.set()
        typing_task.cancel()
        await conversation.rollback_incomplete_turn(USER_ID)
        await send_message(
            chat_id, f"[empty response — stop_reason={e.stop_reason}. try rephrasing]"
        )
    except Exception as e:
        stop.set()
        typing_task.cancel()
        await conversation.rollback_incomplete_turn(USER_ID)
        error_msg = _classify_error(e)
        logger.exception(f"[{USER_ID}] Processing error")
        await send_message(chat_id, error_msg)


def _classify_error(e: Exception) -> str:
    msg = str(e)
    name = type(e).__name__

    if "timeout" in name.lower() or "timed out" in msg.lower():
        return "[timed out waiting for model response. try again or CLEAR]"
    if "overloaded" in msg.lower() or "529" in msg:
        return "[model overloaded — try again in a minute]"
    if "rate" in msg.lower() and "limit" in msg.lower():
        return "[rate limited — try again in a minute]"
    if "context" in msg.lower() and ("long" in msg.lower() or "length" in msg.lower()):
        return "[conversation too long for model context. send CLEAR to reset]"
    if "400" in msg:
        return f"[bad request to model API: {msg}]"

    return f"[error: {name}: {msg}]"


async def _typing_loop(chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await send_typing(chat_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except TimeoutError:
            continue
