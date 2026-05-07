from pathlib import Path

import httpx

from saiman_signal import config


async def transcribe(audio_path: Path) -> str:
    """Transcribe an audio file using OpenAI GPT-4o audio transcription."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            data={"model": "gpt-4o-transcribe"},
            files={"file": (audio_path.name, audio_path.read_bytes())},
        )
        response.raise_for_status()
        return response.json()["text"]
