import httpx

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

_USER_AGENT = f"saiman-signal/0.1 by {config.REDDIT_USERNAME}"

_access_token: str | None = None


async def _get_token(client: httpx.AsyncClient) -> str:
    global _access_token
    if _access_token:
        return _access_token

    response = await client.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    _access_token = response.json()["access_token"]
    return _access_token


async def execute(args: dict) -> str:
    urls = args["urls"]
    if isinstance(urls, str):
        urls = [urls]
    urls = urls[:10]

    output = ""
    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _get_token(client)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT}

        for i, url in enumerate(urls):
            if i > 0:
                output += f"\n{'=' * 60}\n\n"
            try:
                thread_output = await _fetch_thread(client, headers, url)
                output += thread_output
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    # Token expired, refresh and retry
                    global _access_token
                    _access_token = None
                    token = await _get_token(client)
                    headers["Authorization"] = f"Bearer {token}"
                    try:
                        thread_output = await _fetch_thread(client, headers, url)
                        output += thread_output
                    except Exception as e2:
                        output += f"Error fetching {url}: {e2}\n"
                else:
                    output += f"Error fetching {url}: {e}\n"
            except Exception as e:
                output += f"Error fetching {url}: {e}\n"

    return output


async def _fetch_thread(client: httpx.AsyncClient, headers: dict, url: str) -> str:
    api_path = _url_to_api_path(url)
    response = await client.get(
        f"https://oauth.reddit.com{api_path}",
        headers=headers,
        params={"sort": "top", "limit": 100},
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list) or len(data) < 2:
        return f"Unexpected response format for {url}"

    # Parse post
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

    # Parse comments
    comments_data = data[1].get("data", {}).get("children", [])
    if comments_data:
        output += f"\n{'-' * 60}\nTOP COMMENTS\n{'-' * 60}\n\n"
        output += _format_comments(comments_data, depth=0, total_limit=100)

    return output


def _url_to_api_path(url: str) -> str:
    """Convert a reddit URL to an API path for oauth.reddit.com."""
    # Remove domain, query params, trailing slash
    for prefix in ("https://www.reddit.com", "https://reddit.com", "https://old.reddit.com"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
            break
    path = url.split("?")[0].rstrip("/")
    if path.endswith(".json"):
        path = path[:-5]
    return path


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

        # Recurse into replies
        replies = data.get("replies")
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            output += _format_comments(reply_children, depth + 1, total_limit)

    return output
