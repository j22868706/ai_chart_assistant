import asyncio
import logging
import os

from dotenv import load_dotenv
from groq import AsyncGroq, RateLimitError

load_dotenv()
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set, please check the .env file")

# Initialize AsyncGroq client for non-blocking speech-to-text processing.
# An explicit timeout avoids the request hanging indefinitely on network issues.
client = AsyncGroq(api_key=GROQ_API_KEY, timeout=60.0)

# whisper-large-v3-turbo is on Groq's free tier (no credit card required)
# and is one of the cheapest/most generous Whisper offerings available.
STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# Optional ISO-639-1 language hint (e.g. "en", "zh"). Providing this improves
# transcription accuracy and speed versus letting Whisper auto-detect the
# language on every request. Leave unset/empty to auto-detect.
STT_LANGUAGE = os.environ.get("GROQ_STT_LANGUAGE", "en") or None


def _read_file_bytes(path: str) -> bytes:
    """Blocking file read, meant to be offloaded to a thread via asyncio.to_thread."""
    with open(path, "rb") as f:
        return f.read()


async def transcribe_audio(audio_file_path: str) -> str:
    """Use Groq Whisper API to transcribe an audio file into text."""
    if not os.path.exists(audio_file_path):
        raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

    try:
        # Reading the file is blocking I/O; run it in a thread so it doesn't
        # stall the FastAPI event loop while other requests are being handled.
        audio_bytes = await asyncio.to_thread(_read_file_bytes, audio_file_path)

        create_kwargs = {
            "file": (os.path.basename(audio_file_path), audio_bytes),
            "model": STT_MODEL,
            "temperature": 0.0,  # Zero temperature for deterministic, accurate transcription
        }
        if STT_LANGUAGE:
            create_kwargs["language"] = STT_LANGUAGE

        transcript = await client.audio.transcriptions.create(**create_kwargs)

        # Handle different response formats safely (object vs raw string)
        if hasattr(transcript, "text"):
            content = transcript.text
        elif isinstance(transcript, str):
            content = transcript
        else:
            content = str(transcript)

        if not content or not content.strip():
            raise RuntimeError("Transcription result is empty")

        return content.strip()

    except RateLimitError as e:
        # Groq's Whisper free tier has a daily request cap. Surface a friendly,
        # actionable message instead of a raw stack trace.
        logger.warning("Groq STT free-tier rate limit reached: %s", e)
        raise RuntimeError(
            "Groq speech-to-text free-tier usage limit reached. "
            "Please try again in a few minutes."
        ) from e
    except (FileNotFoundError, RuntimeError):
        raise
    except Exception as e:
        logger.exception("Groq STT transcription failed")
        raise RuntimeError(f"Audio transcription failed: {e}") from e