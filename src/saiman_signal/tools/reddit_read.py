import asyncio
import json

from saiman_signal import config

DEFINITION = {
    "name": "reddit_read",
    "description": (
        "Fetch full content from Reddit threads including the post and top comments."
        " Use after reddit_search to read threads in detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reddit URLs to read (max 10).",
            },
        },
        "required": ["urls"],
        "additionalProperties": False,
    },
}


async def execute(args: dict) -> str:
    urls = args["urls"]
    if isinstance(urls, str):
        urls = [urls]
    urls = urls[:10]

    output = ""
    for i, url in enumerate(urls):
        if i > 0:
            output += f"\n{'=' * 60}\n\n"
        try:
            thread_output = await _fetch_thread(url)
            output += thread_output
        except Exception as e:
            output += f"Error fetching {url}: {e}\n"

    return output


async def _fetch_thread(url: str) -> str:
    json_url = _normalize_url(url)

    # Fetch via SSH to avoid Reddit blocking AWS IPs
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o",
        "ConnectTimeout=10",
        config.REDDIT_SSH_HOST,
        "curl",
        "-s",
        "-A",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0",
        json_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"SSH curl failed: {stderr.decode().strip()}")

    data = json.loads(stdout)

    if not isinstance(data, list) or len(data) < 2:
        return f"Unexpected response format for {url}"

    post_data = data[0]["data"]["children"][0]["data"]
    title = post_data.get("title", "Untitled")
    selftext = post_data.get("selftext", "")
    author = post_data.get("author", "[deleted]")
    score = post_data.get("score", 0)
    num_comments = post_data.get("num_comments", 0)
    subreddit = post_data.get("subreddit", "unknown")

    output = f"Reddit Thread: {title}\n{'=' * 60}\n"
    output += f"r/{subreddit} | u/{author} | Score: {score} | {num_comments} comments\n\n"
    if selftext:
        output += selftext + "\n"
    else:
        output += "[Link post - no text content]\n"

    comments_data = data[1].get("data", {}).get("children", [])
    if comments_data:
        output += f"\n{'-' * 60}\nTOP COMMENTS\n{'-' * 60}\n\n"
        output += _format_comments(comments_data, depth=0, total_limit=100)

    return output


def _normalize_url(url: str) -> str:
    clean = url.split("?")[0].rstrip("/")
    if clean.endswith(".json"):
        clean = clean[:-5]
    return clean + ".json?sort=top"


def _format_comments(
    children: list, depth: int, total_limit: int, max_at_depth: int | None = None
) -> str:
    if depth >= 3 or total_limit <= 0:
        return ""

    limit = max_at_depth or (20 if depth == 0 else 5 if depth == 1 else 2)
    indent = "    " * depth
    output = ""

    for child in children[:limit]:
        if total_limit <= 0:
            break
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        author = data.get("author", "[deleted]")
        body = data.get("body", "")
        score = data.get("score", 0)

        if author == "[deleted]" and body in ("[deleted]", "[removed]"):
            continue

        total_limit -= 1
        output += f"{indent}[{score} pts] u/{author}\n"
        for line in body.split("\n"):
            output += f"{indent}{line}\n"
        output += "\n"

        replies = data.get("replies")
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            output += _format_comments(reply_children, depth + 1, total_limit)

    return output
