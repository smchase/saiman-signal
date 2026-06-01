import os
from pathlib import Path

SIGNAL_API_URL = os.environ["SIGNAL_API_URL"]
BOT_PHONE_NUMBER = os.environ["BOT_PHONE_NUMBER"]

PRIMARY_NUMBER = os.environ["PRIMARY_NUMBER"]
SECONDARY_NUMBERS = [
    n.strip()
    for n in os.environ.get("SECONDARY_NUMBERS", "").split(",")
    if n.strip()
]
ALLOWED_NUMBERS = {PRIMARY_NUMBER} | set(SECONDARY_NUMBERS)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")

EXA_API_KEY = os.environ["EXA_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

REDDIT_SSH_HOST = os.environ["REDDIT_SSH_HOST"]

BELI_EMAIL = os.environ["BELI_EMAIL"]
BELI_PASSWORD = os.environ["BELI_PASSWORD"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
ATTACHMENTS_DIR = DATA_DIR / "attachments"
DB_PATH = DATA_DIR / "saiman.db"

SYSTEM_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "system_prompts"


def is_primary(user_id: str) -> bool:
    return user_id == PRIMARY_NUMBER or user_id == f"tg_{TELEGRAM_CHAT_ID}"


def location_path(user_id: str) -> Path:
    safe_name = user_id.replace("+", "")
    return DATA_DIR / f"location_{safe_name}.json"
