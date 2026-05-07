import os
from pathlib import Path

SIGNAL_API_URL = os.environ["SIGNAL_API_URL"]
BOT_PHONE_NUMBER = os.environ["BOT_PHONE_NUMBER"]
ALLOWED_NUMBER = os.environ["ALLOWED_NUMBER"]

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")

EXA_API_KEY = os.environ["EXA_API_KEY"]

REDDIT_SSH_HOST = os.environ.get("REDDIT_SSH_HOST", "REDACTED_SSH_HOST")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
ATTACHMENTS_DIR = DATA_DIR / "attachments"
DB_PATH = DATA_DIR / "saiman.db"
