"""Transactional shared enrollment and account-scoped durable work queue."""

import json
from pathlib import Path
import sqlite3
import threading
import time


class Store:
    def __init__(self, root):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            Path(root) / "multi-account.sqlite3", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS album_assets (
                album TEXT NOT NULL, account TEXT NOT NULL, asset TEXT NOT NULL,
                PRIMARY KEY(album,account,asset));
            CREATE TABLE IF NOT EXISTS enrollment (
                id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
            INSERT OR IGNORE INTO enrollment VALUES (1, '{"pets":{},"negatives":[]}');
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL,
                cursor TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000Z', error TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                account TEXT NOT NULL, asset TEXT NOT NULL, taken TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT, result TEXT,
                PRIMARY KEY(account,asset));
            CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(account,status,retry_at);
        """)

    def close(self):
        self.db.close()

    def enrollment(self):
        with self.lock:
            return json.loads(
                self.db.execute(
                    "SELECT document FROM enrollment WHERE id=1"
                ).fetchone()[0]
            )

    def save(self, document):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE enrollment SET document=? WHERE id=1", (json.dumps(document),)
            )

    def bind(self, account, owner):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT owner FROM accounts WHERE id=?", (account,)
            ).fetchone()
            if row and row[0] != owner:
                raise ValueError(
                    f"Account {account} belongs to a different stored Immich owner"
                )
            other = self.db.execute(
                "SELECT id FROM accounts WHERE owner=? AND id<>?", (owner, account)
            ).fetchone()
            if other:
                raise ValueError(
                    "Two configured accounts resolve to the same Immich owner"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO accounts(id,owner) VALUES (?,?)",
                (account, owner),
            )

    def cursor(self, account):
        with self.lock:
            return self.db.execute(
                "SELECT cursor FROM accounts WHERE id=?", (account,)
            ).fetchone()[0]

    def enqueue(self, account, assets, *, cursor=None, rescan=False):
        """Commit discoveries and their watermark together. Inclusive queries dedupe here."""
        with self.lock, self.db:
            for asset, taken in assets:
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(account,asset,taken) VALUES (?,?,?)",
                    (account, asset, taken),
                )
                if rescan:
                    self.db.execute(
                        "UPDATE jobs SET status='pending',attempts=0,retry_at=0,error=NULL WHERE account=? AND asset=?",
                        (account, asset),
                    )
            if cursor:
                self.db.execute(
                    "UPDATE accounts SET cursor=? WHERE id=?", (cursor, account)
                )

    def batch(self, account, size, ready_at=None):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM jobs WHERE account=? AND status<>'done' AND retry_at<=? ORDER BY attempts,taken,asset LIMIT ?",
                    (account, time.time() if ready_at is None else ready_at, size),
                )
            ]

    def ready_count(self, account, ready_at):
        with self.lock:
            return self.db.execute(
                "SELECT count(*) FROM jobs WHERE account=? AND status<>'done' AND retry_at<=?",
                (account, ready_at),
            ).fetchone()[0]

    def complete(self, account, asset, result):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET status='done',error=NULL,result=? WHERE account=? AND asset=?",
                (json.dumps(result), account, asset),
            )

    def failed(self, account, asset, error):
        with self.lock, self.db:
            attempts = (
                self.db.execute(
                    "SELECT attempts FROM jobs WHERE account=? AND asset=?",
                    (account, asset),
                ).fetchone()[0]
                + 1
            )
            delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
            self.db.execute(
                "UPDATE jobs SET status='retry',attempts=?,retry_at=?,error=? WHERE account=? AND asset=?",
                (attempts, time.time() + delay, error, account, asset),
            )

    def account_error(self, account, error):
        with self.lock, self.db:
            self.db.execute("UPDATE accounts SET error=? WHERE id=?", (error, account))

    def summary(self):
        with self.lock:
            return {
                r["id"]: {
                    "owner_id": r["owner"],
                    "cursor": r["cursor"],
                    "error": r["error"],
                    "jobs": {
                        j[0]: j[1]
                        for j in self.db.execute(
                            "SELECT status,count(*) FROM jobs WHERE account=? GROUP BY status",
                            (r["id"],),
                        )
                    },
                }
                for r in self.db.execute("SELECT * FROM accounts").fetchall()
            }

    def review(self, limit=40, excluded=()):
        with self.lock:
            rows = self.db.execute(
                """SELECT account,asset,taken,result FROM jobs
                WHERE status='done' AND result LIKE '%low_confidence%'
                AND NOT EXISTS (
                    SELECT 1 FROM json_each(?) AS reviewed
                    WHERE json_extract(reviewed.value, '$[0]') = jobs.account
                    AND json_extract(reviewed.value, '$[1]') = jobs.asset
                )
                ORDER BY taken DESC LIMIT ?""",
                (json.dumps(list(excluded)), limit),
            ).fetchall()
            return [
                {
                    "account": r["account"],
                    "asset_id": r["asset"],
                    "taken": r["taken"],
                    "matches": [
                        m
                        for m in json.loads(r["result"])
                        if m["outcome"] == "low_confidence"
                    ],
                }
                for r in rows
            ]

    def album_assets(self, album, account):
        with self.lock:
            return {r[0] for r in self.db.execute(
                "SELECT asset FROM album_assets WHERE album=? AND account=?", (album, account))}

    def record_album_assets(self, album, account, assets):
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO album_assets VALUES (?,?,?)",
                                [(album, account, asset) for asset in assets])

    def forget_album_assets(self, album, account, assets):
        with self.lock, self.db:
            self.db.executemany("DELETE FROM album_assets WHERE album=? AND account=? AND asset=?",
                                [(album, account, asset) for asset in assets])

    def clear_album_assets(self, album):
        with self.lock, self.db:
            self.db.execute("DELETE FROM album_assets WHERE album=?", (album,))
