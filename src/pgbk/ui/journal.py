"""All request transitions and audit records commit before acknowledging a write."""
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from .common import Problem, sha, utcnow


class Journal:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, subject TEXT, version INTEGER, expires REAL, csrf TEXT);
        CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, started REAL, count INTEGER);
        CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, subject TEXT, profile TEXT, issued REAL,
          template TEXT, name TEXT UNIQUE, state TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS audit (time TEXT, subject TEXT, profile TEXT, decision TEXT, requestId TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS catalog (scope TEXT PRIMARY KEY, updated TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('cursor-key', ?)", (secrets.token_hex(32),))
            self.cursor_key = db.execute("SELECT value FROM settings WHERE key='cursor-key'").fetchone()[0]

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise Problem("JOURNAL_UNAVAILABLE") from None
            except BaseException:
                self.db.rollback()
                raise

    def audit(self, subject, profile, decision, request_id, result=None):
        with self.transaction() as db:
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)",
                       (utcnow(), subject, profile, decision, request_id, json.dumps(result)))

    def rate_limit(self, peer):
        now = time.time()
        with self.transaction() as db:
            for bucket, limit in [("global", 100), (sha(peer.encode()), 10)]:
                row = db.execute("SELECT * FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                count = row["count"] + 1 if row and now - row["started"] < 60 else 1
                start = row["started"] if row and now - row["started"] < 60 else now
                if count > limit:
                    raise Problem("LOGIN_RATE_LIMITED", 429)
                db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)", (bucket, start, count))

    def issue(self, subject, profile, template):
        key = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, 'issued', NULL)",
                       (sha(key.encode()), subject, profile, time.time(), template, "pgbk-ui-" + sha(key.encode())[:40]))
        return key

    def reserve(self, key, subject, profile, template):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (sha(key.encode()),)).fetchone()
            if not row or time.time() - row["issued"] > 86400:
                raise Problem("LAUNCH_TOKEN_EXPIRED", 409)
            if (row["subject"], row["profile"]) != (subject, profile):
                raise Problem("IDEMPOTENCY_CONFLICT", 409)
            if row["state"] == "issued" and row["template"] != template:
                raise Problem("TEMPLATE_CHANGED", 409)
            owner = row["state"] == "issued"
            if owner:
                db.execute("UPDATE requests SET state='reserved' WHERE id=?", (row["id"],))
                db.execute("INSERT INTO audit VALUES (?, ?, ?, 'reserved', ?, NULL)",
                           (utcnow(), subject, profile, row["id"]))
            return dict(row), owner

    def finish(self, request_id, result, subject, profile):
        with self.transaction() as db:
            db.execute("UPDATE requests SET state='accepted', result=? WHERE id=?", (json.dumps(result), request_id))
            db.execute("INSERT INTO audit VALUES (?, ?, ?, 'accepted', ?, ?)",
                       (utcnow(), subject, profile, request_id, json.dumps(result)))

    def history(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM requests WHERE state != 'issued'")]

    def cache(self, scope, data):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO catalog VALUES (?, ?, ?)", (scope, utcnow(), json.dumps(data)))

    def cached(self, scope):
        with self.lock:
            row = self.db.execute("SELECT * FROM catalog WHERE scope=?", (scope,)).fetchone()
            return (json.loads(row["data"]), row["updated"]) if row else (None, None)
