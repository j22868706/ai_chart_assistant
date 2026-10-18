"""Bounded, durable receipt deduplication, not a durable job queue."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time


class UpdateStore:
    def __init__(self, path: Path, retention_seconds: int = 7 * 86400, max_entries: int = 100000):
        self.path = path
        self.retention_seconds = retention_seconds
        self.max_entries = max_entries

    def claim(self, update_id: int) -> bool:
        """Atomically accept a new receipt; False means already accepted."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Do not follow a database-file symlink; restrict existing files as well.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=1.0)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS receipts (update_id INTEGER PRIMARY KEY, received_at REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS receipt_age ON receipts(received_at)")
                now = time.time()
                db.execute("DELETE FROM receipts WHERE received_at < ?", (now - self.retention_seconds,))
                if db.execute("SELECT 1 FROM receipts WHERE update_id = ?", (update_id,)).fetchone():
                    return False
                if db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= self.max_entries:
                    raise RuntimeError("Update receipt capacity reached")
                db.execute("INSERT INTO receipts VALUES (?, ?)", (update_id, now))
        return True
