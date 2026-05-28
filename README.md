# saiman-signal

Multi-user personal research assistant on Signal. Combines Claude Opus 4.6 (extended thinking) with web/Reddit research tools, accessible via Signal DM. Supports multiple users with isolated conversations, per-user system prompt profiles, and per-user location/timezone.

## Architecture

- **Bot** (`src/saiman_signal/bot.py`): WebSocket listener for Signal messages, per-user cancel-and-restart orchestration, typing indicators
- **Agent** (`src/saiman_signal/agent.py`): LLM loop with tool execution (max 20 iterations), adaptive thinking, per-user system prompt construction
- **Tools**: Web search (Exa), page reading, Reddit search/read (via SSH proxy), Beli restaurant lookup, location setting
- **Conversation** (`src/saiman_signal/conversation.py`): SQLite persistence with per-user isolation and context pruning
- **Transcription** (`src/saiman_signal/transcription.py`): Voice memo transcription via OpenAI GPT-4o
- **Signal CLI REST API**: Docker container handling Signal protocol

## Multi-User

Users are identified by phone number. The primary user (creator) gets a tailored system prompt profile; secondary users get a generic family profile. Each user has:
- Isolated conversation history (same DB, partitioned by phone number)
- Independent cancel-and-restart (one user's messages don't interrupt another's)
- Per-user location/timezone (`data/location_{phone}.json`)
- Per-user system prompt preamble (`system_prompts/primary.txt` or `secondary.txt`)

System prompts live in a gitignored `system_prompts/` directory:
- `base.txt` — shared prompt (research approach, response style, tools)
- `primary.txt` — creator's preamble
- `secondary.txt` — family members' preamble

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker
- AWS credentials configured (for Bedrock)
- SSH key for Reddit proxy (access to `REDDIT_SSH_HOST`)

## Local Development

```bash
uv sync
cp .env.example .env  # fill in real values
mkdir -p system_prompts
# Create system_prompts/base.txt, primary.txt, secondary.txt
docker compose up -d  # start Signal CLI REST API
uv run python -m saiman_signal
```

## EC2 Setup

### Instance

- AMI: Ubuntu 24.04 (ARM)
- Instance type: t4g.small
- Security group: SSH (22) inbound

### Initial Setup

```bash
# Install dependencies
sudo apt update && sudo apt install -y docker.io docker-compose-v2
curl -LsSf https://astral.sh/uv/install.sh | sh

# Docker permissions
sudo usermod -aG docker $USER
# Log out and back in

# Clone repo
git clone git@github.com:smchase/saiman-signal.git ~/saiman-signal
cd ~/saiman-signal

# Environment
cp .env.example .env
# Fill in real values

# System prompts (not in repo — create manually)
mkdir -p system_prompts
# Create base.txt, primary.txt, secondary.txt

# Signal CLI REST API
docker compose up -d

# Install systemd service
sudo cp saiman-signal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now saiman-signal
```

### AWS Credentials

The EC2 instance uses an IAM instance profile with Bedrock access. No AWS credentials file needed — the SDK picks up the role automatically.

Required IAM permissions:
```json
{
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel*",
  "Resource": "*"
}
```

### SSH Key for Reddit Proxy

The bot proxies Reddit requests through a university server to avoid AWS IP blocks. It SSHs to `REDDIT_SSH_HOST` using the EC2 instance's default SSH key (`~/.ssh/id_ed25519`).

Setup: ensure the EC2 instance's public key is in `~/.ssh/authorized_keys` on the target server.

## Deployment

Push to `main` triggers automatic deployment via GitHub Actions:

1. SSH into EC2
2. `git pull`
3. `systemctl restart saiman-signal`

### CI/CD Secrets (GitHub)

- `EC2_HOST`: EC2 public IP
- `SSH_KEY`: Private key for EC2 SSH access

### Manual Deploy

```bash
ssh ubuntu@$EC2_HOST
cd ~/saiman-signal
git pull
sudo systemctl restart saiman-signal
```

## Signal Registration

Signal CLI REST API handles the Signal protocol. Registration is done once:

```bash
# Register number (replace with real number)
curl -X POST "http://localhost:8080/v1/register/+1XXXXXXXXXX" -H "Content-Type: application/json" -d '{"captcha": "signalcaptcha://...", "use_voice": true}'

# Verify with code received via voice call
curl -X POST "http://localhost:8080/v1/register/+1XXXXXXXXXX/verify/XXXXXX"
```

Captcha tokens obtained from https://signalcaptchas.org/registration/generate.html

## Monitoring

```bash
# Bot logs
sudo journalctl -u saiman-signal -f

# Signal CLI logs
docker compose logs -f signal-cli
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SIGNAL_API_URL` | Signal CLI REST API URL (default: `http://localhost:8080`) |
| `BOT_PHONE_NUMBER` | Bot's registered Signal number |
| `PRIMARY_NUMBER` | Creator's phone number (gets primary system prompt profile) |
| `SECONDARY_NUMBERS` | Comma-separated additional allowed numbers (get secondary profile) |
| `AWS_REGION` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | Claude model ID |
| `EXA_API_KEY` | Exa search API key |
| `OPENAI_API_KEY` | OpenAI key (for voice transcription) |
| `REDDIT_SSH_HOST` | SSH host for Reddit proxy (e.g. `user@host`) |
| `BELI_EMAIL` | Beli account email |
| `BELI_PASSWORD` | Beli account password |
| `EC2_HOST` | EC2 public IP (used by CI/CD and manual deploy) |
| `DATA_DIR` | Data directory for SQLite DB and attachments |
