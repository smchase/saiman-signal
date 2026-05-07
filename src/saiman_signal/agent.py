import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from anthropic import AsyncAnthropicBedrock

from saiman_signal import config, conversation
from saiman_signal.tools import TOOLS, TOOL_DEFINITIONS

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

    for iteration in range(MAX_ITERATIONS):
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
            # Final response — store and return
            final_text = "\n".join(text_parts)
            assistant_content = thinking_blocks + [{"type": "text", "text": final_text}]
            await conversation.add_message("assistant", assistant_content)
            return _split_response(final_text)

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
        for tc, result in zip(tool_calls, tool_results):
            if isinstance(result, Exception):
                result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": f"Error: {result}",
                        "is_error": True,
                    }
                )
            else:
                result_content.append(
                    {"type": "tool_result", "tool_use_id": tc["id"], "content": result}
                )

        await conversation.add_message("user", result_content)
        messages.append({"role": "user", "content": result_content})

    # Max iterations — force final answer
    force_msg = [
        {
            "type": "text",
            "text": "You've reached the maximum number of tool calls. Provide your best answer with what you have.",
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
    return _split_response(final_text)


async def _execute_tool(tool_call: dict) -> str:
    name = tool_call["name"]
    args = tool_call["input"]
    tool_fn = TOOLS.get(name)
    if not tool_fn:
        raise ValueError(f"Unknown tool: {name}")
    return await tool_fn(args)


def _split_response(text: str) -> list[str]:
    parts = re.split(r"\n---\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text.strip()]
