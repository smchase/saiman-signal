import json
import logging

import aiosqlite

from saiman_signal import config

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_blocks TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id);
"""


async def init() -> None:
    global _db
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    await _migrate()
    await _db.commit()


async def _migrate() -> None:
    cursor = await _db.execute("PRAGMA table_info(messages)")
    columns = {row[1] for row in await cursor.fetchall()}

    if not columns:
        await _db.executescript(_SCHEMA)
    elif "user_id" not in columns:
        default = config.PRIMARY_NUMBER
        await _db.execute(
            f"ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT '{default}'"
        )
        await _db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)"
        )


async def add_message(user_id: str, role: str, content_blocks: list[dict]) -> int:
    cursor = await _db.execute(
        "INSERT INTO messages (user_id, role, content_blocks) VALUES (?, ?, ?)",
        (user_id, role, json.dumps(content_blocks)),
    )
    await _db.commit()
    return cursor.lastrowid


async def load_all(user_id: str) -> list[dict]:
    cursor = await _db.execute(
        "SELECT role, content_blocks FROM messages WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    messages = []
    for role, content_blocks_json in rows:
        messages.append({"role": role, "content": json.loads(content_blocks_json)})

    messages = _prune_old_tool_results(messages)

    for msg in messages[-2:]:
        content = msg["content"]
        if content and isinstance(content[-1], dict):
            last = content[-1]
            if last.get("type") == "text" and not last.get("text"):
                continue
            last["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
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


async def rollback_incomplete_turn(user_id: str) -> None:
    """Delete any messages after the last user message that has no completed assistant reply."""
    cursor = await _db.execute(
        "SELECT id FROM messages WHERE user_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return
    last_user_id = row[0]
    cursor = await _db.execute(
        "SELECT id FROM messages WHERE user_id = ? AND id > ?"
        " AND role = 'assistant' ORDER BY id DESC LIMIT 1",
        (user_id, last_user_id),
    )
    last_assistant = await cursor.fetchone()
    if last_assistant:
        cursor = await _db.execute(
            "SELECT MAX(id) FROM messages WHERE user_id = ?", (user_id,)
        )
        max_row = await cursor.fetchone()
        if max_row and max_row[0] != last_assistant[0]:
            await _db.execute(
                "DELETE FROM messages WHERE user_id = ? AND id > ?",
                (user_id, last_assistant[0]),
            )
            await _db.commit()
    else:
        cursor = await _db.execute(
            "SELECT id FROM messages WHERE user_id = ? AND role != 'user' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        last_non_user = await cursor.fetchone()
        if last_non_user:
            await _db.execute(
                "DELETE FROM messages WHERE user_id = ? AND id > ? AND role != 'user'",
                (user_id, last_non_user[0]),
            )
            await _db.commit()


async def clear(user_id: str) -> None:
    await _db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    await _db.commit()
