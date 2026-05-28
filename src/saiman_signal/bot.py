import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from saiman_signal import config, conversation, signal_api
from saiman_signal.agent import EmptyResponseError
from saiman_signal.agent import run as agent_run
from saiman_signal.transcription import transcribe

logger = logging.getLogger(__name__)

_VOICE_CONTENT_TYPES = {"audio/aac", "audio/ogg", "audio/mp4", "audio/mpeg"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}

_tasks: dict[str, asyncio.Task] = {}


def _time_prefix(user_id: str) -> str:
    try:
        data = json.loads(config.location_path(user_id).read_text())
        tz = ZoneInfo(data["timezone"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return f"[{now.strftime('%A, %B %-d, %Y at %-I:%M %p')}]\n\n"


async def _cancel_current(user_id: str) -> None:
    task = _tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await conversation.rollback_incomplete_turn(user_id)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    await conversation.init()
    logger.info("Bot starting")

    ws_url = (
        f"ws://{config.SIGNAL_API_URL.removeprefix('http://').removeprefix('https://')}"
        f"/v1/receive/{config.BOT_PHONE_NUMBER}"
    )

    while True:
        try:
            logger.info(f"Connecting to {ws_url}")
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                logger.info("Connected")
                async for raw in ws:
                    try:
                        envelope = json.loads(raw).get("envelope", {})
                        if envelope.get("dataMessage") is not None:
                            asyncio.create_task(_handle_envelope(envelope))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"Disconnected: {e}")
            await asyncio.sleep(5)


async def _handle_envelope(envelope: dict) -> None:
    source = envelope.get("source") or envelope.get("sourceNumber", "")
    if source not in config.ALLOWED_NUMBERS:
        return

    user_id = source

    data_msg = envelope["dataMessage"]
    timestamp = data_msg.get("timestamp", 0)
    text = data_msg.get("message", "") or ""
    attachments = data_msg.get("attachments", [])

    logger.info(f"[{user_id}] Message received (len={len(text)}, attachments={len(attachments)})")

    with contextlib.suppress(Exception):
        await signal_api.send_read_receipt(source, timestamp)

    if text.strip() == "CLEAR":
        await _cancel_current(user_id)
        with contextlib.suppress(Exception):
            await signal_api.stop_typing(source)
        await conversation.clear(user_id)
        await signal_api.react(source, source, timestamp, "✅")
        logger.info(f"[{user_id}] Conversation cleared")
        return

    content_blocks = []
    for att in attachments:
        content_type = att.get("contentType", "")
        att_id = att.get("id")
        if not att_id:
            continue

        if content_type in _VOICE_CONTENT_TYPES:
            path = await signal_api.download_attachment(att_id)
            if not path:
                await signal_api.send_message(source, "[failed to download voice message]")
                return
            try:
                transcribed = await transcribe(path)
            except Exception as e:
                logger.error(f"[{user_id}] Transcription failed: {e}")
                await signal_api.send_message(source, f"[transcription failed: {e}]")
                return
            if not transcribed.strip():
                await signal_api.send_message(source, "[transcription returned empty]")
                return
            text = f"[voice message] {transcribed}" + (f"\n{text}" if text else "")

        elif content_type in _IMAGE_CONTENT_TYPES:
            path = await signal_api.download_attachment(att_id)
            if not path:
                await signal_api.send_message(source, "[failed to download image]")
                return
            image_data = base64.standard_b64encode(path.read_bytes()).decode()
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": content_type,
                    "data": image_data,
                },
            })

    if text:
        content_blocks.append({"type": "text", "text": f"{_time_prefix(user_id)}{text}"})

    if not content_blocks:
        return

    await conversation.add_message(user_id, "user", content_blocks)

    await _cancel_current(user_id)
    _tasks[user_id] = asyncio.create_task(_process_and_respond(user_id, source))


async def _process_and_respond(user_id: str, recipient: str) -> None:
    start = time.monotonic()
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(recipient, stop))

    try:
        messages = await conversation.load_all(user_id)
        logger.info(f"[{user_id}] Running agent with {len(messages)} messages in context")
        response_parts = await agent_run(messages, user_id)

        stop.set()
        await typing_task

        elapsed = time.monotonic() - start
        logger.info(f"[{user_id}] Response ready ({elapsed:.1f}s, {len(response_parts)} part(s))")

        for part in response_parts:
            await signal_api.send_message(recipient, part)

    except asyncio.CancelledError:
        stop.set()
        typing_task.cancel()
        raise
    except EmptyResponseError as e:
        stop.set()
        typing_task.cancel()
        await conversation.rollback_incomplete_turn(user_id)
        await signal_api.send_message(
            recipient, f"[empty response — stop_reason={e.stop_reason}. try rephrasing]"
        )
    except Exception as e:
        stop.set()
        typing_task.cancel()
        await conversation.rollback_incomplete_turn(user_id)
        error_msg = _classify_error(e)
        logger.exception(f"[{user_id}] Processing error")
        await signal_api.send_message(recipient, error_msg)


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


async def _typing_loop(recipient: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await signal_api.send_typing(recipient)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
        except TimeoutError:
            continue
