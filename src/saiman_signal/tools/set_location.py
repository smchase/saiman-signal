import json

from saiman_signal import config

DEFINITION = {
    "name": "set_location",
    "description": (
        "Set the user's current city and timezone. Use when the user mentions"
        " being in or traveling to a new location. You know timezones — pass"
        " the IANA timezone string (e.g. 'America/Mexico_City', 'Europe/London')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name (e.g. 'Mexico City', 'Toronto').",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone (e.g. 'America/Mexico_City').",
            },
        },
        "required": ["city", "timezone"],
        "additionalProperties": False,
    },
}


async def execute(args: dict, user_id: str) -> str:
    city = args["city"]
    timezone = args["timezone"]
    path = config.location_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"city": city, "timezone": timezone}))
    return f"Location set to {city} ({timezone})"
