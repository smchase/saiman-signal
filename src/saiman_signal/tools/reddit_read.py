import asyncio
import re
from datetime import UTC, datetime
from html import unescape

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
    """Fetch multiple Reddit threads as HTML via SSH proxy, parse comments."""
    old_reddit_urls = [_to_old_reddit_url(u) for u in urls]
    delimiter = "---REDDIT_SPLIT---"
    curl_cmds = []
    for url in old_reddit_urls:
        curl_cmds.append(
            f'curl -sL -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
            f" AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0"
            f' Safari/537.36" \'{url}\''
        )
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
            html = raw.strip()
            if not html or "commentarea" not in html:
                results.append((url, Exception("No comment data in response")))
            else:
                results.append((url, _parse_thread(html, url)))
        except Exception as e:
            results.append((url, e))
    return results


def _to_old_reddit_url(url: str) -> str:
    """Convert any reddit URL to old.reddit.com with sort=top."""
    clean = url.split("?")[0].rstrip("/")
    clean = re.sub(
        r"https?://(?:www\.|old\.|np\.|new\.|m\.)?reddit\.com",
        "https://old.reddit.com",
        clean,
    )
    return clean + "/?sort=top&limit=200"


def _parse_thread(html: str, url: str) -> str:
    """Parse old.reddit.com thread HTML into structured text."""
    post_match = re.search(
        r'data-type="link"[^>]*'
        r'data-author="([^"]*)"[^>]*'
        r'data-subreddit="([^"]*)"[^>]*'
        r'data-timestamp="(\d+)"[^>]*'
        r'data-comments-count="(\d+)"[^>]*'
        r'data-score="([^"]*)"',
        html,
    )

    if not post_match:
        return f"Failed to parse thread: {url}"

    author = post_match.group(1)
    subreddit = post_match.group(2)
    timestamp_ms = int(post_match.group(3))
    num_comments = post_match.group(4)
    score = post_match.group(5)

    title_match = re.search(r'<a class="title[^"]*"[^>]*>([^<]+)</a>', html)
    title = unescape(title_match.group(1)) if title_match else "Untitled"

    # Post body: look for the expando form specific to the post (thing_id=t3_)
    selftext = ""
    post_form = re.search(
        r'<form[^>]*id="form-t3_[^"]*"[^>]*>.*?'
        r'<div class="md">(.*?)</div>\s*</div>\s*</form>',
        html,
        re.DOTALL,
    )
    if post_form:
        selftext = _html_to_text(post_form.group(1))

    date_str = datetime.fromtimestamp(
        timestamp_ms / 1000, tz=UTC
    ).strftime("%b %d, %Y")

    output = f"Reddit Thread: {title}\n{'=' * 60}\n"
    output += (
        f"r/{subreddit} | u/{author} | {date_str}"
        f" | Score: {score} | {num_comments} comments\n\n"
    )
    if selftext:
        output += selftext + "\n"
    else:
        output += "[Link post - no text content]\n"

    comments = _parse_comments(html)
    if comments:
        output += f"\n{'-' * 60}\nTOP COMMENTS\n{'-' * 60}\n\n"
        output += _format_comments(comments)

    return output


def _parse_comments(html: str) -> list[dict]:
    """Extract pre-expanded comments with correct depth via stack-based parsing."""
    comment_area_idx = html.find("commentarea")
    if comment_area_idx == -1:
        return []

    comment_html = html[comment_area_idx:]

    # Build event list: div opens, div closes, siteTable opens, comments
    div_opens = [m.start() for m in re.finditer(r"<div[\s>]", comment_html)]
    div_closes = [m.start() for m in re.finditer(r"</div>", comment_html)]
    st_opens = [
        (m.start(), m.group(1))
        for m in re.finditer(
            r'<div[^>]*id="(siteTable_t[13]_[^"]+)"', comment_html
        )
    ]
    comments_pos = [
        (m.start(), m.group(1), m.group(2))
        for m in re.finditer(
            r'data-fullname="(t1_[^"]+)"[^>]*data-type="comment"'
            r'[^>]*data-author="([^"]+)"',
            comment_html,
        )
    ]

    events = []
    for pos in div_opens:
        events.append((pos, "div_open"))
    for pos in div_closes:
        events.append((pos, "div_close"))
    for pos, st_id in st_opens:
        events.append((pos, "st_open", st_id))
    for pos, fullname, author in comments_pos:
        events.append((pos, "comment", fullname, author))
    events.sort()

    # Track div depth and siteTable stack to find each comment's parent
    div_depth = 0
    st_stack: list[tuple[str, int]] = []
    comment_parents: dict[str, str | None] = {}

    for event in events:
        etype = event[1]
        if etype == "div_open":
            div_depth += 1
        elif etype == "div_close":
            while st_stack and st_stack[-1][1] >= div_depth:
                st_stack.pop()
            div_depth -= 1
        elif etype == "st_open":
            st_stack.append((event[2], div_depth))
        elif etype == "comment":
            fullname = event[2]
            comment_parents[fullname] = st_stack[-1][0] if st_stack else None

    # Resolve depths from parent chain
    depth_memo: dict[str, int] = {}

    def get_depth(fullname: str) -> int:
        if fullname in depth_memo:
            return depth_memo[fullname]
        st = comment_parents.get(fullname)
        if not st or st.startswith("siteTable_t3_"):
            depth_memo[fullname] = 0
            return 0
        parent_fn = st.replace("siteTable_", "")
        d = get_depth(parent_fn) + 1
        depth_memo[fullname] = d
        return d

    # Extract comment content using data-fullname as anchor (it precedes data-type)
    comment_anchors = [
        (m.start(), m.group(1))
        for m in re.finditer(
            r'data-fullname="(t1_[^"]+)"[^>]*data-type="comment"',
            comment_html,
        )
    ]

    results = []
    for i, (start, fullname) in enumerate(comment_anchors):
        end = (
            comment_anchors[i + 1][0]
            if i + 1 < len(comment_anchors)
            else start + 10000
        )
        chunk = comment_html[start:end]

        author_m = re.search(r'data-author="([^"]+)"', chunk)
        score_m = re.search(
            r'<span class="score unvoted" title="([^"]*)"', chunk
        )
        if not score_m:
            score_m = re.search(
                r'<span class="score [^"]*" title="([^"]*)"', chunk
            )
        body_m = re.search(
            r'<div class="usertext-body[^"]*"[^>]*>\s*<div class="md">'
            r"(.*?)</div>\s*</div>\s*</form>",
            chunk,
            re.DOTALL,
        )

        if not (author_m and body_m):
            continue

        author = author_m.group(1)
        body = _html_to_text(body_m.group(1))

        if author == "[deleted]" and body.strip() in ("[deleted]", "[removed]"):
            continue

        score_str = score_m.group(1) if score_m else "?"
        depth = get_depth(fullname)

        results.append({
            "author": author,
            "score": score_str,
            "body": body,
            "depth": depth,
        })

    return results


def _format_comments(comments: list[dict]) -> str:
    """Format comments with limits: 20 top-level, 5 depth-1, 2 depth-2 per parent."""
    output = ""
    top_count = 0
    depth1_count = 0
    depth2_count = 0

    for comment in comments:
        depth = comment["depth"]

        if depth == 0:
            if top_count >= 20:
                continue
            top_count += 1
            depth1_count = 0
            depth2_count = 0
        elif depth == 1:
            if depth1_count >= 5:
                continue
            depth1_count += 1
            depth2_count = 0
        elif depth == 2:
            if depth2_count >= 2:
                continue
            depth2_count += 1
        else:
            continue

        indent = "    " * depth
        output += f"{indent}[{comment['score']} pts] u/{comment['author']}\n"
        for line in comment["body"].split("\n"):
            if line.strip():
                output += f"{indent}{line}\n"
        output += "\n"

    return output


def _html_to_text(html_content: str) -> str:
    """Convert HTML comment body to plain text."""
    text = html_content
    # Block quotes
    text = re.sub(r"<blockquote>", "> ", text)
    text = re.sub(r"</blockquote>", "\n", text)
    # Paragraphs and line breaks
    text = re.sub(r"<p>", "", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    # Lists
    text = re.sub(r"<li>", "- ", text)
    text = re.sub(r"</li>", "\n", text)
    # Links: use text if available, otherwise show URL
    text = re.sub(
        r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        lambda m: re.sub(r"<[^>]+>", "", m.group(2)).strip() or m.group(1),
        text,
        flags=re.DOTALL,
    )
    # Inline formatting
    text = re.sub(r"<(?:strong|b)>(.*?)</(?:strong|b)>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<(?:em|i)>(.*?)</(?:em|i)>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<code>([^<]*)</code>", r"`\1`", text)
    # Code blocks
    text = re.sub(r"<pre>(.*?)</pre>", r"\1", text, flags=re.DOTALL)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
