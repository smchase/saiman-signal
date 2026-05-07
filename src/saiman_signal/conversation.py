import json
import logging

import aiosqlite

from saiman_signal import config

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content_blocks TEXT NOT NULL
);
"""


async def init() -> None:
    global _db
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    await _db.execute(_SCHEMA)
    await _db.commit()


async def add_message(role: str, content_blocks: list[dict]) -> int:
    cursor = await _db.execute(
        "INSERT INTO messages (role, content_blocks) VALUES (?, ?)",
        (role, json.dumps(content_blocks)),
    )
    await _db.commit()
    return cursor.lastrowid


async def load_all() -> list[dict]:
    cursor = await _db.execute("SELECT role, content_blocks FROM messages ORDER BY id ASC")
    rows = await cursor.fetchall()
    messages = []
    for role, content_blocks_json in rows:
        messages.append({"role": role, "content": json.loads(content_blocks_json)})

    # Prune old tool results to save context (keep last 3 tool cycles intact)
    messages = _prune_old_tool_results(messages)

    # Apply ephemeral cache_control to last 2 messages for prompt caching
    for msg in messages[-2:]:
        content = msg["content"]
        if content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return messages


def _prune_old_tool_results(messages: list[dict]) -> list[dict]:
    """Replace old tool_result content with a placeholder to save context.

    Keeps the last 3 tool result messages intact; older ones get their content
    replaced with a short placeholder.
    """
    tool_result_indices = []
    for i, msg in enumerate(messages):
        if (
            msg["role"] == "user"
            and isinstance(msg["content"], list)
            and any(b.get("type") == "tool_result" for b in msg["content"])
        ):
            tool_result_indices.append(i)

    # Keep last 3 tool result messages intact
    to_prune = tool_result_indices[:-3] if len(tool_result_indices) > 3 else []

    for i in to_prune:
        content = messages[i]["content"]
        messages[i]["content"] = [
            {
                "type": "tool_result",
                "tool_use_id": b["tool_use_id"],
                "content": "[Tool result cleared to save context]",
            }
            for b in content
            if b.get("type") == "tool_result"
        ]

    return messages


async def rollback_incomplete_turn() -> None:
    """Delete any messages after the last user message that has no completed assistant reply."""
    cursor = await _db.execute(
        "SELECT id FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if not row:
        return
    last_user_id = row[0]
    # Check if there's a completed assistant response after it
    cursor = await _db.execute(
        "SELECT id FROM messages WHERE id > ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
        (last_user_id,),
    )
    last_assistant = await cursor.fetchone()
    if last_assistant:
        # There is an assistant response — check if there are incomplete messages after it
        # (this handles the case of mid-tool-loop cancellation)
        cursor = await _db.execute("SELECT MAX(id) FROM messages")
        max_row = await cursor.fetchone()
        if max_row and max_row[0] != last_assistant[0]:
            # There are messages after the last assistant reply — those are incomplete
            await _db.execute("DELETE FROM messages WHERE id > ?", (last_assistant[0],))
            await _db.commit()
    else:
        # No assistant response after last user message — this whole turn is incomplete
        # Keep the user message(s), delete everything else after them
        # Actually we want to keep all consecutive user messages at the end
        cursor = await _db.execute(
            """SELECT id FROM messages WHERE role != 'user'
               ORDER BY id DESC LIMIT 1"""
        )
        last_non_user = await cursor.fetchone()
        if last_non_user:
            # Delete intermediate messages between last completed turn and user messages
            # No — the user messages are already stored correctly. We just need to delete
            # any assistant/tool messages that were added during the incomplete agent run.
            await _db.execute(
                "DELETE FROM messages WHERE id > ? AND role != 'user'", (last_non_user[0],)
            )
            await _db.commit()


async def clear() -> None:
    await _db.execute("DELETE FROM messages")
    await _db.commit()
