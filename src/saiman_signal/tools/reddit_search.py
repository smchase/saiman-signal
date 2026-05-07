import httpx

from saiman_signal import config

DEFINITION = {
    "name": "reddit_search",
    "description": (
        "Search Reddit for threads and discussions. Returns titles, URLs, and dates."
        " Use reddit_read to fetch full thread content and comments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query. Include subreddit names to filter.",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of threads to return (1-30). Default: 10.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


async def execute(args: dict) -> str:
    query = args["query"]
    num_results = args.get("num_results", 10)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.exa.ai/search",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.EXA_API_KEY}",
            },
            json={
                "query": query,
                "type": "auto",
                "num_results": num_results,
                "include_domains": ["reddit.com"],
            },
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return f"No Reddit threads found for: {query}"

    output = f"Reddit search results for: {query}\n{'=' * 60}\n\n"
    for i, r in enumerate(results, 1):
        output += f"[{i}] {r.get('title', 'Untitled')}\n"
        url = r["url"]
        # Extract subreddit from URL
        import re

        match = re.search(r"/r/([^/]+)/", url)
        subreddit = f"r/{match.group(1)}" if match else "reddit"
        output += f"    {subreddit}"
        if r.get("published_date"):
            output += f" | {r['published_date'][:10]}"
        output += f"\n    {url}\n\n"

    return output
