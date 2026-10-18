import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import test_webhook_security as webhook_tests
from utils.limits import Capacity, positive_limit


class ResourceLimitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = webhook_tests.WebhookTests.asyncSetUp
    post = webhook_tests.WebhookTests.post

    async def test_request_limit_checks_actual_body_without_content_length(self):
        async def chunks():
            yield b" " * 32
            yield b" " * 33

        with patch.object(self.main, "MAX_WEBHOOK_BYTES", 64):
            self.assertEqual((await self.post(b" " * 65, self.headers, raw=True)).status_code, 413)
            request = httpx.Request("POST", "http://test/webhook", headers=self.headers, content=chunks())
            self.assertNotIn("content-length", request.headers)
            response = await self.transport.handle_async_request(request)
            self.assertEqual(response.status_code, 413)
            await response.aclose()
        self.process.assert_not_awaited()

    async def test_voice_metadata_boundaries(self):
        for field, value in (("duration", self.main.MAX_VOICE_SECONDS + 1),
                             ("file_size", self.main.MAX_AUDIO_BYTES + 1)):
            update = copy.deepcopy(self.update)
            update["message"]["voice"][field] = value
            response = await self.post(update, self.headers)
            self.assertEqual(response.json()["status"], "rejected")
        for field, value in (("duration", True), ("duration", -1), ("duration", "10"),
                             ("duration", None), ("file_size", 0), ("file_size", True)):
            update = copy.deepcopy(self.update)
            update["message"]["voice"][field] = value
            self.assertEqual((await self.post(update, self.headers)).status_code, 400)
        self.process.assert_not_awaited()
        update = copy.deepcopy(self.update)
        update["message"]["voice"].update(duration=self.main.MAX_VOICE_SECONDS,
                                          file_size=self.main.MAX_AUDIO_BYTES)
        self.assertEqual((await self.post(update, self.headers)).json()["status"], "processing")

    async def test_capacity_rejects_parallel_work_and_releases(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def wait_for_finish(*args):
            started.set()
            await finish.wait()

        self.process.side_effect = wait_for_finish
        capacity = Capacity(1)
        with patch.object(self.main, "job_capacity", capacity):
            task = asyncio.create_task(self.post(self.update, self.headers))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                response = await self.post(self.update, self.headers)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["retry-after"], "10")
                self.assertEqual(capacity.active, 1)
                self.assertEqual(self.process.await_count, 1)
            finally:
                finish.set()
                await task
            self.assertEqual(capacity.active, 0)
            self.update["update_id"] = 2
            self.assertEqual((await self.post(self.update, self.headers)).status_code, 200)

    async def test_capacity_releases_on_failure_timeout_and_cancellation(self):
        for mode in ("failure", "timeout", "cancel"):
            capacity = Capacity(1)
            capacity.acquire()
            started = asyncio.Event()

            async def work(*args):
                started.set()
                if mode == "failure":
                    raise RuntimeError("synthetic failure")
                await asyncio.Event().wait()

            self.process.side_effect = work
            with patch.object(self.main, "job_capacity", capacity), \
                 patch.object(self.main, "JOB_TIMEOUT_SECONDS", 0.02):
                task = asyncio.create_task(self.main.run_voice_job(123, "file"))
                await started.wait()
                if mode == "cancel":
                    task.cancel()
                if mode == "timeout":
                    with self.assertLogs("main", level="ERROR"):
                        await task
                else:
                    with self.assertRaises(RuntimeError if mode == "failure" else asyncio.CancelledError):
                        await task
            self.assertEqual(capacity.active, 0)


class AudioDownloadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = webhook_tests.WebhookTests.asyncSetUp

    def stream_client(self, chunks, headers=None):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in chunks:
                    yield chunk

        response = httpx.Response(200, headers=headers, stream=Body(),
                                  request=httpx.Request("GET", "https://example.invalid/audio"))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        client = MagicMock()
        client.stream.return_value = context
        return client

    async def test_stream_size_limits_and_empty_download(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.main, "MAX_AUDIO_BYTES", 4):
            for index, (chunks, headers) in enumerate((([b"12345"], {}), ([b"1"], {"content-length": "5"}),
                                                       ([b"12345"], {"content-length": "1"}), ([], {}))):
                path = Path(directory) / f"{index}.ogg"
                with self.assertRaises(ValueError):
                    await self.main.download_audio(self.stream_client(chunks, headers), "https://example.invalid", path)
                if path.exists():
                    self.assertLessEqual(path.stat().st_size, 4)
            path = Path(directory) / "valid.ogg"
            await self.main.download_audio(self.stream_client([b"12", b"34"]), "https://example.invalid", path)
            self.assertEqual(path.read_bytes(), b"1234")

    async def test_oversized_audio_is_cleaned_and_never_sent_to_groq(self):
        # Restore the real pipeline: setup mocks it for webhook dispatch tests.
        self.worker.stop()
        client = self.stream_client([b"12345"])
        client.get = AsyncMock(return_value=httpx.Response(
            200, json={"result": {"file_path": "audio.ogg"}},
            request=httpx.Request("GET", "https://example.invalid/getFile")))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(self.main, "TEMP_DIR", Path(directory)), \
             patch.object(self.main, "MAX_AUDIO_BYTES", 4), \
             patch.object(self.main.httpx, "AsyncClient", return_value=context), \
             patch.object(self.main, "send_telegram_message", new_callable=AsyncMock), \
             patch.object(self.main, "transcribe_audio", new_callable=AsyncMock) as stt, \
             patch.object(self.main, "generate_soap_note", new_callable=AsyncMock) as llm, \
             self.assertLogs("main", level="ERROR"):
            await self.main.process_voice_message(123, "file")
            stt.assert_not_awaited()
            llm.assert_not_awaited()
            self.assertEqual(list(Path(directory).iterdir()), [])


class LimitConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_fails_closed(self):
        for value in ("", "0", "-1", "1.5", "true", "17", "１２"):
            with patch.dict("os.environ", {"TEST_LIMIT": value}), self.assertRaises(RuntimeError):
                positive_limit("TEST_LIMIT", 2, 16)
