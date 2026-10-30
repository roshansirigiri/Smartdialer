"""SQLite wrapper. Every state change happens inside one BEGIN IMMEDIATE transaction.

BEGIN IMMEDIATE takes the write lock up front, so two workers cannot interleave
read-then-write. Every UPDATE that claims something is *also* guarded by
`WHERE state = <expected>` (compare-and-set), so the code stays correct if this
is ported to PostgreSQL READ COMMITTED (then add FOR UPDATE SKIP LOCKED to the
candidate selects).
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  agent_id TEXT PRIMARY KEY, campaign TEXT NOT NULL, state TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0, call_id TEXT, pending_state TEXT,
  wrap_until REAL, state_since REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_agents_cs ON agents(campaign, state, state_since);

CREATE TABLE IF NOT EXISTS borrowers(
  borrower_id TEXT PRIMARY KEY, campaign TEXT NOT NULL, phone TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'READY', attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0, call_id TEXT, priority INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_borrowers_ready ON borrowers(campaign, state, next_attempt_at, priority);

CREATE TABLE IF NOT EXISTS calls(
  call_id TEXT PRIMARY KEY, campaign TEXT NOT NULL, agent_id TEXT, borrower_id TEXT NOT NULL,
  state TEXT NOT NULL, mode TEXT NOT NULL, provider TEXT, provider_call_id TEXT,
  created_at REAL NOT NULL, initiated_at REAL, answered_at REAL, connected_at REAL, ended_at REAL,
  updated_at REAL NOT NULL, fail_reason TEXT, abandoned INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_calls_cs ON calls(campaign, state);
CREATE INDEX IF NOT EXISTS ix_calls_pcid ON calls(provider, provider_call_id);
CREATE INDEX IF NOT EXISTS ix_calls_answered ON calls(campaign, answered_at);
CREATE INDEX IF NOT EXISTS ix_calls_ended ON calls(campaign, ended_at);

CREATE TABLE IF NOT EXISTS call_events(
  provider TEXT NOT NULL, event_id TEXT NOT NULL, call_id TEXT, type TEXT, outcome TEXT, ts REAL,
  PRIMARY KEY(provider, event_id));

CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, campaign TEXT, mode TEXT, requested INTEGER,
  approved INTEGER, action TEXT, reasons TEXT, explain TEXT);

CREATE TABLE IF NOT EXISTS leases(
  name TEXT PRIMARY KEY, owner TEXT, expires REAL);
"""


class Database:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._memory = path == ":memory:"
        self._local = threading.local()
        self._lock = threading.RLock()
        self._shared = self._open() if self._memory else None
        if not self._memory:
            self._conn()  # creates schema

    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=60)
        c.row_factory = sqlite3.Row
        if not self._memory:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
        return c

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            return self._shared
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._open()
        return c

    @contextlib.contextmanager
    def txn(self):
        """All-or-nothing unit of work. Do not nest."""
        lock = self._lock if self._memory else contextlib.nullcontext()
        with lock:
            c = self._conn()
            if getattr(self._local, "in_txn", False):
                raise RuntimeError("nested transaction")
            self._local.in_txn = True
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")
            finally:
                self._local.in_txn = False

    def query(self, sql: str, args=()):
        with self.txn() as c:
            return c.execute(sql, args).fetchall()
