import httpx

from saiman_signal import config

DEFINITION = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Reddit is excluded from results"
        " - use reddit_search for Reddit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results (1-50). Default: 5.",
            },
            "max_characters": {
                "type": "integer",
                "description": "Max characters per result. Default: 2000.",
            },
            "search_type": {
                "type": "string",
                "description": "Search speed/quality. Default: 'auto'.",
                "enum": ["fast", "auto", "deep"],
            },
            "livecrawl": {
                "type": "string",
                "description": "Content freshness. Default: 'fallback'.",
                "enum": ["fallback", "preferred", "always"],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


async def execute(args: dict) -> str:
    query = args["query"]
    num_results = args.get("num_results", 5)
    max_characters = args.get("max_characters", 2000)
    search_type = args.get("search_type", "auto")
    livecrawl = args.get("livecrawl", "fallback")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.exa.ai/search",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.EXA_API_KEY}",
            },
            json={
                "query": query,
                "type": search_type,
                "num_results": num_results,
                "contents": {"text": {"max_characters": max_characters}},
                "livecrawl": livecrawl,
            },
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return f"No results found for: {query}"

    output = f"Search results for: {query}\n{'=' * 60}\n\n"
    for i, r in enumerate(results, 1):
        output += f"[{i}] {r.get('title', 'Untitled')}\n"
        output += f"URL: {r['url']}\n"
        if r.get("author"):
            output += f"Author: {r['author']}\n"
        if r.get("published_date"):
            output += f"Published: {r['published_date']}\n"
        if r.get("text"):
            output += f"\n{r['text']}\n"
        output += f"\n{'-' * 60}\n\n"

    return output
