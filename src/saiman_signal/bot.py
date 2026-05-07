import asyncio
import base64
import contextlib
import json
import logging

import websockets

from saiman_signal import agent, config, conversation, signal_api
from saiman_signal.transcription import transcribe

logger = logging.getLogger(__name__)

_current_task: asyncio.Task | None = None
_typing_stop: asyncio.Event | None = None

_VOICE_CONTENT_TYPES = {"audio/aac", "audio/ogg", "audio/mp4", "audio/mpeg"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await conversation.init()
    logger.info("Bot starting")

    ws_url = f"ws://{config.SIGNAL_API_URL.removeprefix('http://').removeprefix('https://')}/v1/receive/{config.BOT_PHONE_NUMBER}"

    while True:
        try:
            logger.info(f"Connecting to {ws_url}")
            async with websockets.connect(ws_url) as ws:
                logger.info("Connected")
                async for raw in ws:
                    try:
                        envelope = json.loads(raw).get("envelope", {})
                        if envelope.get("dataMessage") is not None:
                            asyncio.create_task(_handle_envelope(envelope))
                    except json.JSONDecodeError:
                        pass
        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            logger.warning(f"Disconnected: {e}")
            await asyncio.sleep(2)


async def _handle_envelope(envelope: dict) -> None:
    source = envelope.get("source") or envelope.get("sourceNumber", "")
    if source != config.ALLOWED_NUMBER:
        return

    data_msg = envelope["dataMessage"]
    timestamp = data_msg.get("timestamp", 0)
    text = data_msg.get("message", "") or ""
    attachments = data_msg.get("attachments", [])

    with contextlib.suppress(Exception):
        await signal_api.send_read_receipt(source, timestamp)

    # Handle CLEAR
    if text.strip() == "CLEAR":
        await conversation.clear()
        await signal_api.react(source, source, timestamp, "✅")
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
        content_blocks.append({"type": "text", "text": text})

    if not content_blocks:
        return

    # Store user message
    await conversation.add_message("user", content_blocks)

    # Cancel-and-restart if currently processing
    global _current_task, _typing_stop
    if _current_task and not _current_task.done():
        _current_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _current_task
        await conversation.rollback_incomplete_turn()

    _current_task = asyncio.create_task(_process_and_respond(source))


async def _process_and_respond(recipient: str) -> None:
    global _typing_stop

    # Start typing indicator
    _typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(recipient, _typing_stop))

    try:
        messages = await conversation.load_all()
        response_parts = await agent.run(messages)

        _typing_stop.set()
        await typing_task

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
