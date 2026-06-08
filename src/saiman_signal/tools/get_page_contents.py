import httpx

from saiman_signal import config

DEFINITION = {
    "name": "get_page_contents",
    "description": (
        "Fetch full content from specific URLs. Use when you have a URL and need to read it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs to fetch content from (max 10).",
            },
            "max_characters": {
                "type": "integer",
                "description": "Max characters per page. Default: 5000.",
            },
            "livecrawl": {
                "type": "string",
                "description": "Content freshness. Default: 'fallback'.",
                "enum": ["fallback", "preferred", "always"],
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
    max_characters = args.get("max_characters", 5000)
    livecrawl = args.get("livecrawl", "fallback")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.exa.ai/contents",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.EXA_API_KEY}",
            },
            json={
                "urls": urls[:10],
                "text": {"max_characters": max_characters},
                "livecrawl": livecrawl,
            },
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return ""

    output = f"Page contents for {len(urls)} URL(s):\n{'=' * 60}\n\n"
    for i, r in enumerate(results, 1):
        output += f"[{i}] {r.get('title', 'Untitled')}\n"
        output += f"URL: {r['url']}\n"
        if r.get("author"):
            output += f"Author: {r['author']}\n"
        if r.get("published_date"):
            output += f"Published: {r['published_date']}\n"
        text = r.get("text", "")
        output += f"\n{text}\n" if text else "\n[No text content available]\n"
        output += f"\n{'-' * 60}\n\n"

    return output
