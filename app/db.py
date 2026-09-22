"""SQLite 持久化层：会话、事件、投递尝试三张表。"""
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "replay.db"))
DB_PATH = os.path.abspath(DB_PATH)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    secret           TEXT NOT NULL,
    target_url       TEXT NOT NULL,
    speed            REAL NOT NULL DEFAULT 60.0,
    max_retries      INTEGER NOT NULL DEFAULT 3,
    retry_base_delay REAL NOT NULL DEFAULT 1.0,
    status           TEXT NOT NULL DEFAULT 'idle',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,          -- 按虚拟时间轴排序后的投递顺序
    ts                  REAL NOT NULL,             -- 事件时间戳（epoch 秒）
    body                TEXT NOT NULL,             -- 实际发送的 JSON 报文（签名基于此串）
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending/delivering/retrying/delivered/failed
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_response_code  INTEGER,
    last_error          TEXT,
    delivered_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);

CREATE TABLE IF NOT EXISTS attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    attempt_no      INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'auto',  -- auto=引擎自动投递 / manual=手动重试
    started_at      REAL NOT NULL,
    duration_ms     REAL,
    delay_before    REAL NOT NULL DEFAULT 0,       -- 本次尝试前等待的重试间隔（秒）
    signature       TEXT,
    sig_ts          TEXT,                          -- 签名所用的 X-Webhook-Timestamp
    response_code   INTEGER,
    response_body   TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_event ON attempts(event_id, attempt_no);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        d = os.path.dirname(DB_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        with _lock:
            _conn.executescript(SCHEMA)
    return _conn


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        cur = get_conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur.rowcount


def executemany(sql: str, seq_of_params) -> None:
    with _lock:
        get_conn().executemany(sql, seq_of_params)
        get_conn().commit()


def now() -> float:
    return time.time()
