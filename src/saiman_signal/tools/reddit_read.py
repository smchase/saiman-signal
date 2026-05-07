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

    try:
        results = await _fetch_threads_via_ssh(urls)
    except Exception as e:
        return f"Error: {e}"

    output = ""
    for i, (url, result) in enumerate(results):
        if i > 0:
            output += f"\n{'=' * 60}\n\n"
        if isinstance(result, Exception):
            output += f"Error fetching {url}: {result}\n"
        else:
            output += result

    return output


async def _fetch_threads_via_ssh(urls: list[str]) -> list[tuple[str, str | Exception]]:
    """Fetch multiple Reddit JSON URLs in a single SSH session."""
    json_urls = [_normalize_url(u) for u in urls]
    # Build a shell command that curls each URL and separates output with a delimiter
    delimiter = "---REDDIT_SPLIT---"
    curl_cmds = []
    for url in json_urls:
        curl_cmds.append(
            f'curl -s -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
            f" AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0"
            f' Safari/537.36" -H "Accept: application/json" \'{url}\''
        )
    # Join with delimiter echoed between each
    shell_cmd = f'\necho "{delimiter}"\n'.join(curl_cmds)

    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o",
        "ConnectTimeout=10",
        config.REDDIT_SSH_HOST,
        "bash",
        "-c",
        shell_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"SSH failed: {stderr.decode().strip()}")

    parts = stdout.decode().split(delimiter)
    results: list[tuple[str, str | Exception]] = []
    for url, raw in zip(urls, parts, strict=True):
        try:
            data = json.loads(raw.strip())
            results.append((url, _parse_thread(url, data)))
        except Exception as e:
            results.append((url, e))
    return results


def _parse_thread(url: str, data: object) -> str:
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
