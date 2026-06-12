import re
from html import unescape

import httpx

from saiman_signal import config

DEFINITION = {
    "name": "reddit_search",
    "description": (
        "Search Reddit for threads and discussions via Brave Search."
        " Returns titles, URLs, dates, and snippets."
        " Use reddit_read to fetch full thread content and comments."
        " Subreddit scoping works by adding subreddit names to the query"
        " (soft filter, not exact). Recommended for better relevance."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "subreddits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subreddit(s) to scope the search"
                    " (e.g. ['askTO', 'toronto']). Added to query as keywords."
                ),
            },
            "freshness": {
                "type": "string",
                "description": (
                    "Filter by recency. Default: no limit."
                ),
                "enum": ["pd", "pw", "pm", "py"],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


async def execute(args: dict) -> str:
    query = args["query"]
    subreddits = args.get("subreddits")
    freshness = args.get("freshness")

    search_query = query
    if subreddits:
        search_query += " " + " ".join(subreddits)
    search_query += " site:reddit.com"

    params = {"q": search_query, "count": 20}
    if freshness:
        params["freshness"] = freshness

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": config.BRAVE_API_KEY,
            },
        )
        if response.status_code == 429:
            raise RuntimeError("rate limited")
        response.raise_for_status()

    data = response.json()
    results = data.get("web", {}).get("results", [])

    if not results:
        return ""

    scope = f"r/{'+'.join(subreddits)}" if subreddits else "all of Reddit"
    output = f"Reddit search ({scope}): {query}\n{'=' * 60}\n\n"

    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        description = r.get("description", "")
        age = r.get("age", "")

        sub_match = re.search(r"/r/([^/]+)/", url)
        subreddit = f"r/{sub_match.group(1)}" if sub_match else "reddit"

        snippet = unescape(re.sub(r"<[^>]+>", "", description)).strip()
        if snippet:
            snippet = snippet[:200] + "..." if len(snippet) > 200 else snippet

        output += f"[{i}] {title}\n"
        output += f"    {subreddit}"
        if age:
            output += f" | {age}"
        output += f"\n    {url}\n"
        if snippet:
            output += f"    {snippet}\n"
        output += "\n"

    return output
