import asyncio
import base64
import contextlib
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from saiman_signal import agent, config, conversation, signal_api
from saiman_signal.transcription import transcribe

logger = logging.getLogger(__name__)

_current_task: asyncio.Task | None = None
_typing_stop: asyncio.Event | None = None

_VOICE_CONTENT_TYPES = {"audio/aac", "audio/ogg", "audio/mp4", "audio/mpeg"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}
_LOCATION_PATH = config.DATA_DIR / "location.json"


def _time_prefix() -> str:
    """Generate timestamp prefix for user messages, using location timezone if set."""
    try:
        data = json.loads(_LOCATION_PATH.read_text())
        tz = ZoneInfo(data["timezone"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return f"[{now.strftime('%A, %B %-d, %Y at %-I:%M %p')}]\n\n"


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    await conversation.init()
    logger.info("Bot starting")

    ws_url = f"ws://{config.SIGNAL_API_URL.removeprefix('http://').removeprefix('https://')}/v1/receive/{config.BOT_PHONE_NUMBER}"

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
    global _current_task, _typing_stop
    source = envelope.get("source") or envelope.get("sourceNumber", "")
    if source != config.ALLOWED_NUMBER:
        logger.debug(f"Ignoring message from unauthorized sender: {source}")
        return

    data_msg = envelope["dataMessage"]
    timestamp = data_msg.get("timestamp", 0)
    text = data_msg.get("message", "") or ""
    attachments = data_msg.get("attachments", [])

    logger.info(f"Message received: {text[:100]!r} (attachments: {len(attachments)})")

    with contextlib.suppress(Exception):
        await signal_api.send_read_receipt(source, timestamp)

    # Handle CLEAR
    if text.strip() == "CLEAR":
        if _current_task and not _current_task.done():
            _current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _current_task
            await conversation.rollback_incomplete_turn()
        await conversation.clear()
        await signal_api.react(source, source, timestamp, "✅")
        logger.info("Conversation cleared")
        return

    # Process attachments
    content_blocks = []
    for att in attachments:
        content_type = att.get("contentType", "")
        att_id = att.get("id")
        if not att_id:
            continue

        if content_type in _VOICE_CONTENT_TYPES:
            path = await signal_api.download_attachment(att_id)
            if path:
                try:
                    transcribed = await transcribe(path)
                    text = f"[voice message] {transcribed}" + (f"\n{text}" if text else "")
                except Exception as e:
                    logger.error(f"Transcription failed: {e}")
                    await signal_api.send_message(source, f"Transcription failed: {e}")
                    return

        elif content_type in _IMAGE_CONTENT_TYPES:
            path = await signal_api.download_attachment(att_id)
            if path:
                image_data = base64.standard_b64encode(path.read_bytes()).decode()
                media_type = content_type
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    }
                )

    # Build user message content
    if text:
        content_blocks.append({"type": "text", "text": f"{_time_prefix()}{text}"})

    if not content_blocks:
        return

    # Store user message
    await conversation.add_message("user", content_blocks)

    # Cancel-and-restart if currently processing
    if _current_task and not _current_task.done():
        logger.info("Cancelling in-flight request (new message arrived)")
        _current_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _current_task
        await conversation.rollback_incomplete_turn()

    _current_task = asyncio.create_task(_process_and_respond(source))


async def _process_and_respond(recipient: str) -> None:
    global _typing_stop
    import time

    start = time.monotonic()

    # Start typing indicator
    _typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(recipient, _typing_stop))

    try:
        messages = await conversation.load_all()
        logger.info(f"Running agent with {len(messages)} messages in context")
        response_parts = await agent.run(messages)

        _typing_stop.set()
        await typing_task

        elapsed = time.monotonic() - start
        logger.info(f"Response ready ({elapsed:.1f}s, {len(response_parts)} part(s))")

        for part in response_parts:
            await signal_api.send_message(recipient, part)

    except asyncio.CancelledError:
        _typing_stop.set()
        typing_task.cancel()
        raise
    except Exception as e:
        _typing_stop.set()
        typing_task.cancel()
        logger.exception("Processing error")
        await signal_api.send_message(recipient, f"Error: {e}")


async def _typing_loop(recipient: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await signal_api.send_typing(recipient)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
        except TimeoutError:
            continue


