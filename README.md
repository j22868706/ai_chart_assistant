# AI Chart Assistant — Voice-to-SOAP Note Telegram Bot

An AI-powered clinical documentation assistant that turns a spoken consultation
into a structured SOAP note, delivered directly inside Telegram.

---

## The Problem

Home-visit therapists (physical therapy, occupational therapy, home-care nursing,
etc.) spend a significant portion of their working day commuting between clients'
homes. Unlike clinic-based practitioners, they don't have a desk between sessions
to sit down and write notes.

In practice, this leaves two bad options:

1. **Write the note during the session** — pulling focus away from the client to
   type or write, which reduces the quality of hands-on time and can feel
   impersonal or rushed to the patient.
2. **Write the note after getting home** — piling up documentation for multiple
   visits, often done late at night, extending the effective work day well beyond
   billed clinical hours and increasing burnout risk.

Either way, the therapist is trading either **care quality** or **personal time**
to keep up with documentation — and the commute time in between sessions is
completely wasted from a documentation standpoint.

## The Solution

This bot lets a therapist **dictate a quick voice summary of the session while
commuting** — in their own words, no special format needed — and send it as a
Telegram voice message. The bot:

1. Transcribes the voice note (speech-to-text)
2. Uses an LLM to reorganize the transcript into a structured **SOAP note**
   (Subjective / Objective / Assessment / Plan)
3. Sends both the transcript and the SOAP draft back to the therapist in chat,
   ready to review, lightly edit, and paste into the clinic's official EHR/chart
   system

The commute — dead time in the old workflow — becomes documentation time, without
taking anything away from the actual session with the client.

**Important:** this tool produces a **draft only**. It is a documentation
accelerator, not a replacement for clinical judgment or the official medical
record. See [Privacy & Compliance](#privacy--compliance-important) below.

---

## How It Works (Architecture)

```
Therapist (commuting)
      │  sends voice message
      ▼
┌─────────────────┐
│   Telegram Bot   │
└─────────────────┘
      │  webhook (POST /webhook)
      ▼
┌───────────────────────────────────────────────────────────┐
│                     FastAPI application                     │
│                                                               │
│  1. Verify webhook secret token (optional but recommended)  │
│  2. Return 200 immediately, hand off to a background task   │
│     (avoids Telegram webhook timeout / duplicate delivery)  │
│                                                               │
│  Background task:                                            │
│  3. Download the voice file from Telegram (.ogg)             │
│     → saved with a unique filename to avoid collisions       │
│     between concurrent requests                              │
│  4. Speech-to-Text via Groq Whisper (whisper-large-v3-turbo) │
│  5. SOAP note generation via Groq LLM (openai/gpt-oss-120b)  │
│  6. Send transcript + SOAP draft back to the user            │
│     (auto-split if over Telegram's 4096-char message limit)  │
│  7. Delete the temporary audio file (always, via `finally`)  │
└───────────────────────────────────────────────────────────┘
      │
      ▼
Telegram chat: transcript + SOAP note draft
```

### Why this design

- **Background tasks, not inline processing** — STT + LLM generation can take
  several seconds. If the webhook handler waited for the full pipeline before
  responding, Telegram could time out and re-send the same update, causing the
  same voice note to be processed (and billed) twice. The handler now returns
  `200 OK` immediately and does the real work in a `BackgroundTasks` job.
- **Unique temp filenames** — the original design used a hardcoded
  `temp_voice.ogg`, which meant two therapists (or two messages from the same
  therapist) arriving close together could overwrite each other's audio file.
  Every request now gets a `uuid4()`-based filename.
- **Async Groq client everywhere** — the LLM and STT calls use `AsyncGroq`
  rather than the synchronous `Groq` client, so a slow API call doesn't block
  the FastAPI event loop for other in-flight requests.
- **`finally`-guaranteed cleanup** — the temporary audio file is deleted whether
  the pipeline succeeds or fails, so voice recordings never accumulate on disk.
  (For crash-level resilience beyond normal exceptions — e.g. the process being
  force-killed — a startup/cron cleanup of the `temp_audio/` directory is a
  reasonable future addition.)
- **Rate-limit-aware error handling** — Groq's free tier enforces per-minute /
  per-day request and token caps. Hitting them raises `groq.RateLimitError`,
  which is caught specifically and turned into a friendly "please try again in
  a few minutes" message instead of a raw stack trace being shown to the user.
- **Webhook secret token (optional)** — Telegram supports a `secret_token` on
  `setWebhook`; if configured, the app verifies the
  `X-Telegram-Bot-Api-Secret-Token` header, preventing arbitrary third parties
  from POSTing fake voice-note payloads to the public `/webhook` URL.

---

## Project Structure

```
soap-note-bot/
├── main.py                  # FastAPI app: Telegram webhook + background pipeline
├── services/
│   ├── stt_service.py       # Speech-to-text via Groq Whisper
│   └── llm_service.py       # SOAP note generation via Groq LLM
├── utils/
│   └── prompts.py           # CLINICAL_SOAP_PROMPT template
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .gitignore
├── .env.example
├── DEPLOY.md                 # Step-by-step deployment guide
└── README.md
```

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| API framework | FastAPI + Uvicorn | Async-native, minimal boilerplate for a webhook receiver |
| Messaging | Telegram Bot API (via `httpx`) | Free, ubiquitous, works well on mobile for voice messages |
| Speech-to-text | Groq — `whisper-large-v3-turbo` | Free tier is generous (~2,000 requests/day), very fast inference |
| SOAP note generation | Groq — `openai/gpt-oss-120b` | Free tier available, fast inference, replaces the now-deprecated `llama-3.3-70b-versatile` |
| HTTP client | `httpx` (async) | Non-blocking calls to the Telegram API |

---

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Fill in `.env`:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY` | From [Groq Console](https://console.groq.com) |
| `TELEGRAM_WEBHOOK_SECRET` | Optional. Any random string; used to verify webhook requests |
| `GROQ_STT_MODEL` | Optional, default `whisper-large-v3-turbo` |
| `GROQ_STT_LANGUAGE` | Optional, default `zh`. ISO-639-1 language hint for transcription; leave empty to auto-detect |
| `GROQ_LLM_MODEL` | Optional, default `openai/gpt-oss-120b` |

### 3. Run locally

```bash
uvicorn main:app --reload --port 8000
```

### 4. Expose it to Telegram (local testing)

Telegram needs a public HTTPS URL to send webhooks to. Use a tunnel like ngrok
during development:

```bash
ngrok http 8000
```

Then register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-ngrok-domain>/webhook" \
  -d "secret_token=<your TELEGRAM_WEBHOOK_SECRET>"
```

Send a voice message to your bot on Telegram — it should reply with a
transcript and a SOAP note draft.

---

## Deploying So It Runs Without Your Laptop

Running the bot locally only works while your machine is on and the tunnel is
active — not practical for daily clinical use. See **[DEPLOY.md](./DEPLOY.md)**
for full step-by-step instructions. Summary:

| Platform | Cost | Trade-off |
|---|---|---|
| **Render** | Free (no credit card) | Free web services sleep after 15 min of inactivity; a ~30–50s cold start on the next request. Mitigated by pinging `/health` every ~10 min with a free uptime monitor (e.g. cron-job.org), which keeps it awake and still stays within the free 750 hrs/month. |
| **Railway** | ~$5/month (Hobby) | Always-on, no sleep, simplest developer experience if a small monthly cost is acceptable. |

A `Dockerfile` is included so the same container can be deployed to either
platform (or any other container host) without changes. Both platforms deploy
automatically from a GitHub push — see `DEPLOY.md` for the full walkthrough,
including how to re-point the Telegram webhook at the deployed URL.

---

## Cost: Designed to Run for Free

Both Groq models used by default are within Groq's free tier (no credit card
required):

| Service | Approximate free allowance |
|---|---|
| `openai/gpt-oss-120b` (SOAP note generation) | ~30 requests/min, ~1,000 requests/day, ~200K tokens/day |
| `whisper-large-v3-turbo` (speech-to-text) | ~2,000 transcription requests/day |

For a single therapist's daily commute usage, this is comfortably within the
free allowance. If usage ever exceeds it, Groq returns HTTP 429 — the app
catches this specifically and replies with a friendly "please try again in a
few minutes" message rather than an error trace.

Hosting can also be free (Render's free tier, see above), so the entire stack
can run at **$0/month** for individual or small-practice use.

---

## Troubleshooting

### `groq.NotFoundError: 404 model_not_found`

Groq periodically deprecates older models. `llama-3.3-70b-versatile` was
deprecated on 2026-06-17; this project defaults to `openai/gpt-oss-120b`
instead. If you see this error for any model, check the current list:

```bash
curl https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $GROQ_API_KEY" | jq '.data[].id'
```

or see the [Groq models page](https://console.groq.com/docs/models).

### `AttributeError: 'AsyncGroq' object has no attribute 'audio'`

Your installed `groq` SDK version predates the audio transcription API
(introduced with SDK v1.0.0, December 2025). Upgrade:

```bash
pip install -U groq
pip show groq   # confirm version >= 1.2.0
```

### `RuntimeError: Groq free-tier usage limit reached`

You've hit Groq's free-tier rate limit (requests or tokens per minute/day).
This is handled gracefully — the user just needs to wait a few minutes and
retry. If this happens often in real use, consider adding a payment method on
Groq to move to paid-tier limits.

---

## Privacy & Compliance (Important)

This bot handles **voice recordings and clinical content about real patients**.
Before using it for real client sessions, be aware:

- **AI-generated SOAP notes are drafts only.** Every reply includes a reminder
  that the note must be reviewed and confirmed by a licensed clinician before
  it is used as, or copied into, the official medical record. The prompt
  (`utils/prompts.py`) explicitly instructs the model not to invent findings
  that weren't mentioned in the transcript.
- **No encryption-at-rest or access control is implemented in this codebase.**
  Voice files are downloaded to local/ephemeral disk, processed, and deleted
  immediately after each request — but the Telegram bot itself has no patient
  consent flow, audit log, or role-based access control.
- Before using this for real patients, consider adding, depending on your
  jurisdiction (e.g. Taiwan's Personal Data Protection Act, HIPAA, GDPR):
  - Encryption in transit and at rest
  - Access control and audit logging
  - Patient consent / notification that a voice note is being processed by AI
  - A defined data retention and deletion policy
- Restrict who can talk to the bot (e.g. only add your own clinical staff to
  it) rather than leaving it open to the public.

---

## Roadmap / Possible Improvements

- [ ] Startup/cron cleanup of `temp_audio/` to guard against files left behind
      by a hard process crash (the `finally` block already covers normal
      success/failure paths)
- [ ] Multi-therapist support with per-user identification/authentication
- [ ] Direct export to common EHR formats
- [ ] Configurable note templates beyond SOAP (e.g. DAP, BIRP)
- [ ] Patient/session linking so notes can be grouped by client automatically