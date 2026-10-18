# AI Chart Assistant — Multilingual Voice to English SOAP

A Telegram bot that turns spoken therapy-session summaries into **English SOAP
drafts**, while returning the source-language transcript for clinician review.

Built by an occupational therapist transitioning into software engineering to
address a practical home-health rehabilitation workflow: documenting visits when
a workstation is not readily available.

[Watch the demo](https://www.youtube.com/shorts/8v_rkPxsisM)

> This is a technical prototype. Use synthetic data for demonstrations. Generated
> notes require clinician review and are not an official medical record. Hosting
> the app locally does not keep data local: Telegram and Groq process the content.

## The problem and approach

Home-health therapists move between patient visits and may accumulate unfinished
notes until the end of the day. Dictating a short session summary can reduce the
friction of organizing documentation after a visit.

The bot separates transcription from clinical organization. Groq Whisper
transcribes the recording with automatic input-language detection. An LLM then
translates the supplied clinical information into an English SOAP draft. The
source transcript remains visible so the therapist can check the translation,
measurements, and omissions against the recording.

This project does not measure time saved, clinical effectiveness, or documentation
accuracy across a patient population. Its demonstrated outcome is a working
engineering prototype with controlled Chinese and English voice tests.

## Example

Synthetic Chinese input:

> 病人表示右肩輕微疼痛，完成肩部運動兩組，每組十次。未提供治療師評估或後續治療計畫。

English equivalent:

> The patient reports mild right shoulder pain and completed two sets of ten
> shoulder exercises. No therapist assessment or future treatment plan was provided.

Illustrative English draft:

```text
S:
Patient reports mild right shoulder pain.

O:
Patient completed 2 sets of 10 shoulder exercises.

A:
Assessment not provided in the transcript; clinician review required.

P:
Plan not provided in the transcript; clinician review required.
```

Missing assessment and plan information stays missing. Completed exercises do not
establish a future treatment plan, and missing symptoms do not mean symptoms were
denied. The clinician must verify the draft and supply missing information.

## Architecture

```text
Telegram private voice message
    |
    v
FastAPI POST /webhook
    |-- Verify required webhook secret before JSON parsing
    |-- Check body size, user allowlist, private chat, voice limits
    |-- Reserve bounded processing capacity
    |-- Atomically claim update_id in SQLite
    |-- Acknowledge accepted update, dispatch in-process background work
    v
Stream Telegram audio into a unique bounded temporary file
    v
Groq Whisper: automatic input-language detection -> source transcript
    v
Groq LLM: source transcript -> English SOAP draft
    v
Telegram: source transcript + English draft, split into message chunks
    v
Attempt temporary-file cleanup; release processing capacity
```

| Layer | Technology | Purpose |
| --- | --- | --- |
| API | FastAPI / Uvicorn | Webhook validation and asynchronous processing |
| Messaging | Telegram Bot API | Voice input and draft delivery |
| HTTP | httpx | Asynchronous requests and bounded audio streaming |
| Speech | Groq `whisper-large-v3-turbo` | Multilingual transcription |
| LLM | Groq `openai/gpt-oss-120b` | Translation and English SOAP organization |
| Receipts | Python SQLite | Persistent update-ID deduplication |
| Packaging | Python 3.11 / Docker | Pinned dependencies and container entry point |
| Existing host | Render | Existing webhook service; rollout described below |

External calls use async clients. Background work remains inside the web process:
this is not a durable queue, and accepted jobs can be interrupted by a restart.

## Features and safety controls

### Multilingual input, English output

Leave `GROQ_STT_LANGUAGE` blank or unset to omit the input-language hint. Explicit
hints such as `en` or `zh` are optional for deliberate single-language use. They
do not select the output language: the SOAP system policy always requires English.

The app uses the transcription endpoint to preserve the source transcript; the
LLM performs translation and SOAP organization together. See
[Groq speech-to-text documentation](https://console.groq.com/docs/speech-to-text)
for the optional language parameter and endpoint distinction. Language coverage
is finite; detection and mixed-language accuracy require evaluation.

The transcript is passed unchanged as a user message, separate from the system
policy. Instruction-like source content must not override that policy. The policy
requires preservation of numbers, units, laterality, negation, assistance levels,
attribution, and uncertainty. Contradictions stay visible for clarification;
unclear details must not be guessed. Role separation and prompt instructions do
not guarantee factual accuracy or resistance to prompt injection.

### Access control and resource limits

`TELEGRAM_WEBHOOK_SECRET` and a nonempty `TELEGRAM_ALLOWED_USER_IDS` are required;
invalid access-control configuration blocks startup. The secret is compared in
constant time before parsing the body. Only allowlisted non-bot senders in their
own private chat can dispatch work. Group/channel and unauthorized messages are
acknowledged as ignored without a Bot reply. See the
[Telegram webhook contract](https://core.telegram.org/bots/api#setwebhook).

| Setting | Default | Maximum configurable value |
| --- | --- | --- |
| `MAX_WEBHOOK_BYTES` | 65536 (64 KiB) | 1048576 |
| `MAX_AUDIO_BYTES` | 10485760 (10 MiB) | 20971520 |
| `MAX_VOICE_SECONDS` | 300 | 3600 |
| `MAX_CONCURRENT_JOBS` | 2 per process | 16 |
| `JOB_TIMEOUT_SECONDS` | 180 | 900 |

Limits must be positive integers within these ranges. Request-body and audio
limits check actual streamed bytes as well as declared sizes. Audio is downloaded
in chunks; empty audio and compressed HTTP content encodings are rejected.
Duration is checked against Telegram metadata, not independent audio decoding.

Oversized webhook bodies return 413. Authorized oversized voice metadata returns
200 with `rejected`, without calling Groq. Full job capacity returns 429 with
`Retry-After: 10`, without recording a receipt. There is no unbounded waiting
queue. Run one worker: additional processes multiply the concurrency limit.

### Persistent deduplication

The SQLite store records only `update_id` and the receipt timestamp, with atomic
claims across concurrent connections. Its default path is
`state/telegram_updates.sqlite3`; use a separate database per bot. The database
file is owner-readable/writable only, and newly created state directories have
owner-only permissions. SQLite work runs off the event loop.

Receipts are retained for 7 days and capped at 100,000 rows. Expiry is pruned when
new updates arrive, rather than by a timed erasure job. Previously accepted IDs
return 200 with `duplicate`; storage failure or exhaustion returns 503 without
dispatching new work. SQLite allocated file size is not an exact byte quota.

This prevents repeated dispatch within the retention window; it is not
exactly-once execution. Failed, timed-out, or interrupted accepted jobs retain
their receipts. Resend the voice as a new message to retry intentionally. Deleting
the database, losing its disk, or receipt expiry removes deduplication protection.

### Delivery, errors, and temporary recordings

Every Telegram chunk, including a fallback, must return HTTP 200 and boolean
`ok: true`. A confirmed Markdown parsing rejection is retried once as plain text;
other HTTP, API, or network failures stop further chunks. See the
[Telegram API response contract](https://core.telegram.org/bots/api#making-requests).
Earlier chunks may already be delivered when a later chunk fails. A successful
response indicates acceptance, not that the recipient read the message.

Processing errors produce a fixed retry message. Application logs contain fixed
event names rather than raw exceptions, credential URLs, chat IDs, paths,
transcripts, or SOAP content. HTTPX, HTTPCore, and Groq diagnostics are suppressed.
Failed error notifications are caught separately. Proxies, hosting access logs,
and custom integrations still need their own review.

Audio uses UUID filenames and cleanup in `finally`. Success, handled failure,
timeout, and cancellation paths are tested. Failed deletion is logged and may
leave a recording; forced process termination can bypass cleanup. This is
best-effort temporary storage, not a guaranteed erasure mechanism. Timeouts also
do not guarantee cancellation of work already accepted by an external provider.

## Project structure

```text
main.py                         # Webhook, capacity, download, processing, delivery
services/
  stt_service.py                # Source-language transcription
  llm_service.py                # English SOAP generation
utils/
  prompts.py                    # Clinical fidelity and translation policy
  security.py                   # Required secret and numeric allowlist parsing
  limits.py                     # Validated limits and per-process capacity
  update_store.py               # Bounded SQLite receipt store
  logging_config.py             # Suppress sensitive SDK/HTTP diagnostics
tests/                          # Offline unit and integration tests
.env.example                    # Placeholder configuration; no real credentials
requirements.txt                # Pinned direct and transitive runtime dependencies
Dockerfile
README.md
```

`.env`, temporary recordings, and default SQLite state are excluded from Git and
Docker build contexts. Never commit real credentials or patient data.

## Local setup

Use Python 3.11. Dependencies were verified with Python 3.11.3 on macOS; use a
maintained patch release and repeat validation on the actual deployment platform.

```bash
git clone git@github.com:j22868706/ai_chart_assistant.git
cd ai_chart_assistant
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

For a new setup, copy `.env.example` to `.env`. If `.env` already exists, edit it
instead of overwriting it. Required settings:

```env
TELEGRAM_BOT_TOKEN=replace_with_your_bot_token
GROQ_API_KEY=replace_with_your_groq_api_key
TELEGRAM_WEBHOOK_SECRET=replace_with_a_generated_secret
TELEGRAM_ALLOWED_USER_IDS=replace_with_your_numeric_user_id
GROQ_STT_MODEL=whisper-large-v3-turbo
GROQ_STT_LANGUAGE=
GROQ_LLM_MODEL=openai/gpt-oss-120b
TELEGRAM_UPDATE_DB_PATH=state/telegram_updates.sqlite3
```

Use numeric Telegram user IDs, not usernames or group IDs. Generate a webhook
secret locally and keep the same value in Telegram registration and the app:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8000/health
```

Expected response: `{"status":"ok"}`. This checks local availability, not external
credentials or inference quality. Restart after configuration changes. Avoid
`--reload` while processing audio because restarts can interrupt accepted work.

Receiving Telegram webhooks requires a public HTTPS endpoint. Localhost alone
cannot receive them. Controlled live tests used temporary polling, then restored
the original webhook; do not switch an active bot's receiving mode casually.

## Validation and engineering lessons

Run offline checks after installing the pinned requirements:

```bash
python -m unittest discover -s tests -v
```

**47 offline tests passed**, with no expected failures. Coverage includes access
control, malformed and oversized input, streaming limits, capacity release,
SQLite concurrency/retention, source-language handling, English-output policy,
privacy of failures, Telegram delivery, and complete simulated processing.
The integration suite runs real app/service/SDK code against in-memory transports
with fake credentials, synthetic bytes, and temporary databases. Actual HTTP
transports are blocked in that suite. Mock results do not establish model quality.

Deployment preparation was rechecked on **2026-10-18**:

| Check | Result and scope |
| --- | --- |
| Fresh Python 3.11 environment | All 21 dependency pins matched `requirements.txt`; `pip check` and all 47 offline tests passed |
| Linux container | `linux/amd64` image built successfully; all 21 pins, `pip check`, and all 47 offline tests passed |
| Container HTTP checks | With fake credentials and networking disabled, `/health` returned 200; missing/wrong webhook secrets returned 403 before JSON parsing |
| Image contents | Local `.env`, Git metadata, virtual environment, and existing SQLite state were excluded |
| Container shutdown | After adding `exec` to Docker's startup command, Uvicorn ran as PID 1 and stopped cleanly in approximately 0.32 seconds with exit code 0 |

The existing local `venv` differed from 14 required dependency pins, so its
passing tests alone were insufficient deployment evidence. The fresh environment
and Linux container checks above used the exact pinned set. These checks did not
call Telegram or Groq, and do not establish hosted end-to-end behavior.

Live verification completed on **2026-10-18**:

| Check | Result and scope |
| --- | --- |
| Dependency consistency and audit | Candidate runtime set passed `pip check`; `pip-audit` reported no known vulnerabilities at audit time |
| Actual local server | Uvicorn listened only on loopback; health, rejection paths, clean shutdown, and listener release verified |
| Telegram credentials and delivery | Bot authentication verified; test message accepted for the verified user |
| Chinese voice | Automatic detection retained Chinese transcript; English SOAP preserved the synthetic scenario's key facts |
| English voice | Automatic detection retained English transcript; English SOAP preserved the same key facts |
| Clinical example review | User confirmed both results; S/O retained mild right shoulder pain and 2 sets of 10 exercises, A/P remained unspecified |
| Live-test cleanup | Temporary audio removed and original webhook restored after each controlled test |

Live inputs were synthetic. The tests exercised receipt handling, local webhook
logic, real Telegram downloads/replies, and real Groq calls through temporary
polling, not a newly deployed hosted webhook. This is a two-case demonstration,
not a multilingual clinical benchmark. Other languages, accents, mixed-language
audio, and real-world recordings remain unevaluated.

Two practical defects emerged during validation. An English input-language hint
was still configured during an early Chinese test; the default is now automatic
detection. Delivery tests also exposed silently ignored Telegram failures; both
HTTP status and JSON success are now required, including on formatting fallback.
These findings explain why pipeline completion and dependency checks alone were
insufficient to validate the product behavior.

Dependencies are pinned, including indirect packages. Unused `python-multipart`
was removed, and Uvicorn does not require its optional `standard` extras. Resolve
updates in a fresh environment, audit the resolved set, and repeat checks before
replacing pins. A clean audit is time-bounded evidence, not a security guarantee.

## Docker and existing Render service

```bash
docker build -t ai-chart-assistant .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env ai-chart-assistant
```

This basic run uses container-local receipt storage. Mount persistent storage
before relying on deduplication across container replacement.

The existing webhook host is **Render**. Authenticated service inspection on
**2026-10-18** confirmed the following configuration before this release:

| Setting | Inspected state |
| --- | --- |
| Service | `ai_chart_assistant`, public URL `https://ai-chart-assistant.onrender.com` |
| Source | `j22868706/ai_chart_assistant`, branch `main`; auto-deploy on commit enabled |
| Runtime | Docker, Ohio, Free plan, one instance |
| Health check | `/health`, saved and confirmed through the service API |
| Pre-release revision | `a77f1f5a232a45ac9985892699390adff8d51a34`; latest inspected deploy was `live` |
| Environment | Required variables confirmed present by the operator; values were not retrieved or disclosed |
| Storage decision | Retain Free plan without a persistent disk |

Saving the health-check setting redeployed the pre-release revision; it did not
release the local multilingual/security changes. Pushing this release to `main`
will trigger an automatic deployment. Hosted verification of that new revision
remains pending; local DNS access failed during pre-release checks.

Without a disk, the default `TELEGRAM_UPDATE_DB_PATH` is
`state/telegram_updates.sqlite3` on ephemeral storage. An alternative configured
path on the same ephemeral filesystem does not make it persistent. Receipt loss
on restart or redeploy can allow a previously accepted update to be processed
again. Cross-redeploy deduplication is not guaranteed in this configuration.

Before rolling out this version to the existing service:

1. Inspect the linked repository/branch, runtime, deployed revision, auto-deploy
   policy, environment configuration, instance count, and disk attachment.
2. Preserve existing Bot/Groq credentials; configure the required webhook secret
   and numeric user allowlist. Leave `GROQ_STT_LANGUAGE` blank for multilingual use.
   Do not copy the local `.env` into Git or the container image.
3. Use one Uvicorn worker/instance and `/health` for health checks. Docker's command
   binds to `0.0.0.0` and the platform-provided `PORT`; native Python runtimes need
   an equivalent command, not `python main.py`, which intentionally uses loopback.
4. If cross-redeploy receipt persistence becomes required, point
   `TELEGRAM_UPDATE_DB_PATH` at a persistent disk mount. The current rollout
   intentionally uses ephemeral storage. A disk is a billing/configuration decision, and
   [attaching one disables zero-downtime deploys](https://render.com/docs/disks).
   Do not claim cross-redeploy deduplication on ephemeral storage or across
   independent SQLite files on multiple instances.
5. Review and deploy a specific source revision with a rollback reference. Keep
   the existing webhook URL if the service URL is unchanged; changing code alone
   does not require moving the webhook.
6. Check build/startup, health, secret rejection, webhook delivery status, and a
   controlled synthetic Chinese/English voice exchange before marking rollout
   complete. A platform rollback may not restore changed environment settings;
   preserve those separately.

See [Render deployment behavior](https://render.com/docs/deploys) and
[Render web-service configuration](https://render.com/docs/web-services).
Do not infer zero downtime for an in-process audio job during a rollout. This
prototype still needs a durable queue to recover interrupted accepted jobs.

### Free-tier keepalive

[Render keepalive](.github/workflows/render-keepalive.yml) uses a standard GitHub
Actions Ubuntu runner to request the public `/health` endpoint every 10 minutes
(UTC minutes 3, 13, 23, 33, 43, and 53). It also runs when its workflow file is
pushed to `main`, and supports **Run workflow** from the repository's Actions tab.
No secrets, checkout, Telegram messages, or Groq calls are needed. Each run checks
for HTTP 200 and `{"status":"ok"}`, retries up to three attempts for cold starts,
and fails if the endpoint remains unhealthy. Runs are restricted to this public
repository so forks or a switch to private visibility do not incur runner usage.

Standard GitHub-hosted runners are
[free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
This is a best-effort way to reduce idle spin-down, not an uptime guarantee:
[scheduled runs can be delayed or dropped, and are disabled after 60 days of repository inactivity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
If disabled, re-enable the workflow in Actions. Disable this workflow to stop
keepalive requests. Render still shares 750 Free instance hours per workspace
each month; keeping multiple services active can exhaust that allowance.
Restarts, spin-downs, and redeploys still lose ephemeral SQLite receipts.

The application revision `2609fe5` was subsequently verified `live` on Render
after correcting the numeric user allowlist. Its public `/health` returned 200,
and missing/wrong webhook secrets returned 403. Hosted Chinese/English voice
verification remains pending. Keepalive-only commits use `[skip render]` to
avoid redeploying the application for scheduling/documentation changes.

## Privacy, limits, and next work

This project is not certified or asserted to comply with HIPAA, GDPR, or other
healthcare privacy requirements. Real patient use requires an appropriate legal
and security review of Telegram, Groq, the host, consent, retention, access,
auditability, and relevant vendor agreements. Model output always requires
clinician verification against the original recording.

Completed:

- [x] Multilingual input with English SOAP policy and source transcript
- [x] Required webhook secret, numeric user allowlist, private-chat restriction
- [x] Bounded requests/audio/jobs and safe error reporting
- [x] Persistent update receipt deduplication with bounded retention
- [x] Offline pipeline simulation and live Chinese/English synthetic tests

Remaining:

- [ ] Verify rollout of the tested revision on the existing Render service
- [ ] Durable background jobs and recovery of interrupted accepted work
- [ ] Per-user quotas and spending controls
- [ ] Stronger recording lifecycle controls and orphan cleanup
- [ ] Clinically reviewed multilingual evaluation beyond two examples
- [ ] Multi-user account management and appropriate audit records
- [ ] Organization-approved templates and reviewed EHR export

## Why I built this

The project combines clinical workflow knowledge with API integration, async
Python, secure configuration, resource control, and testable failure handling.
The engineering goal is a useful documentation draft with visible source evidence
and explicit limits, rather than an unverified promise of autonomous charting.
