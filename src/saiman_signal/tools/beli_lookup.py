import asyncio
import time

import httpx

from saiman_signal import config

DEFINITION = {
    "name": "beli_lookup",
    "description": (
        "Look up restaurants on Beli to get community ratings, score distributions,"
        " pricing, cuisines, hours, and top dish recommendations."
        " Uses autocomplete search — pass specific restaurant names for best results."
        " The matched name and address are included so you can verify correctness."
        " If a match looks wrong, retry with a more specific name (add neighborhood or street)."
        " The distribution is 20 buckets (0-10 scale). Pay attention to the shape:"
        " a steep upward slope at the right end (e.g. [... 7, 13, 59]) means"
        " the place is exceptional. A flat or bell-shaped distribution is less"
        " impressive even if the average looks decent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": (
                    "City name appended to each search"
                    " (e.g. 'Toronto', 'London', 'Montreal')."
                ),
            },
            "restaurants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restaurant names to look up (max 20).",
            },
        },
        "required": ["city", "restaurants"],
        "additionalProperties": False,
    },
}

_BASE = "https://backoffice-service-split-t57o3dxfca-nn.a.run.app"
_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "capacitor://localhost",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
}
_USER_ID = "0acbda57-db5b-4903-9333-b41e71447a4f"

_token: str | None = None
_token_expires: float = 0


async def _get_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires
    if _token and time.time() < _token_expires:
        return _token
    r = await client.post(
        f"{_BASE}/api/token/",
        headers=_HEADERS,
        json={"email": config.BELI_EMAIL, "password": config.BELI_PASSWORD},
    )
    r.raise_for_status()
    _token = r.json()["access"]
    _token_expires = time.time() + 1100  # ~18 min (tokens last 20)
    return _token


async def _lookup_one(client: httpx.AsyncClient, name: str, city: str, token: str) -> str:
    h = {**_HEADERS, "authorization": f"Bearer {token}"}

    # Search
    r = await client.get(
        f"{_BASE}/api/search-app/",
        headers=h,
        params={"term": f"{name} {city}", "user": _USER_ID, "city": "Toronto, ON"},
    )
    if r.status_code != 200:
        return f"## {name}\nNOT FOUND (search error {r.status_code})"

    preds = r.json().get("predictions", [])
    if not preds:
        return f"## {name}\nNOT FOUND"

    pred = preds[0]
    biz_id = pred.get("business")
    sf = pred.get("structured_formatting", {})
    matched_name = sf.get("main_text", "?")
    address = sf.get("secondary_text", "?")

    if not biz_id:
        return f"## {name}\nMatch: {matched_name} — {address}\nNOT ON BELI (no ratings data)"

    # Fetch detail, score, count, histogram, dishes in parallel
    detail_req = client.get(
        f"{_BASE}/api/business/",
        headers=h,
        params={"id": biz_id, "from_business_page": "true"},
    )
    avg_req = client.get(
        f"{_BASE}/api/databusinessfloat-sparse/",
        headers=h,
        params={"business": biz_id, "field__name": "AVGBUSINESSSCORE"},
    )
    count_req = client.get(f"{_BASE}/api/business-count-rated/{biz_id}/", headers=h)
    hist_req = client.get(f"{_BASE}/api/business-histogram-data/{biz_id}/", headers=h)
    dish_req = client.get(
        f"{_BASE}/api/dish-rec/",
        headers=h,
        params={"business": biz_id, "version": "9.0.3", "menu_vibes": "true"},
    )

    r_detail, r_avg, r_count, r_hist, r_dish = await asyncio.gather(
        detail_req, avg_req, count_req, hist_req, dish_req
    )

    # Parse detail
    biz_data = {}
    if r_detail.status_code == 200:
        results = r_detail.json().get("results", [])
        if results:
            biz_data = results[0]

    # Parse score
    avg_score = None
    if r_avg.status_code == 200:
        avg_results = r_avg.json().get("results", [])
        if avg_results:
            avg_score = avg_results[0].get("value")

    # Parse count
    count_rated = None
    if r_count.status_code == 200:
        count_rated = r_count.json().get("count")

    # Parse histogram
    histogram = None
    if r_hist.status_code == 200:
        buckets = r_hist.json().get("config", {}).get("buckets", [])
        if buckets:
            counts = [b["count"] for b in buckets]
            total = sum(counts)
            if total > 0:
                histogram = [round(c / total * 100) for c in counts]

    # Parse dishes
    dishes = []
    if r_dish.status_code == 200:
        for d in r_dish.json().get("results", []):
            if d.get("rec_type") == 1:
                meta = d.get("meta", "")
                count_str = meta.replace(" recommended", "") if meta else ""
                dishes.append(f"{d['name']} ({count_str})")

    # Format output
    neighborhood = biz_data.get("neighborhood")
    loc_str = f"{address} ({neighborhood})" if neighborhood else address
    section = f"## {matched_name}\n"
    section += f"Match: {matched_name} — {loc_str}\n"

    score_str = f"{avg_score:.1f}/10" if avg_score else "unrated"
    count_str = f"{count_rated:,}" if count_rated else "0"
    section += f"Score: {score_str} | {count_str} ratings\n"

    if histogram:
        section += f"Distribution (%): {histogram}\n"

    cuisines = biz_data.get("cuisines", [])
    price = biz_data.get("price_key", "?")
    if cuisines:
        section += f"Cuisines: {', '.join(cuisines)} | Price: {price}\n"

    hours_config = biz_data.get("businessHoursConfig")
    if hours_config and "hours" in hours_config:
        parts = []
        for day_info in hours_config["hours"]:
            day = day_info["day"][:3]
            hrs = ", ".join(day_info["hours"])
            parts.append(f"{day} {hrs}")
        section += f"Hours: {' | '.join(parts)}\n"

    if dishes:
        section += f"Top dishes: {', '.join(dishes[:5])}\n"

    return section


async def execute(args: dict) -> str:
    city = args["city"]
    restaurants = args["restaurants"][:20]

    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _get_token(client)
        results = await asyncio.gather(
            *[_lookup_one(client, name, city, token) for name in restaurants]
        )

    return "\n---\n".join(results)
