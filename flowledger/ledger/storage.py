"""Durable SQLite (WAL) persistence for the ledger, with hash-verified restore."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flowledger.ledger.engine import LedgerEngine
from flowledger.ledger.models import Currency, LedgerPosting


class StorageIntegrityError(Exception):
    """Raised when a restored ledger does not reproduce the stored hash chain."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY, name TEXT NOT NULL, account_type TEXT NOT NULL, currency TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT UNIQUE NOT NULL, reference TEXT NOT NULL,
  description TEXT NOT NULL, currency TEXT NOT NULL, postings TEXT NOT NULL, timestamp TEXT NOT NULL,
  prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
  BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
  BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


class SqliteLedgerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def journal_mode(self) -> str:
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def persist(self, engine: LedgerEngine) -> int:
        """Atomically write all accounts and any journal entries not yet stored. Returns rows added."""
        added = 0
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            for a in engine.list_accounts():
                c.execute(
                    "INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)",
                    (a.account_id, a.name, a.account_type.value, a.currency.value),
                )
            for e in engine.list_entries():
                cur = c.execute(
                    "INSERT OR IGNORE INTO journal (entry_id,reference,description,currency,postings,"
                    "timestamp,prev_hash,entry_hash,metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                    (e.entry_id, e.reference, e.description, e.currency.value,
                     json.dumps([p.to_dict() for p in e.postings]), e.timestamp, e.prev_hash,
                     e.entry_hash, json.dumps(e.metadata, sort_keys=True)),
                )
                added += cur.rowcount
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return added

    def restore(self) -> LedgerEngine:
        """Rebuild a LedgerEngine by replaying the journal; verify every stored hash is reproduced."""
        engine = LedgerEngine()
        for aid, name, atype, cur in self._conn.execute("SELECT * FROM accounts"):
            engine.create_account(aid, name, atype, cur)
        for row in self._conn.execute("SELECT * FROM journal ORDER BY seq"):
            _, entry_id, ref, desc, cur, postings, ts, prev_hash, entry_hash, meta = row
            entry = engine.record_journal_entry(
                reference=ref, description=desc, currency=Currency(cur),
                postings=[LedgerPosting(**{**p}) for p in json.loads(postings)],
                metadata=json.loads(meta), entry_id=entry_id, timestamp=ts,
            )
            if entry.prev_hash != prev_hash or entry.entry_hash != entry_hash:
                raise StorageIntegrityError(f"Hash mismatch replaying entry {entry_id}")
        return engine
