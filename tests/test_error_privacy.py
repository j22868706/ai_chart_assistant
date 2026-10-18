"""Regression checks for secrets and clinical content in failure paths."""

import importlib
import logging
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from groq import RateLimitError

from utils.logging_config import configure_safe_logging


SENSITIVE = "fake-bot-token fake-api-key patient-private-content"


class ErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1", "TELEGRAM_BOT_TOKEN": "fake-bot-token",
            "GROQ_API_KEY": "fake-api-key", "TELEGRAM_WEBHOOK_SECRET": "test-secret",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        })
        env.start()
        self.addCleanup(env.stop)
        self.main = importlib.import_module("main")
        network = patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def assert_private(self, text):
        for value in SENSITIVE.split():
            self.assertNotIn(value, text)
        self.assertNotIn("Traceback", text)

    async def test_pipeline_and_notification_failure_are_safe(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError(SENSITIVE))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        for notification_fails in (False, True):
            send = AsyncMock(side_effect=[None, RuntimeError(SENSITIVE)] if notification_fails else None)
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(self.main, "TEMP_DIR", Path(directory)), \
                     patch.object(self.main.httpx, "AsyncClient", return_value=context), \
                     patch.object(self.main, "send_telegram_message", send), \
                     self.assertLogs("main", level="ERROR") as logs:
                    await self.main.process_voice_message(123, "fake-file")
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assert_private("\n".join(logs.output))
            self.assert_private(str(send.call_args_list))
            self.assertEqual(send.await_args_list[-1].args[2], "❌ Processing failed, please try again later.")

    async def test_service_errors_and_rate_limits_are_safe(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
        for module_name, method in (("services.llm_service", "llm"), ("services.stt_service", "stt")):
            module = importlib.import_module(module_name)
            target = module.client.chat.completions if method == "llm" else module.client.audio.transcriptions
            errors = [RuntimeError(SENSITIVE), RateLimitError(SENSITIVE, response=response, body={"error": SENSITIVE})]
            for error in errors:
                with patch.object(target, "create", new=AsyncMock(side_effect=error)), \
                     patch("services.stt_service.os.path.exists", return_value=True), \
                     patch("services.stt_service._read_file_bytes", return_value=b"synthetic"), \
                     self.assertLogs(module_name, level="WARNING") as logs:
                    try:
                        if method == "llm":
                            await module.generate_soap_note("synthetic transcript")
                        else:
                            await module.transcribe_audio("fake.ogg")
                    except RuntimeError as exc:
                        self.assert_private(str(exc))
                        self.assertNotIn(SENSITIVE, "".join(traceback.format_exception(exc)))
                        self.assertTrue(exc.__suppress_context__)
                    else:
                        self.fail("Expected sanitized service error")
                self.assert_private("\n".join(logs.output))

    async def test_cleanup_failure_does_not_escape_with_sensitive_path(self):
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=MagicMock())
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(self.main.httpx, "AsyncClient", return_value=context), \
             patch.object(self.main, "send_telegram_message", new=AsyncMock(side_effect=RuntimeError(SENSITIVE))), \
             patch.object(Path, "unlink", side_effect=OSError(SENSITIVE)), \
             self.assertLogs("main", level="ERROR") as logs:
            await self.main.process_voice_message(123, "fake-file")
        self.assert_private("\n".join(logs.output))
        self.assertIn("ERROR:main:temporary_audio_cleanup_failed", logs.output)

    async def test_http_sdk_diagnostics_suppressed(self):
        configure_safe_logging()
        with self.assertLogs(level="INFO") as logs:
            for name in ("httpx", "httpcore.connection", "groq._base_client", "groq.future_child"):
                logging.getLogger(name).critical(SENSITIVE)
            logging.getLogger("main").info("safe_event")
        self.assertEqual(logs.output, ["INFO:main:safe_event"])
