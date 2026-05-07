import asyncio
import logging
import re
from datetime import UTC, datetime

from anthropic import AsyncAnthropicBedrock

from saiman_signal import config, conversation
from saiman_signal.tools import TOOL_DEFINITIONS, TOOLS

logger = logging.getLogger(__name__)

_client = AsyncAnthropicBedrock(aws_region=config.AWS_REGION)

_SYSTEM_PROMPT_PATH = __file__.replace("agent.py", "system_prompt.txt")

MAX_ITERATIONS = 20


def _build_system_prompt() -> str:
    with open(_SYSTEM_PROMPT_PATH) as f:
        base = f.read()
    date_str = datetime.now(UTC).strftime("%B %d, %Y")
    return f"Today's date is {date_str}.\n\n{base}"


async def run(messages: list[dict]) -> list[str]:
    """Run the agent loop. Returns list of message strings to send."""
    system_prompt = _build_system_prompt()
    all_tool_calls: list[dict] = []

    for _iteration in range(MAX_ITERATIONS):
        response = await _client.messages.create(
            model=config.BEDROCK_MODEL_ID,
            max_tokens=21333,
            system=system_prompt,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice={"type": "auto"},
            thinking={"type": "enabled", "budget_tokens": 10000},
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
        system=system_prompt,
        messages=messages,
        tools=[],
        thinking={"type": "enabled", "budget_tokens": 10000},
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

    if not parts:
        return None
    return f"🛠️ {', '.join(parts)}"


def _split_response(text: str) -> list[str]:
    parts = re.split(r"\n---\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text.strip()]
