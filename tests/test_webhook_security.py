"""Offline access-control tests; real credentials and network are never used."""

import copy
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from utils.security import parse_allowed_user_ids, validate_webhook_secret
from utils.update_store import UpdateStore


class ConfigurationTests(unittest.TestCase):
    def test_secret_validation(self):
        for value in (None, "", "has spaces", "é", "x" * 257):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_webhook_secret(value)
        self.assertEqual(validate_webhook_secret("valid_secret-123"), "valid_secret-123")

    def test_allowlist_validation(self):
        for value in (None, "", "123,", "name", "0", "-1", "1.0", str(2**52)):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                parse_allowed_user_ids(value)
        self.assertEqual(parse_allowed_user_ids("123, 456,123"), frozenset({123, 456}))


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "GROQ_API_KEY": "test-key",
            "TELEGRAM_WEBHOOK_SECRET": "test-secret",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network = patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.main = importlib.import_module("main")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = patch.object(self.main, "update_store", UpdateStore(Path(directory.name) / "updates.sqlite3"))
        store.start()
        self.addCleanup(store.stop)
        self.worker = patch.object(self.main, "process_voice_message", new_callable=AsyncMock)
        self.process = self.worker.start()
        self.addCleanup(self.worker.stop)
        # ASGITransport calls the app directly; all client network sends stay blocked.
        self.transport = httpx.ASGITransport(app=self.main.app)
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        self.update = {"update_id": 1, "message": {
            "from": {"id": 123, "is_bot": False},
            "chat": {"id": 123, "type": "private"},
            "voice": {"file_id": "test-file", "duration": 10},
        }}

    async def post(self, payload, headers=None, raw=False):
        request = httpx.Request("POST", "http://test/webhook", headers=headers or {},
                                **({"content": payload} if raw else {"json": payload}))
        response = await self.transport.handle_async_request(request)
        await response.aread()
        await response.aclose()
        return response

    async def test_secret_rejected_before_json_parsing(self):
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                        [("X-Telegram-Bot-Api-Secret-Token", "test-secret")] * 2):
            self.assertEqual((await self.post(b"invalid", headers, raw=True)).status_code, 403)
        self.process.assert_not_awaited()

    async def test_allowed_private_voice(self):
        response = await self.post(self.update, self.headers)
        self.assertEqual(response.json(), {"status": "processing"})
        self.process.assert_awaited_once_with(123, "test-file")

    async def test_denied_messages_never_dispatch(self):
        changes = [
            ("from", "id", 456), ("from", "id", "123"),
            ("from", "id", True), ("from", "is_bot", True),
            ("chat", "type", "group"), ("chat", "type", "supergroup"),
            ("chat", "type", "channel"), ("chat", "id", 456),
        ]
        for section, key, value in changes:
            update = copy.deepcopy(self.update)
            update["message"][section][key] = value
            self.assertEqual((await self.post(update, self.headers)).json(), {"status": "ignored"})
        for field in ("from", "chat"):
            update = copy.deepcopy(self.update)
            del update["message"][field]
            self.assertEqual((await self.post(update, self.headers)).json(), {"status": "ignored"})
        self.process.assert_not_awaited()

    async def test_malformed_updates(self):
        for payload in ([], None, "text"):
            self.assertEqual((await self.post(payload, self.headers)).status_code, 400)
        self.assertEqual((await self.post(b"{", self.headers, raw=True)).status_code, 400)
        for voice in (None, {}, {"file_id": 123}, {"file_id": ""}):
            update = copy.deepcopy(self.update)
            update["message"]["voice"] = voice
            self.assertEqual((await self.post(update, self.headers)).status_code, 400)
        self.process.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
