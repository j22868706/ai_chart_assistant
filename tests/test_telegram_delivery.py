import importlib
import json
import os
import unittest
from unittest.mock import patch

import httpx


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1", "GROQ_API_KEY": "offline-key",
            "TELEGRAM_BOT_TOKEN": "offline-token", "TELEGRAM_WEBHOOK_SECRET": "offline-secret",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        })
        env.start()
        self.addCleanup(env.stop)
        self.main = importlib.import_module("main")
        network = patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                               side_effect=AssertionError("External network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    async def send(self, replies, text="synthetic note"):
        self.requests = []

        def respond(request):
            self.requests.append(json.loads(request.content))
            reply = replies[len(self.requests) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await self.main.send_telegram_message(client, 123, text)

    async def test_nonformat_errors_are_not_retried(self):
        for status in (400, 401, 403, 429, 500, 503):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "^Telegram message delivery failed$"):
                await self.send([httpx.Response(status, json={"ok": False, "error_code": status,
                                                              "description": "sensitive-provider-text"})])
            self.assertEqual(len(self.requests), 1)

    async def test_malformed_or_unsuccessful_200_is_rejected(self):
        replies = [httpx.Response(200, content=b"not JSON sensitive-provider-text")]
        replies += [httpx.Response(200, json=value) for value in (
            [], None, {}, {"ok": False}, {"ok": "true"}, {"ok": 1},
        )]
        for reply in replies:
            with self.subTest(body=reply.content), self.assertRaisesRegex(RuntimeError, "^Telegram message delivery failed$"):
                await self.send([reply])
            self.assertEqual(len(self.requests), 1)

    async def test_plain_text_retry_checked_and_later_chunks_stopped(self):
        parse_error = lambda: httpx.Response(400, json={"ok": False, "error_code": 400,
            "description": "Bad Request: can't parse entities: synthetic"})
        for fallback in (httpx.Response(403, json={"ok": False}),
                         httpx.Response(200, json={"ok": False}), parse_error()):
            with self.assertRaisesRegex(RuntimeError, "^Telegram message delivery failed$"):
                await self.send([parse_error(), fallback], text="x" * 5000)
            self.assertEqual(len(self.requests), 2)
            self.assertIn("parse_mode", self.requests[0])
            self.assertNotIn("parse_mode", self.requests[1])
            self.assertEqual(self.requests[0]["text"], self.requests[1]["text"])

    async def test_network_error_is_sanitized_and_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, "^Telegram message delivery failed$") as error:
            await self.send([httpx.ReadTimeout("sensitive-token-and-content")])
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(len(self.requests), 1)
