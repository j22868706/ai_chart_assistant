import os
import logging
from groq import AsyncGroq, RateLimitError
from dotenv import load_dotenv
from utils.prompts import CLINICAL_SOAP_PROMPT

load_dotenv()
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set, please check the .env file")

# Use AsyncGroq to prevent the synchronous client from blocking the FastAPI event loop
client = AsyncGroq(api_key=GROQ_API_KEY)

# openai/gpt-oss-120b is Groq's recommended replacement for the deprecated
# llama-3.3-70b-versatile model (deprecated 2026-06-17), and stays within
# Groq's no-credit-card free tier for typical personal-bot usage.
LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "openai/gpt-oss-120b")


async def generate_soap_note(transcript: str) -> str:
    """
    Use Groq LLM to convert a consultation transcript into a structured SOAP medical note.
    """
    if not transcript or not transcript.strip():
        raise ValueError("Transcript is empty; unable to generate SOAP note")

    try:
        chat_completion = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": CLINICAL_SOAP_PROMPT},
                {"role": "user", "content": transcript},
            ],
            model=LLM_MODEL,
            temperature=0.2,  # Low temperature does not guarantee clinical accuracy.
        )
    except RateLimitError:
        # Groq's free tier has per-minute and per-day request/token caps.
        # Surface a friendly, actionable message instead of a raw stack trace.
        logger.warning("llm_rate_limited")
        raise RuntimeError(
            "Groq free-tier usage limit reached (requests or tokens per minute/day). "
            "Please try again in a few minutes."
        ) from None
    except Exception:
        logger.error("llm_request_failed")
        raise RuntimeError("Failed to generate SOAP note") from None

    content = chat_completion.choices[0].message.content
    if not content or not content.strip():
        raise RuntimeError("LLM returned empty content")

    return content.strip()
