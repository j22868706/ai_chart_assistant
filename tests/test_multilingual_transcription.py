"""Verify automatic language configuration and preservation, without a provider."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx


class MultilingualTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1", "GROQ_API_KEY": "offline-key"})
        env.start()
        self.addCleanup(env.stop)
        self.service = importlib.import_module("services.stt_service")
        network = patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audio_path = Path(directory.name) / "synthetic.ogg"
        self.audio_path.write_bytes(b"synthetic-audio-not-a-recording")

    async def test_auto_language_omits_hint_and_preserves_source_transcript(self):
        sources = [
            "Patient denies pain. Completed 2 sets of 10 right shoulder exercises.",
            "病人否認疼痛。右肩運動兩組，每組十次。",
            "病人 denies pain，右肩運動 2 sets of 10，使用 RW 行走 50 ft，需要 SBA。",
            "El paciente niega dolor. Hizo 2 series de 10 ejercicios del hombro derecho.",
        ]
        with patch.object(self.service, "STT_LANGUAGE", None), \
             patch.object(self.service.client.audio.transcriptions, "create", new_callable=AsyncMock) as create:
            for source in sources:
                create.return_value = SimpleNamespace(text=source)
                self.assertEqual(await self.service.transcribe_audio(str(self.audio_path)), source)
                arguments = create.await_args.kwargs
                self.assertNotIn("language", arguments)
                self.assertEqual(arguments["file"][1], self.audio_path.read_bytes())

    async def test_explicit_input_language_hint_remains_available(self):
        with patch.object(self.service.client.audio.transcriptions, "create", new_callable=AsyncMock) as create:
            create.return_value = SimpleNamespace(text="synthetic source")
            for language in ("en", "zh", "es"):
                with patch.object(self.service, "STT_LANGUAGE", language):
                    await self.service.transcribe_audio(str(self.audio_path))
                self.assertEqual(create.await_args.kwargs["language"], language)


class LanguageConfigurationTests(unittest.TestCase):
    def test_fresh_import_defaults_to_auto_detection(self):
        # A new process is needed to test environment parsing at module import.
        code = (
            "import asyncio,json; from services import stt_service; "
            "print(json.dumps(stt_service.STT_LANGUAGE)); "
            "asyncio.run(stt_service.client.close())"
        )
        for configured, expected in ((None, None), ("", None), ("  ", None), (" EN ", "en")):
            env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", GROQ_API_KEY="offline-key")
            env.pop("GROQ_STT_LANGUAGE", None)
            if configured is not None:
                env["GROQ_STT_LANGUAGE"] = configured
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), expected)
