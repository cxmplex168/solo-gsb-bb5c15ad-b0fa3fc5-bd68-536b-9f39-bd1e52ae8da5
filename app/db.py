"""SQLite persistence layer (async via aiosqlite).

The database holds four groups of tables:

* sessions / events        -- replay session configuration and its timeline
* deliveries / attempts    -- every HTTP delivery made by the replay engine
* catchboxes / catches     -- the built-in local receiver endpoint
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

DB_PATH = os.environ.get("WEBHOOK_SANDBOX_DB", "/data/sandbox.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'created',
    secret             TEXT NOT NULL DEFAULT '',
    target_url         TEXT NOT NULL,
    signature_scheme   TEXT NOT NULL DEFAULT 'hmac-sha256',
    speed              REAL NOT NULL DEFAULT 1,
    retry_max          INTEGER NOT NULL DEFAULT 3,
    retry_base_ms      INTEGER NOT NULL DEFAULT 1000,
    retry_factor       REAL NOT NULL DEFAULT 2.0,
    on_failure         TEXT NOT NULL DEFAULT 'continue',
    header_prefix      TEXT NOT NULL DEFAULT 'X-Webhook',
    created_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL,
    cursor             INTEGER NOT NULL DEFAULT 0,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    event_id    TEXT,
    type        TEXT,
    timestamp   TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, idx);

CREATE TABLE IF NOT EXISTS deliveries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_idx      INTEGER NOT NULL,
    event_id       TEXT,
    type           TEXT,
    event_ts       TEXT,
    virtual_ts     TEXT,
    target_url     TEXT NOT NULL,
    signature      TEXT,
    status_code    INTEGER,
    state          TEXT NOT NULL DEFAULT 'pending',
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 1,
    error          TEXT,
    delivered_at   REAL,
    attempts_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_deliveries_session ON deliveries(session_id, event_idx);

CREATE TABLE IF NOT EXISTS catchboxes (
    token        TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    fail_status  INTEGER NOT NULL DEFAULT 0,
    fail_every   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL REFERENCES catchboxes(token) ON DELETE CASCADE,
    received_at REAL NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    headers     TEXT NOT NULL,
    body        TEXT,
    status_code INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catches_token ON catches(token, id DESC);
"""


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect():
    """Yield a short-lived row-factory connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
