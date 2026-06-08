import re
from html import unescape
from xml.etree import ElementTree

import httpx

DEFINITION = {
    "name": "reddit_search",
    "description": (
        "Search Reddit for threads and discussions. Returns titles, URLs, dates,"
        " and body snippets. Use reddit_read to fetch full thread content and comments."
        " This is keyword-based search (not semantic) — use specific terms that would"
        " appear in post titles/bodies. Scoping to subreddit(s) is recommended for"
        " better relevance."
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
                    " (e.g. ['askTO', 'toronto']). Omit for global search."
                ),
            },
            "sort": {
                "type": "string",
                "description": "Sort order. Default: relevance.",
                "enum": ["relevance", "new", "top", "comments"],
            },
            "time_filter": {
                "type": "string",
                "description": "Time window. Default: all.",
                "enum": ["all", "year", "month", "week", "day"],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_NS = {"atom": "http://www.w3.org/2005/Atom"}
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


async def execute(args: dict) -> str:
    query = args["query"]
    subreddits = args.get("subreddits")
    sort = args.get("sort")
    time_filter = args.get("time_filter")

    params = {"q": query, "type": "link"}
    if sort:
        params["sort"] = sort
    if time_filter:
        params["t"] = time_filter

    if subreddits:
        sub_path = "+".join(s.strip().strip("/") for s in subreddits)
        url = f"https://www.reddit.com/r/{sub_path}/search.rss"
        params["restrict_sr"] = "on"
    else:
        url = "https://www.reddit.com/search.rss"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 429:
            raise RuntimeError("rate limited")
        response.raise_for_status()

    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError:
        raise RuntimeError("failed to parse response")

    entries = root.findall("atom:entry", _NS)
    if not entries:
        return ""

    scope = f"r/{'+'.join(subreddits)}" if subreddits else "all of Reddit"
    output = f"Reddit search ({scope}): {query}\n{'=' * 60}\n\n"

    for i, entry in enumerate(entries, 1):
        title = entry.find("atom:title", _NS)
        link = entry.find("atom:link", _NS)
        updated = entry.find("atom:updated", _NS)
        author_el = entry.find("atom:author/atom:name", _NS)
        cat_el = entry.find("atom:category", _NS)
        content_el = entry.find("atom:content", _NS)

        title_text = title.text if title is not None else "Untitled"
        link_href = link.get("href", "") if link is not None else ""
        date_str = updated.text[:10] if updated is not None and updated.text else ""
        author = author_el.text if author_el is not None else "?"
        subreddit = cat_el.get("label", "?") if cat_el is not None else "?"

        snippet = ""
        if content_el is not None and content_el.text:
            text = re.sub(r"<[^>]+>", " ", unescape(content_el.text))
            text = re.sub(r"\s+", " ", text).strip()
            text = re.sub(r"\s*submitted by /u/\S+.*$", "", text)
            if text:
                snippet = text[:200] + "..." if len(text) > 200 else text

        output += f"[{i}] {title_text}\n"
        output += f"    {subreddit} | {author} | {date_str}\n"
        output += f"    {link_href}\n"
        if snippet:
            output += f"    {snippet}\n"
        output += "\n"

    return output
