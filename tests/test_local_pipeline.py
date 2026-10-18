"""Local integration: real app/services/SDKs with in-memory HTTP transports."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from groq import AsyncGroq

from utils.limits import Capacity
from utils.prompts import CLINICAL_SOAP_PROMPT
from utils.update_store import UpdateStore


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1", "TELEGRAM_BOT_TOKEN": "offline-token",
            "GROQ_API_KEY": "offline-key", "TELEGRAM_WEBHOOK_SECRET": "offline-secret",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        })
        env.start()
        self.addCleanup(env.stop)
        self.main = importlib.import_module("main")
        self.stt = importlib.import_module("services.stt_service")
        self.llm = importlib.import_module("services.llm_service")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio_dir = Path(self.directory.name) / "audio"
        self.audio_dir.mkdir()
        self.failure = None
        self.calls = []
        self.messages = []
        self.stt_started = asyncio.Event()
        self.audio = b"synthetic-audio-only" * 4096
        self.transcript = "Patient reports shoulder pain. Completed 2 sets of 10 exercises."
        self.note = "S:\nShoulder pain reported.\nO:\n2 sets of 10 exercises.\nA:\nNot provided.\nP:\nNot provided."
        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.route)
        stt_client = AsyncGroq(api_key="offline-key", max_retries=0,
                              http_client=original_client(transport=transport))
        llm_client = AsyncGroq(api_key="offline-key", max_retries=0,
                              http_client=original_client(transport=transport))
        self.addAsyncCleanup(stt_client.close)
        self.addAsyncCleanup(llm_client.close)

        def telegram_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original_client(*args, **kwargs)

        for target, name, value in (
            (self.main, "TEMP_DIR", self.audio_dir),
            (self.main, "update_store", UpdateStore(Path(self.directory.name) / "receipts.sqlite3")),
            (self.main, "job_capacity", Capacity(2)),
            (self.main, "TELEGRAM_WEBHOOK_SECRET", "offline-secret"),
            (self.main, "TELEGRAM_ALLOWED_USER_IDS", frozenset({123})),
            (self.stt, "client", stt_client), (self.llm, "client", llm_client),
            (httpx, "AsyncClient", telegram_client),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        # Any accidental use of an actual HTTP transport fails before networking.
        network = patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                               side_effect=AssertionError("External network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.transport = httpx.ASGITransport(app=self.main.app)

    async def route(self, request):
        host, path = request.url.host, request.url.path
        self.calls.append((host, path))
        if host == "api.telegram.org" and path.endswith("/sendMessage"):
            message = json.loads(request.content)
            self.messages.append(message)
            final = "Voice Transcript" in message["text"] or "SOAP Medical" in message["text"]
            if final and self.failure == "send_network":
                raise httpx.ConnectError("synthetic sensitive exception", request=request)
            if final and self.failure == "send_http":
                return httpx.Response(500, json={"ok": False})
            if final and self.failure == "send_api":
                return httpx.Response(200, json={"ok": False, "description": "synthetic failure"})
            if final and self.failure == "markdown" and "parse_mode" in message:
                return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if host == "api.telegram.org" and path.endswith("/getFile"):
            if self.failure == "get_file":
                return httpx.Response(503, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/test.ogg"}})
        if host == "api.telegram.org" and path.endswith("/voice/test.ogg"):
            if self.failure == "download":
                class BrokenBody(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b"x" * 65536
                        raise httpx.ReadError("synthetic download failure")
                return httpx.Response(200, stream=BrokenBody())
            return httpx.Response(200, content=self.audio)
        if host == "api.groq.com" and path.endswith("/audio/transcriptions"):
            self.assertEqual(len(list(self.audio_dir.glob("*.ogg"))), 1)
            self.assertIn(self.audio, request.content)
            if self.failure == "stt":
                return httpx.Response(500, json={"error": {"message": "synthetic STT failure"}})
            if self.failure in ("timeout", "cancel"):
                self.stt_started.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json={"text": self.transcript})
        if host == "api.groq.com" and path.endswith("/chat/completions"):
            self.assertEqual(json.loads(request.content)["messages"], [
                {"role": "system", "content": CLINICAL_SOAP_PROMPT},
                {"role": "user", "content": self.transcript},
            ])
            if self.failure == "llm":
                return httpx.Response(500, json={"error": {"message": "synthetic LLM failure"}})
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
                "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": self.note}}]})
        raise AssertionError("Unexpected HTTP destination")

    async def post(self, update_id=1):
        request = httpx.Request("POST", "http://local/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "offline-secret"},
            json={"update_id": update_id, "message": {"from": {"id": 123, "is_bot": False},
                "chat": {"id": 123, "type": "private"}, "voice": {"file_id": "offline-file", "duration": 10}}})
        response = await self.transport.handle_async_request(request)
        await response.aread()
        await response.aclose()
        return response

    def assert_clean(self):
        self.assertEqual(list(self.audio_dir.iterdir()), [])
        self.assertEqual(self.main.job_capacity.active, 0)

    async def test_successful_pipeline_and_duplicate(self):
        self.assertEqual((await self.post()).json(), {"status": "processing"})
        self.assertIn(self.transcript, self.messages[-1]["text"])
        self.assertIn(self.note, self.messages[-1]["text"])
        count = len(self.calls)
        self.assertEqual((await self.post()).json(), {"status": "duplicate"})
        self.assertEqual(len(self.calls), count)
        self.assert_clean()

    async def test_multilingual_sources_reach_english_soap_and_original_reply(self):
        # These are canned provider results, not model-quality evaluations.
        sources = [
            "病人表示右肩疼痛，完成兩組、每組十次運動。未提供評估或計畫。",
            "病人 reports right shoulder pain，完成 2 sets of 10 exercises。",
            "El paciente refiere dolor en el hombro derecho. Realizó 2 series de 10 ejercicios.",
        ]
        self.note = (
            "S:\nPatient reports right shoulder pain.\n"
            "O:\nCompleted 2 sets of 10 exercises.\n"
            "A:\nAI-assisted assessment draft: Reported shoulder pain warrants clarification; "
            "exercise completion alone does not establish progress.\n"
            "P:\nSuggested plan for clinician review: Reassess reported pain during "
            "the documented exercises before considering progression."
        )
        with patch.object(self.stt, "STT_LANGUAGE", None):
            for update_id, source in enumerate(sources, start=100):
                self.transcript = source
                self.assertEqual((await self.post(update_id)).json()["status"], "processing")
                reply = self.messages[-1]["text"]
                self.assertIn("Voice Transcript (source language):", reply)
                self.assertIn(source, reply)
                self.assertIn("SOAP Medical Note Draft (English):", reply)
                self.assertIn(self.note, reply)
                self.assert_clean()

    async def test_failures_clean_files_and_stop_downstream_calls(self):
        for index, failure in enumerate(("get_file", "download", "stt", "llm", "send_network"), start=10):
            with self.subTest(failure=failure):
                self.failure = failure
                self.calls.clear()
                with self.assertLogs(level="ERROR") as logs:
                    response = await self.post(index)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.messages[-1]["text"], "❌ Processing failed, please try again later.")
                self.assertNotIn("synthetic sensitive", str(logs.output))
                if failure in ("get_file", "download"):
                    self.assertFalse(any(host == "api.groq.com" for host, _ in self.calls))
                if failure == "stt":
                    self.assertFalse(any(path.endswith("/chat/completions") for _, path in self.calls))
                self.assert_clean()

    async def test_markdown_fallback_delivers_plain_text(self):
        self.failure = "markdown"
        await self.post()
        self.assertNotIn("parse_mode", self.messages[-1])
        self.assertEqual(self.messages[-1]["text"], self.messages[-2]["text"])
        self.assert_clean()

    async def test_long_reply_is_split_without_losing_content(self):
        self.note = "S:\n" + "synthetic " * 1000
        await self.post()
        chunks = self.messages[1:]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item["text"]) <= 4096 for item in chunks))
        self.assertIn(self.note.strip(), "".join(item["text"] for item in chunks))
        self.assert_clean()

    async def test_job_timeout_cleans_downloaded_audio(self):
        self.failure = "timeout"
        with patch.object(self.main, "JOB_TIMEOUT_SECONDS", 0.05), self.assertLogs("main", level="ERROR"):
            await self.post()
        self.assertTrue(any(path.endswith("/audio/transcriptions") for _, path in self.calls))
        self.assert_clean()

    async def test_cancellation_cleans_downloaded_audio(self):
        self.failure = "cancel"
        task = asyncio.create_task(self.post())
        try:
            await asyncio.wait_for(self.stt_started.wait(), timeout=2)
            self.assertEqual(len(list(self.audio_dir.glob("*.ogg"))), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_clean()

    async def test_final_http_delivery_failure_must_be_reported(self):
        self.failure = "send_http"
        with self.assertLogs("main", level="ERROR"):
            await self.post()
        self.assert_clean()

    async def test_final_api_delivery_failure_must_be_reported(self):
        self.failure = "send_api"
        with self.assertLogs("main", level="ERROR"):
            await self.post()
        self.assert_clean()
