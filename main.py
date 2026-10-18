import logging
import asyncio
import json
import os
import secrets
from pathlib import Path
import uuid

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
import httpx

from services.llm_service import generate_soap_note
from services.stt_service import transcribe_audio
from utils.security import parse_allowed_user_ids, validate_webhook_secret
from utils.logging_config import configure_safe_logging
from utils.limits import Capacity, positive_limit
from utils.update_store import UpdateStore

load_dotenv()

configure_safe_logging()
logger = logging.getLogger(__name__)

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set, please check the .env file")

TELEGRAM_WEBHOOK_SECRET = validate_webhook_secret(os.getenv("TELEGRAM_WEBHOOK_SECRET"))
TELEGRAM_ALLOWED_USER_IDS = parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
MAX_WEBHOOK_BYTES = positive_limit("MAX_WEBHOOK_BYTES", 65536, 1048576)
MAX_AUDIO_BYTES = positive_limit("MAX_AUDIO_BYTES", 10485760, 20971520)
MAX_VOICE_SECONDS = positive_limit("MAX_VOICE_SECONDS", 300, 3600)
MAX_CONCURRENT_JOBS = positive_limit("MAX_CONCURRENT_JOBS", 2, 16)
JOB_TIMEOUT_SECONDS = positive_limit("JOB_TIMEOUT_SECONDS", 180, 900)
job_capacity = Capacity(MAX_CONCURRENT_JOBS)
update_store = UpdateStore(Path(os.getenv("TELEGRAM_UPDATE_DB_PATH", "state/telegram_updates.sqlite3")))

TEMP_DIR = Path("temp_audio")
TEMP_DIR.mkdir(exist_ok=True)


async def send_telegram_message(
    client: httpx.AsyncClient, chat_id: int, text: str
) -> None:
    """Send a message to the user, automatically handling Telegram's 4096-character limit

    by splitting it into chunks if necessary.
    """
    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH):
        chunk = text[i : i + TELEGRAM_MAX_MESSAGE_LENGTH]
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        for attempt in range(2):
            try:
                resp = await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
                result = resp.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("Telegram message delivery failed") from None
            if not isinstance(result, dict):
                raise RuntimeError("Telegram message delivery failed")
            if resp.status_code == 200 and result.get("ok") is True:
                break
            description = result.get("description")
            markdown_error = (
                attempt == 0
                and resp.status_code == 400
                and result.get("ok") is False
                and type(result.get("error_code")) is int
                and result["error_code"] == 400
                and isinstance(description, str)
                and description.lower().startswith("bad request: can't parse entities")
            )
            if not markdown_error:
                raise RuntimeError("Telegram message delivery failed")
            # Retry only a confirmed formatting rejection, once, as plain text.
            payload.pop("parse_mode")


async def process_voice_message(chat_id: int, file_id: str) -> None:
    """Background task: Download audio -> STT transcription -> LLM SOAP note generation -> Send back to user.

    The webhook acknowledges before processing. Work remains in-process and
    not durable; accepted update receipts are deduplicated separately.
    """
    local_audio_path = TEMP_DIR / f"{uuid.uuid4().hex}.ogg"

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            await send_telegram_message(
                client, chat_id, "🎙️ Voice message received, processing, please wait..."
            )

            # 1. Get the voice file download path
            file_info_res = await client.get(
                f"{TELEGRAM_API_URL}/getFile?file_id={file_id}"
            )
            file_info_res.raise_for_status()
            file_info = file_info_res.json()["result"]
            reported_size = file_info.get("file_size")
            if reported_size is not None and (
                type(reported_size) is not int or not 0 < reported_size <= MAX_AUDIO_BYTES
            ):
                raise ValueError("Invalid audio size")
            file_path_info = file_info["file_path"]
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path_info}"

            # 2. Download the voice file to local temporary storage (using a unique filename to avoid race conditions)
            await download_audio(client, download_url, local_audio_path)

            # 3. Speech-to-Text transcription
            transcript = await transcribe_audio(str(local_audio_path))

            # 4. Generate SOAP medical note
            soap_note = await generate_soap_note(transcript)

            # 5. Send back to the user
            reply_text = (
                f"🎙️ **Voice Transcript (source language):**\n{transcript}\n\n"
                f"📋 **SOAP Medical Note Draft (English):**\n{soap_note}\n\n"
            )
            await send_telegram_message(client, chat_id, reply_text)

        except Exception:
            logger.error("voice_processing_failed")
            try:
                await send_telegram_message(
                    client,
                    chat_id,
                    "❌ Processing failed, please try again later.",
                )
            except Exception:
                logger.error("telegram_error_notification_failed")
        finally:
            try:
                local_audio_path.unlink(missing_ok=True)
            except OSError:
                logger.error("temporary_audio_cleanup_failed")


async def download_audio(client: httpx.AsyncClient, url: str, path: Path) -> None:
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Unsupported audio content encoding")
        length = response.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_AUDIO_BYTES):
            raise ValueError("Invalid audio size")
        total = 0
        with path.open("xb") as output:
            async for chunk in response.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise ValueError("Audio size limit exceeded")
                output.write(chunk)
        if total == 0:
            raise ValueError("Empty audio")


async def run_voice_job(chat_id: int, file_id: str) -> None:
    try:
        async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
            await process_voice_message(chat_id, file_id)
    except TimeoutError:
        logger.error("voice_processing_timeout")
    finally:
        job_capacity.release()


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    secret_headers = request.headers.getlist("X-Telegram-Bot-Api-Secret-Token")
    if len(secret_headers) != 1 or not secrets.compare_digest(
        secret_headers[0].encode("utf-8"), TELEGRAM_WEBHOOK_SECRET.encode("ascii")
    ):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_WEBHOOK_BYTES):
        raise HTTPException(status_code=413, detail="Request too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Request too large")
        body.extend(chunk)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid update")

    message = data.get("message")
    if not isinstance(message, dict):
        return {"status": "ignored"}

    sender = message.get("from")
    chat = message.get("chat")
    # Acknowledge disallowed updates without processing or replying to Telegram.
    # Returning success avoids repeated webhook deliveries for denied messages.
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return {"status": "ignored"}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if (
        type(user_id) is not int
        or user_id not in TELEGRAM_ALLOWED_USER_IDS
        or sender.get("is_bot") is not False
        or chat.get("type") != "private"
        or type(chat_id) is not int
        or chat_id != user_id
    ):
        return {"status": "ignored"}

    if "voice" in message:
        voice = message["voice"]
        if not isinstance(voice, dict) or not isinstance(voice.get("file_id"), str) or not voice["file_id"].strip():
            raise HTTPException(status_code=400, detail="Invalid voice message")
        file_id = voice["file_id"]
        duration = voice.get("duration")
        size = voice.get("file_size")
        if type(duration) is not int or duration < 0:
            raise HTTPException(status_code=400, detail="Invalid voice duration")
        if size is not None and (type(size) is not int or size <= 0):
            raise HTTPException(status_code=400, detail="Invalid voice size")
        if duration > MAX_VOICE_SECONDS or (size is not None and size > MAX_AUDIO_BYTES):
            # Acknowledge permanently oversized messages to avoid repeated delivery.
            return {"status": "rejected", "reason": "voice_limit_exceeded"}
        update_id = data.get("update_id")
        if type(update_id) is not int or not 0 <= update_id <= 2**63 - 1:
            raise HTTPException(status_code=400, detail="Invalid update ID")
        if not job_capacity.acquire():
            raise HTTPException(status_code=429, detail="Service busy", headers={"Retry-After": "10"})

        handed_off = False
        try:
            try:
                accepted = await asyncio.to_thread(update_store.claim, update_id)
            except Exception:
                logger.error("update_receipt_store_failed")
                raise HTTPException(status_code=503, detail="Service unavailable") from None
            if not accepted:
                return {"status": "duplicate"}
            # Receipt persistence prevents redelivery, but work is still in-process.
            background_tasks.add_task(run_voice_job, chat_id, file_id)
            handed_off = True
        finally:
            if not handed_off:
                job_capacity.release()
        return {"status": "processing"}

    return {"status": "ignored"}


@app.get("/")
async def root():
    # Render (and curious browsers) will hit "/" by default. Without this route
    # it just 404s, which is harmless but noisy in the logs. Give it a friendly
    # response instead.
    return {"status": "ok", "service": "ai-chart-assistant", "docs": "/health for health check"}


@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, workers=1)
