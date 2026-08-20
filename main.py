import logging
import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
import httpx

from services.llm_service import generate_soap_note
from services.stt_service import transcribe_audio

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set, please check the .env file")

# Optional: Set a secret_token during Telegram setWebhook and verify it here
# to prevent unauthorized requests directly hitting your /webhook endpoint.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

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
        resp = await client.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
        )
        if resp.status_code != 200:
            # If Markdown parsing fails, fallback to plain text and resend to ensure delivery
            await client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )


async def process_voice_message(chat_id: int, file_id: str) -> None:
    """Background task: Download audio -> STT transcription -> LLM SOAP note generation -> Send back to user.

    Separated from the webhook handler so Telegram immediately receives a 200 OK,
    preventing timeout re-deliveries and duplicate processing.
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
            file_path_info = file_info_res.json()["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path_info}"

            # 2. Download the voice file to local temporary storage (using a unique filename to avoid race conditions)
            file_res = await client.get(download_url)
            file_res.raise_for_status()
            local_audio_path.write_bytes(file_res.content)

            # 3. Speech-to-Text transcription
            transcript = await transcribe_audio(str(local_audio_path))

            # 4. Generate SOAP medical note
            soap_note = await generate_soap_note(transcript)

            # 5. Send back to the user
            reply_text = (
                f"🎙️ **Voice Transcript:**\n{transcript}\n\n"
                f"📋 **SOAP Medical Note Draft:**\n{soap_note}\n\n"
            )
            await send_telegram_message(client, chat_id, reply_text)

        except Exception as e:
            logger.exception(
                "An error occurred while processing the voice message (chat_id=%s)",
                chat_id,
            )
            await send_telegram_message(
                client,
                chat_id,
                f"❌ Processing failed, please try again later.\nError message: {e}",
            )
        finally:
            if local_audio_path.exists():
                local_audio_path.unlink()


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    # Verify Telegram secret token (if configured)
    if TELEGRAM_WEBHOOK_SECRET:
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret_header != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")

    data = await request.json()

    message = data.get("message", {})
    if "voice" in message:
        chat_id = message["chat"]["id"]
        file_id = message["voice"]["file_id"]

        # Hand off to background processing and immediately return 200 to prevent webhook timeout re-delivery
        background_tasks.add_task(process_voice_message, chat_id, file_id)
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

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
