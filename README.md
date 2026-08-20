# AI Chart Assistant — Voice-to-SOAP Note Telegram Bot

An AI-powered clinical documentation assistant that converts spoken therapy-session summaries into structured SOAP notes and delivers them directly through Telegram.

Built to address a practical documentation challenge in home health rehabilitation, where therapists often travel between patient visits without immediate access to a workstation.

▶️ **[Watch the Demo Video on YouTube](https://www.youtube.com/shorts/8v_rkPxsisM)**

---

## Overview

**AI Chart Assistant** is an asynchronous, voice-driven clinical documentation pipeline.

A therapist can record a short voice summary of a therapy session and send it to the Telegram bot. The system automatically:

1. Receives the Telegram voice message through a webhook
2. Downloads and temporarily stores the audio file
3. Converts speech to text using Groq Whisper
4. Transforms the transcript into a structured SOAP note using an LLM
5. Returns both the transcript and generated SOAP draft to Telegram
6. Deletes the temporary audio file after processing

The generated SOAP note is intended as a **draft for clinician review**, not a replacement for clinical judgment or the official medical record.

---

## The Problem

Home health therapists often spend significant time traveling between patient visits.

Unlike clinic-based practitioners, they may not have immediate access to a workstation between sessions, making clinical documentation difficult to complete efficiently.

This creates two common workflows:

### 1. Document during the session

Typing or writing notes during patient care can interrupt the therapist's interaction with the patient and reduce hands-on treatment time.

### 2. Document after returning home

Multiple unfinished notes can accumulate throughout the day, extending documentation into personal time and increasing administrative burden.

The goal of this project is to turn otherwise unused commute time into an opportunity for documentation **without interrupting patient care**.

---

## Solution

The therapist can dictate a short summary immediately after a session using natural language.

For example:

> "Patient reported mild shoulder pain today. We performed shoulder strengthening and range-of-motion exercises. Patient required verbal cues during functional transfers because of decreased balance."

The system converts the spoken summary into a structured clinical note:

```text
Subjective:
Patient reported mild shoulder pain.

Objective:
Patient participated in shoulder strengthening and ROM exercises.
Verbal cues were required during functional transfers.

Assessment:
Patient continues to demonstrate impaired balance affecting
functional mobility and transfer performance.

Plan:
Continue therapeutic exercises, balance training, and functional
transfer training.
```

The therapist can then review and edit the generated draft before entering it into the organization's official documentation system.

---

## System Architecture

```text
                 Therapist
                     │
                     │ Voice Message
                     ▼
             ┌─────────────────┐
             │  Telegram Bot   │
             └────────┬────────┘
                      │
                      │ POST /webhook
                      ▼
             ┌────────────────────────┐
             │      FastAPI App       │
             │                        │
             │  1. Verify webhook     │
             │     secret             │
             │                        │
             │  2. Return HTTP 200    │
             │     immediately        │
             │                        │
             │  3. Background task    │
             └───────────┬────────────┘
                         │
                         ▼
               Download Telegram Audio
                         │
                         ▼
              ┌─────────────────────┐
              │ Groq Whisper        │
              │ whisper-large-v3-   │
              │ turbo               │
              └──────────┬──────────┘
                         │
                         ▼
                    Transcript
                         │
                         ▼
              ┌─────────────────────┐
              │ Groq LLM            │
              │ openai/gpt-oss-120b │
              └──────────┬──────────┘
                         │
                         ▼
                    SOAP Draft
                         │
                         ▼
                Telegram Response
                         │
                         ▼
              Temporary Audio Deleted
```

---

## Key Engineering Decisions

### Asynchronous Webhook Processing

Speech-to-text and LLM inference can take several seconds.

Instead of keeping the Telegram webhook request open during the entire AI pipeline, the application:

1. Receives the webhook
2. Returns `200 OK` immediately
3. Hands the processing pipeline to a FastAPI background task

This prevents Telegram webhook timeouts and reduces the risk of duplicate processing.

---

### Async API Clients

The application uses asynchronous clients for external API requests.

```text
Telegram
   │
   ▼
FastAPI
   │
   ├── Async Telegram API
   │
   ├── Async Groq STT
   │
   └── Async Groq LLM
```

This prevents slow external API calls from unnecessarily blocking the FastAPI event loop.

---

### Unique Temporary Audio Files

Each incoming voice message receives a unique `uuid4()` filename.

Instead of:

```text
temp_voice.ogg
```

the system creates unique temporary files such as:

```text
temp_audio/
├── 2f5c...a91.ogg
├── 9d21...f34.ogg
└── 7ab4...c82.ogg
```

This prevents concurrent requests from overwriting each other's audio files.

---

### Guaranteed Temporary File Cleanup

Temporary recordings are removed inside a `finally` block.

```text
Request
   │
   ▼
Download audio
   │
   ▼
Speech-to-text
   │
   ▼
SOAP generation
   │
   ▼
Send response
   │
   ▼
finally → Delete audio
```

This ensures temporary recordings are deleted after both successful and failed processing paths.

---

### Rate-Limit-Aware Error Handling

External AI APIs may return rate-limit errors.

Instead of exposing raw exceptions to the user, the application catches rate-limit failures and returns a user-friendly response.

```text
Groq API
   │
   ├── Success → Continue pipeline
   │
   └── Rate Limit → Friendly retry message
```

This provides a better user experience while preventing implementation details from being exposed to end users.

---

### Telegram Webhook Security

The application optionally supports Telegram's webhook secret token.

When configured, the application validates:

```text
X-Telegram-Bot-Api-Secret-Token
```

before processing incoming webhook requests.

This reduces the risk of arbitrary requests being sent to the public webhook endpoint.

---

## Project Structure

```text
soap-note-bot/
│
├── main.py
│   └── FastAPI application, Telegram webhook,
│       background processing pipeline
│
├── services/
│   ├── stt_service.py
│   │   └── Speech-to-text service
│   │
│   └── llm_service.py
│       └── SOAP note generation
│
├── utils/
│   └── prompts.py
│       └── Clinical SOAP prompt template
│
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .gitignore
├── .env
└── README.md
```

---

## Tech Stack

| Layer            | Technology                 | Purpose                        |
| ---------------- | -------------------------- | ------------------------------ |
| Backend          | Python                     | Application development        |
| API Framework    | FastAPI                    | Asynchronous webhook server    |
| Server           | Uvicorn                    | ASGI application server        |
| Messaging        | Telegram Bot API           | Voice-message interface        |
| HTTP Client      | httpx                      | Asynchronous API communication |
| Speech-to-Text   | Groq Whisper               | Voice transcription            |
| LLM              | Groq `openai/gpt-oss-120b` | SOAP note generation           |
| Containerization | Docker                     | Reproducible deployment        |
| Deployment       | Render / Railway           | Cloud hosting                  |

---

## End-to-End Workflow

### Step 1 — Voice Input

The therapist records a voice summary through Telegram.

### Step 2 — Webhook Reception

Telegram sends the voice-message event to:

```text
POST /webhook
```

The application validates the request and immediately returns `200 OK`.

### Step 3 — Background Processing

FastAPI processes the voice message asynchronously.

The audio file is downloaded and stored temporarily with a unique filename.

### Step 4 — Speech Recognition

The audio is sent to:

```text
Groq Whisper
whisper-large-v3-turbo
```

The resulting transcript becomes the input for the clinical documentation pipeline.

### Step 5 — SOAP Generation

The transcript is passed to the LLM with a clinical SOAP prompt.

The model organizes the information into:

* Subjective
* Objective
* Assessment
* Plan

### Step 6 — Response

The bot sends the therapist:

```text
Transcript

+

SOAP Note Draft
```

Long responses are automatically split to respect Telegram's message-length limitations.

### Step 7 — Cleanup

The temporary audio recording is deleted after processing.

---

## Local Development

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd soap-note-bot
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

Activate it:

```bash
# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
GROQ_API_KEY=your_groq_api_key

TELEGRAM_WEBHOOK_SECRET=your_random_secret

GROQ_STT_MODEL=whisper-large-v3-turbo
GROQ_STT_LANGUAGE=zh
GROQ_LLM_MODEL=openai/gpt-oss-120b
```

### 5. Start the application

```bash
uvicorn main:app --reload --port 8000
```

### 6. Test the webhook locally

Telegram requires a publicly accessible HTTPS endpoint for webhooks.

For local development, a tunneling service such as ngrok can be used:

```bash
ngrok http 8000
```

Register the webhook:

```bash
curl -X POST \
  "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-ngrok-domain>/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Send a voice message to the Telegram bot to test the complete pipeline.

---

## Docker

The project includes a `Dockerfile` for containerized deployment.

Build the image:

```bash
docker build -t soap-note-bot .
```

Run the container:

```bash
docker run -p 8000:8000 --env-file .env soap-note-bot
```

The same container can be deployed to a cloud container platform without changing the application code.

---

## Deployment

The application can be deployed to platforms such as:

* Render
* Railway
* Other Docker-compatible cloud platforms

A typical production flow is:

```text
GitHub
   │
   ▼
Cloud Platform
   │
   ▼
Docker Container
   │
   ▼
FastAPI + Uvicorn
   │
   ▼
Telegram Webhook
```

---

## Cost Optimization

The application was designed around services with free-tier availability during development.

The architecture minimizes infrastructure requirements by using:

* Serverless-style webhook processing
* External speech-to-text inference
* External LLM inference
* Docker-based deployment
* Temporary local storage
* No database requirement for the initial version

This makes the prototype suitable for experimentation and small-scale individual use.

Actual API quotas and pricing may change over time, so production deployments should verify the current provider limits.

---

## Privacy & Compliance

**This project is a technical prototype and should not be used with real patient information without appropriate security, privacy, and compliance controls.**

The application processes potentially sensitive clinical information and voice recordings.

The current implementation does **not** provide a complete production-grade healthcare security model.

Important considerations before processing real patient data include:

* Encryption in transit and at rest
* Authentication and authorization
* Role-based access control
* Audit logging
* Patient consent and notification
* Data retention and deletion policies
* Secure secrets management
* Vendor and API compliance requirements
* HIPAA/GDPR/local privacy-law considerations where applicable

The current implementation temporarily stores voice recordings during processing and deletes them after the pipeline completes.

The generated SOAP note should always be reviewed by a qualified clinician before being entered into an official medical record.

---

## Roadmap

* [ ] Add authentication and multi-therapist support
* [ ] Add persistent user/session management
* [ ] Add encrypted storage
* [ ] Add audit logging
* [ ] Add automatic startup cleanup for orphaned audio files
* [ ] Add configurable documentation templates
* [ ] Support DAP and BIRP notes
* [ ] Add patient/session linking
* [ ] Add structured EHR export
* [ ] Add production-grade monitoring and observability
* [ ] Add automated tests for the complete processing pipeline

---

## Why I Built This

As an occupational therapist transitioning into software engineering, I built this project around a real clinical documentation workflow.

The goal was not only to demonstrate LLM integration, but to solve a practical problem while applying software engineering principles such as:

* Asynchronous API design
* Webhook processing
* External API integration
* Error handling
* Concurrency safety
* Temporary file management
* Containerization
* Cloud deployment
* Security and privacy considerations

This project represents the intersection of **healthcare domain knowledge and software engineering**.
