import asyncio
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_webhook_security as webhook_tests
from utils.update_store import UpdateStore


class UpdateReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = webhook_tests.WebhookTests.asyncSetUp
    post = webhook_tests.WebhookTests.post

    async def test_repeated_updates_dispatch_once_even_after_reopening_database(self):
        self.assertEqual((await self.post(self.update, self.headers)).json()["status"], "processing")
        self.main.update_store = UpdateStore(self.main.update_store.path)
        self.assertEqual((await self.post(self.update, self.headers)).json(), {"status": "duplicate"})
        self.process.assert_awaited_once_with(123, "test-file")
        self.assertEqual(self.main.job_capacity.active, 0)

    async def test_parallel_retries_dispatch_once(self):
        responses = await asyncio.gather(*(self.post(self.update, self.headers) for _ in range(2)))
        self.assertEqual(sorted(r.json()["status"] for r in responses), ["duplicate", "processing"])
        self.assertEqual(self.process.await_count, 1)
        self.assertEqual(self.main.job_capacity.active, 0)

    async def test_invalid_id_not_recorded_or_dispatched(self):
        for value in (None, True, "1", -1, 2**63):
            update = copy.deepcopy(self.update)
            update["update_id"] = value
            self.assertEqual((await self.post(update, self.headers)).status_code, 400)
        self.process.assert_not_awaited()
        self.assertFalse(self.main.update_store.path.exists())

    async def test_store_failure_releases_capacity_and_allows_retry(self):
        with patch.object(self.main.update_store, "claim", side_effect=OSError("fake-secret")), \
             self.assertLogs("main", level="ERROR") as logs:
            response = await self.post(self.update, self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("fake-secret", str(logs.output) + response.text)
        self.assertEqual(self.main.job_capacity.active, 0)
        self.process.assert_not_awaited()
        self.assertEqual((await self.post(self.update, self.headers)).json()["status"], "processing")

    async def test_denied_sender_cannot_poison_receipt(self):
        update = copy.deepcopy(self.update)
        update["message"]["from"]["id"] = 999
        self.assertEqual((await self.post(update, self.headers)).json()["status"], "ignored")
        self.assertFalse(self.main.update_store.path.exists())
        self.assertEqual((await self.post(self.update, self.headers)).json()["status"], "processing")

    async def test_failed_job_is_not_automatically_dispatched_again(self):
        self.process.side_effect = RuntimeError("synthetic job failure")
        with self.assertRaises(RuntimeError):
            await self.post(self.update, self.headers)
        self.assertEqual((await self.post(self.update, self.headers)).json()["status"], "duplicate")
        self.assertEqual(self.process.await_count, 1)
        self.assertEqual(self.main.job_capacity.active, 0)


class SQLiteStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_claim_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.sqlite3"
            results = await asyncio.gather(*(asyncio.to_thread(UpdateStore(path).claim, 42) for _ in range(8)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(path) as db:
                columns = [row[1] for row in db.execute("PRAGMA table_info(receipts)")]
            self.assertEqual(columns, ["update_id", "received_at"])

    async def test_retention_and_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateStore(Path(directory) / "receipts.sqlite3", retention_seconds=10, max_entries=1)
            with patch("utils.update_store.time.time", return_value=100):
                self.assertTrue(store.claim(1))
                self.assertFalse(store.claim(1))
                with self.assertRaises(RuntimeError):
                    store.claim(2)
            with patch("utils.update_store.time.time", return_value=111):
                self.assertTrue(store.claim(2))

    async def test_database_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            target = path / "target"
            target.write_text("unchanged")
            link = path / "receipts.sqlite3"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                UpdateStore(link).claim(1)
            self.assertEqual(target.read_text(), "unchanged")
