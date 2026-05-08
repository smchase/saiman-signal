import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropicBedrock

from saiman_signal import config, conversation
from saiman_signal.tools import TOOL_DEFINITIONS, TOOLS

logger = logging.getLogger(__name__)

_client = AsyncAnthropicBedrock(aws_region=config.AWS_REGION, timeout=300.0)

_SYSTEM_PROMPT_PATH = __file__.replace("agent.py", "system_prompt.txt")

MAX_ITERATIONS = 20


def _load_system_prompt() -> str:
    with open(_SYSTEM_PROMPT_PATH) as f:
        return f.read()


_SYSTEM_PROMPT = _load_system_prompt()


_LOCATION_PATH = config.DATA_DIR / "location.json"


def _get_context_prefix() -> str:
    """Build date/time/location context string."""
    try:
        location = json.loads(_LOCATION_PATH.read_text())
        city = location["city"]
        tz = ZoneInfo(location["timezone"])
        now = datetime.now(tz)
        time_str = now.strftime("%B %d, %Y, %I:%M %p %Z")
        return f"[{time_str} | Location: {city}]\n\n"
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        date_str = datetime.now(UTC).strftime("%B %d, %Y")
        return f"[Current date: {date_str}]\n\n"


def _inject_date_context(messages: list[dict]) -> list[dict]:
    """Prepend date/location context to the first user message for stable system prompt caching."""
    import copy

    messages = copy.deepcopy(messages)
    prefix = _get_context_prefix()

    for msg in messages:
        if msg["role"] == "user":
            content = msg["content"]
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        block["text"] = prefix + block["text"]
                        return messages
            elif isinstance(content, str):
                msg["content"] = prefix + content
                return messages
    return messages


async def run(messages: list[dict]) -> list[str]:
    """Run the agent loop. Returns list of message strings to send."""
    messages = _inject_date_context(messages)
    all_tool_calls: list[dict] = []

    for _iteration in range(MAX_ITERATIONS):
        response = await _client.messages.create(
            model=config.BEDROCK_MODEL_ID,
            max_tokens=21333,
            system=_SYSTEM_PROMPT,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice={"type": "auto"},
            thinking={"type": "adaptive"},
            extra_body={"output_config": {"effort": "high"}},
        )

        # Extract thinking blocks, text, and tool_use from response
        thinking_blocks = []
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "thinking":
                thinking_blocks.append(
                    {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
                )
            elif block.type == "redacted_thinking":
                thinking_blocks.append({"type": "redacted_thinking", "data": block.data})
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )

        if not tool_calls:
            final_text = "\n".join(text_parts)
            assistant_content = thinking_blocks + [{"type": "text", "text": final_text}]
            await conversation.add_message("assistant", assistant_content)
            logger.info(f"Agent done (iterations: {_iteration + 1}, tools: {len(all_tool_calls)})")
            parts = _split_response(final_text)
            summary = _build_tool_summary(all_tool_calls)
            if summary:
                parts.append(summary)
            return parts

        all_tool_calls.extend(tool_calls)
        tool_names = [tc["name"] for tc in tool_calls]
        logger.info(f"Iteration {_iteration + 1}: calling {tool_names}")

        # Build assistant content with thinking + text + tool_use
        assistant_content = thinking_blocks[:]
        if text_parts:
            assistant_content.append({"type": "text", "text": "\n".join(text_parts)})
        assistant_content.extend(tool_calls)

        await conversation.add_message("assistant", assistant_content)
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        tool_results = await asyncio.gather(
            *[_execute_tool(tc) for tc in tool_calls], return_exceptions=True
        )

        # Build tool result content
        result_content = []
        for tc, result in zip(tool_calls, tool_results, strict=True):
            if isinstance(result, Exception):
                logger.warning(f"Tool {tc['name']} failed: {result}")
                result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": f"Error: {result}",
                        "is_error": True,
                    }
                )
            else:
                logger.info(f"Tool {tc['name']} returned {len(result)} chars")
                result_content.append(
                    {"type": "tool_result", "tool_use_id": tc["id"], "content": result}
                )

        await conversation.add_message("user", result_content)
        messages.append({"role": "user", "content": result_content})

    # Max iterations — force final answer
    force_msg = [
        {
            "type": "text",
            "text": "You've reached the maximum number of tool calls. "
            "Provide your best answer with what you have.",
        }
    ]
    await conversation.add_message("user", force_msg)
    messages.append({"role": "user", "content": force_msg})

    response = await _client.messages.create(
        model=config.BEDROCK_MODEL_ID,
        max_tokens=21333,
        system=_SYSTEM_PROMPT,
        messages=messages,
        tools=[],
        thinking={"type": "adaptive"},
        extra_body={"output_config": {"effort": "high"}},
    )

    final_text = ""
    assistant_content = []
    for block in response.content:
        if block.type == "thinking":
            assistant_content.append(
                {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
            )
        elif block.type == "redacted_thinking":
            assistant_content.append({"type": "redacted_thinking", "data": block.data})
        elif block.type == "text":
            final_text = block.text

    assistant_content.append({"type": "text", "text": final_text})
    await conversation.add_message("assistant", assistant_content)
    parts = _split_response(final_text)
    summary = _build_tool_summary(all_tool_calls)
    if summary:
        parts.append(summary)
    parts.append("⚠️ Response may be incomplete — tool call limit reached.")
    return parts


async def _execute_tool(tool_call: dict) -> str:
    name = tool_call["name"]
    args = tool_call["input"]
    tool_fn = TOOLS.get(name)
    if not tool_fn:
        raise ValueError(f"Unknown tool: {name}")
    return await tool_fn(args)


def _build_tool_summary(tool_calls: list[dict]) -> str | None:
    if not tool_calls:
        return None

    web_searches = 0
    pages_read = 0
    reddit_searches = 0
    reddit_threads_read = 0
    beli_lookups = 0

    for tc in tool_calls:
        name = tc["name"]
        args = tc["input"]
        if name == "web_search":
            web_searches += 1
        elif name == "get_page_contents":
            urls = args.get("urls", [])
            pages_read += len(urls) if isinstance(urls, list) else 1
        elif name == "reddit_search":
            reddit_searches += 1
        elif name == "reddit_read":
            urls = args.get("urls", [])
            reddit_threads_read += len(urls) if isinstance(urls, list) else 1
        elif name == "beli_lookup":
            restaurants = args.get("restaurants", [])
            beli_lookups += len(restaurants) if isinstance(restaurants, list) else 1

    parts = []
    if web_searches:
        parts.append(f"{web_searches} web search" + ("es" if web_searches > 1 else ""))
    if pages_read:
        parts.append(f"{pages_read} page" + ("s" if pages_read > 1 else "") + " read")
    if reddit_searches:
        parts.append(f"{reddit_searches} Reddit search" + ("es" if reddit_searches > 1 else ""))
    if reddit_threads_read:
        parts.append(
            f"{reddit_threads_read} Reddit thread"
            + ("s" if reddit_threads_read > 1 else "")
            + " read"
        )
    if beli_lookups:
        parts.append(
            f"{beli_lookups} Beli lookup" + ("s" if beli_lookups > 1 else "")
        )

    if not parts:
        return None
    return f"🛠️ {', '.join(parts)}"


def _split_response(text: str) -> list[str]:
    parts = re.split(r"\n---\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text.strip()]
