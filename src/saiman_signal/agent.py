import asyncio
import json
import logging
import re

from anthropic import AsyncAnthropicBedrock

from saiman_signal import config, conversation
from saiman_signal.tools import TOOL_DEFINITIONS, TOOLS, TOOLS_WITH_USER_ID

logger = logging.getLogger(__name__)


class EmptyResponseError(Exception):
    """Raised when the model returns no text (e.g. safety-filtered)."""

    def __init__(self, stop_reason: str | None = None):
        self.stop_reason = stop_reason
        super().__init__(f"empty response (stop_reason={stop_reason})")

_client = AsyncAnthropicBedrock(aws_region=config.AWS_REGION, timeout=300.0)

MAX_ITERATIONS = 20


def _load_prompt_file(name: str) -> str:
    path = config.SYSTEM_PROMPTS_DIR / name
    with open(path) as f:
        return f.read().strip()


def _get_location(user_id: str) -> tuple[str | None, str | None]:
    """Read persisted location. Returns (city, timezone) or (None, None)."""
    try:
        data = json.loads(config.location_path(user_id).read_text())
        return data["city"], data["timezone"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None, None


def _build_system_prompt(user_id: str) -> str:
    """System prompt with user profile and location appended."""
    base = _load_prompt_file("base.txt")

    if config.is_primary(user_id):
        preamble = _load_prompt_file("primary.txt")
    else:
        preamble = _load_prompt_file("secondary.txt")

    prompt = f"{preamble}\n\n{base}"

    city, tz = _get_location(user_id)
    if city:
        prompt += f"\n\n[User location: {city} ({tz})]"

    return prompt


async def run(messages: list[dict], user_id: str) -> list[str]:
    """Run the agent loop. Returns list of message strings to send."""
    system_prompt = _build_system_prompt(user_id)
    all_tool_calls: list[dict] = []
    hard_errors: set[str] = set()
    tool_call_counts: dict[str, int] = {}
    tool_empty_counts: dict[str, int] = {}

    for _iteration in range(MAX_ITERATIONS):
        response = await _client.messages.create(
            model=config.BEDROCK_MODEL_ID,
            max_tokens=21333,
            system=system_prompt,
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
            final_text = "\n".join(text_parts).strip()
            if not final_text:
                logger.warning(
                    f"Model returned empty text — stop_reason={response.stop_reason}, "
                    f"content_types={[b.type for b in response.content]}"
                )
                raise EmptyResponseError(response.stop_reason)
            assistant_content = thinking_blocks + [{"type": "text", "text": final_text}]
            await conversation.add_message(user_id, "assistant", assistant_content)
            logger.info(f"Agent done (iterations: {_iteration + 1}, tools: {len(all_tool_calls)})")
            parts = _split_response(final_text)
            summary = _build_tool_summary(all_tool_calls)
            if summary:
                parts.append(summary)
            location_notice = _build_location_notice(all_tool_calls)
            if location_notice:
                parts.append(location_notice)
            parts.extend(_build_warnings(hard_errors, tool_call_counts, tool_empty_counts))
            return parts

        all_tool_calls.extend(tool_calls)
        tool_names = [tc["name"] for tc in tool_calls]
        logger.info(f"Iteration {_iteration + 1}: calling {tool_names}")

        # Build assistant content with thinking + text + tool_use
        assistant_content = thinking_blocks[:]
        joined_text = "\n".join(text_parts).strip()
        if joined_text:
            assistant_content.append({"type": "text", "text": joined_text})
        assistant_content.extend(tool_calls)

        await conversation.add_message(user_id, "assistant", assistant_content)
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        tool_results = await asyncio.gather(
            *[_execute_tool(tc, user_id) for tc in tool_calls], return_exceptions=True
        )

        # Build tool result content
        result_content = []
        for tc, result in zip(tool_calls, tool_results, strict=True):
            name = tc["name"]
            tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
            if isinstance(result, Exception):
                logger.warning(f"Tool {name} failed: {result}")
                hard_errors.add(f"{name}: {result}")
                result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": f"Error: {result}",
                        "is_error": True,
                    }
                )
            elif result == "":
                logger.info(f"Tool {name} returned empty")
                tool_empty_counts[name] = tool_empty_counts.get(name, 0) + 1
                result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": "No results found.",
                    }
                )
            else:
                logger.info(f"Tool {name} returned {len(result)} chars")
                result_content.append(
                    {"type": "tool_result", "tool_use_id": tc["id"], "content": result}
                )

        await conversation.add_message(user_id, "user", result_content)
        messages.append({"role": "user", "content": result_content})

    # Max iterations — force final answer
    force_msg = [
        {
            "type": "text",
            "text": "You've reached the maximum number of tool calls. "
            "Provide your best answer with what you have.",
        }
    ]
    await conversation.add_message(user_id, "user", force_msg)
    messages.append({"role": "user", "content": force_msg})

    response = await _client.messages.create(
        model=config.BEDROCK_MODEL_ID,
        max_tokens=21333,
        system=system_prompt,
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

    final_text = final_text.strip()
    if not final_text:
        raise EmptyResponseError(response.stop_reason)

    assistant_content.append({"type": "text", "text": final_text})
    await conversation.add_message(user_id, "assistant", assistant_content)
    parts = _split_response(final_text)
    summary = _build_tool_summary(all_tool_calls)
    if summary:
        parts.append(summary)
    location_notice = _build_location_notice(all_tool_calls)
    if location_notice:
        parts.append(location_notice)
    parts.extend(_build_warnings(hard_errors, tool_call_counts, tool_empty_counts))
    parts.append("⚠️ Response may be incomplete — tool call limit reached.")
    return parts


async def _execute_tool(tool_call: dict, user_id: str) -> str:
    name = tool_call["name"]
    args = tool_call["input"]
    tool_fn = TOOLS.get(name)
    if not tool_fn:
        raise ValueError(f"Unknown tool: {name}")
    if name in TOOLS_WITH_USER_ID:
        return await tool_fn(args, user_id)
    return await tool_fn(args)


def _build_warnings(
    hard_errors: set[str],
    tool_call_counts: dict[str, int],
    tool_empty_counts: dict[str, int],
) -> list[str]:
    warnings = []
    for msg in sorted(hard_errors):
        warnings.append(f"⚠️ {msg}")
    for name, empty in tool_empty_counts.items():
        if empty == tool_call_counts.get(name, 0):
            warnings.append(f"⚠️ {name}: all calls returned empty")
    return warnings


def _build_location_notice(tool_calls: list[dict]) -> str | None:
    for tc in tool_calls:
        if tc["name"] == "set_location":
            city = tc["input"].get("city", "")
            tz = tc["input"].get("timezone", "")
            return f"📍 Location set to {city} ({tz})"
    return None


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
