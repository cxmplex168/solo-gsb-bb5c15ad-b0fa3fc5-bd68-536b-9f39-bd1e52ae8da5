"""FastAPI application: REST API, built-in catch-all receiver, static UI."""
from __future__ import annotations

import csv
import io
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db as database
from .db import connect, dumps
from .engine import EngineError, ReplayEngine
from .models import CatchBoxCreate, CatchBoxUpdate, SessionCreate, SessionUpdate
from .util import iso, parse_events_text

engine = ReplayEngine()

CATCH_BODY_LIMIT = 64 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    await engine.startup()
    yield
    await engine.shutdown()


app = FastAPI(title="Webhook 事件回放沙箱", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers


def _row(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else None  # type: ignore[arg-type]


async def _load_session(session_id: str) -> Dict[str, Any]:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "会话不存在")
    return dict(row)


async def _load_events(db, session_id: str) -> List[Dict[str, Any]]:
    cur = await db.execute(
        "SELECT idx, event_id, type, timestamp, payload FROM events "
        "WHERE session_id=? ORDER BY idx",
        (session_id,),
    )
    rows = []
    for r in await cur.fetchall():
        item = dict(r)
        item["event_id"] = _safe_json(item["event_id"])
        item["payload"] = _safe_json(item["payload"])
        rows.append(item)
    return rows


async def _load_deliveries(db, session_id: str) -> List[Dict[str, Any]]:
    cur = await db.execute(
        "SELECT * FROM deliveries WHERE session_id=? ORDER BY event_idx", (session_id,)
    )
    rows = []
    for r in await cur.fetchall():
        item = dict(r)
        item["event_id"] = _safe_json(item["event_id"])
        item["attempts"] = json.loads(item.pop("attempts_json") or "[]")
        rows.append(item)
    return rows


def _safe_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


# --------------------------------------------------------------------------- session API


@app.post("/api/parse-preview")
async def parse_preview(body: Dict[str, Any]) -> Dict[str, Any]:
    """Parse pasted events without persisting anything (used by the UI)."""
    try:
        events = parse_events_text(body.get("events_text", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "count": len(events),
        "start": iso(events[0]["timestamp"]),
        "end": iso(events[-1]["timestamp"]),
        "span_ms": int(
            (events[-1]["timestamp"] - events[0]["timestamp"]).total_seconds() * 1000
        ),
        "events": [
            {
                "idx": i,
                "event_id": e["event_id"],
                "type": e["type"],
                "timestamp": iso(e["timestamp"]),
                "payload": e["payload"],
            }
            for i, e in enumerate(events)
        ],
    }


@app.post("/api/sessions", status_code=201)
async def create_session(body: SessionCreate) -> Dict[str, Any]:
    try:
        events = parse_events_text(body.events_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    session_id = uuid.uuid4().hex[:12]
    name = body.name.strip() or f"回放 {time.strftime('%m-%d %H:%M')}"
    now = time.time()
    async with connect() as db:
        await db.execute(
            "INSERT INTO sessions (id, name, status, secret, target_url, signature_scheme, "
            "speed, retry_max, retry_base_ms, retry_factor, on_failure, header_prefix, "
            "created_at, cursor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                session_id,
                name,
                "created",
                body.secret,
                body.target_url,
                body.signature_scheme,
                body.speed,
                body.retry_max,
                body.retry_base_ms,
                body.retry_factor,
                body.on_failure,
                body.header_prefix,
                now,
            ),
        )
        await db.executemany(
            "INSERT INTO events (session_id, idx, event_id, type, timestamp, payload) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    session_id,
                    i,
                    dumps(e["event_id"]),
                    str(e["type"]),
                    iso(e["timestamp"]),
                    dumps(e["payload"]),
                )
                for i, e in enumerate(events)
            ],
        )
    if body.autostart:
        try:
            await engine.start(session_id)
        except EngineError as exc:
            raise HTTPException(409, str(exc))
    return await _load_session(session_id)


@app.get("/api/sessions")
async def list_sessions() -> List[Dict[str, Any]]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id) AS event_count, "
            "(SELECT COUNT(*) FROM deliveries d WHERE d.session_id=s.id AND d.state='success') "
            "AS delivered_count "
            "FROM sessions s ORDER BY s.created_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> Dict[str, Any]:
    session = await _load_session(session_id)
    async with connect() as db:
        session["events"] = await _load_events(db, session_id)
        session["deliveries"] = await _load_deliveries(db, session_id)
    return session


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate) -> Dict[str, Any]:
    session = await _load_session(session_id)
    fields = body.model_dump(exclude_none=True)
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        async with connect() as db:
            await db.execute(
                f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), session_id)
            )
        if "speed" in fields and session["status"] == "running":
            await engine.set_speed(session_id, fields["speed"])
    return await _load_session(session_id)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    rt = engine.get_runtime(session_id)
    if rt is not None:
        await engine.stop(session_id)
    async with connect() as db:
        await db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    return {"status": "deleted"}


@app.post("/api/sessions/{session_id}/start")
async def start_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    try:
        await engine.start(session_id)
    except EngineError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "running"}


@app.post("/api/sessions/{session_id}/pause")
async def pause_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    try:
        await engine.pause(session_id)
    except EngineError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "paused"}


@app.post("/api/sessions/{session_id}/resume")
async def resume_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    try:
        await engine.resume(session_id)
    except EngineError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "running"}


@app.post("/api/sessions/{session_id}/step")
async def step_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    rt = engine.get_runtime(session_id)
    if rt is None:
        raise HTTPException(409, "会话未在运行，请先启动")
    try:
        await engine.step(session_id)
    except EngineError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "paused"}


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> Dict[str, str]:
    await _load_session(session_id)
    await engine.stop(session_id)
    return {"status": "stopped"}


@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "json") -> Response:
    """Download the full replay record (config + timeline + deliveries)."""
    session = await _load_session(session_id)
    async with connect() as db:
        events = await _load_events(db, session_id)
        deliveries = await _load_deliveries(db, session_id)
    record = {
        "exported_at": iso(datetime.now(timezone.utc)),
        "session": session,
        "events": events,
        "deliveries": deliveries,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "event_idx",
                "event_id",
                "type",
                "event_ts",
                "virtual_ts",
                "state",
                "status_code",
                "attempt_count",
                "max_attempts",
                "signature",
                "error",
                "retry_intervals_ms",
                "target_url",
            ]
        )
        for d in deliveries:
            writer.writerow(
                [
                    d["event_idx"],
                    d["event_id"],
                    d["type"],
                    d["event_ts"],
                    d["virtual_ts"],
                    d["state"],
                    d["status_code"],
                    d["attempt_count"],
                    d["max_attempts"],
                    d["signature"],
                    d["error"] or "",
                    " ".join(
                        str(a.get("scheduled_in_ms", 0)) for a in d["attempts"][1:]
                    ),
                    d["target_url"],
                ]
            )
        data = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="replay-{session_id}-{stamp}.csv"'
            },
        )
    return StreamingResponse(
        io.BytesIO(
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        ),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="replay-{session_id}-{stamp}.json"'
        },
    )


# --------------------------------------------------------------------------- catch box API


@app.post("/api/catchboxes", status_code=201)
async def create_catchbox(body: CatchBoxCreate) -> Dict[str, Any]:
    token = uuid.uuid4().hex
    async with connect() as db:
        await db.execute(
            "INSERT INTO catchboxes (token, name, fail_status, fail_every, created_at, "
            "request_count, fail_count) VALUES (?,?,?,?,?,0,0)",
            (token, body.name, body.fail_status, body.fail_every, time.time()),
        )
    return await _get_catchbox(token)


@app.get("/api/catchboxes")
async def list_catchboxes() -> List[Dict[str, Any]]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM catchboxes ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def _get_catchbox(token: str) -> Dict[str, Any]:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM catchboxes WHERE token=?", (token,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "接收端不存在")
    return dict(row)


@app.get("/api/catchboxes/{token}")
async def get_catchbox(token: str) -> Dict[str, Any]:
    return await _get_catchbox(token)


@app.patch("/api/catchboxes/{token}")
async def update_catchbox(token: str, body: CatchBoxUpdate) -> Dict[str, Any]:
    await _get_catchbox(token)
    fields = body.model_dump(exclude_none=True)
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        async with connect() as db:
            await db.execute(
                f"UPDATE catchboxes SET {sets} WHERE token=?", (*fields.values(), token)
            )
    return await _get_catchbox(token)


@app.delete("/api/catchboxes/{token}")
async def delete_catchbox(token: str) -> Dict[str, str]:
    await _get_catchbox(token)
    async with connect() as db:
        await db.execute("DELETE FROM catchboxes WHERE token=?", (token,))
    return {"status": "deleted"}


@app.get("/api/catchboxes/{token}/requests")
async def list_catches(token: str, limit: int = 100) -> List[Dict[str, Any]]:
    await _get_catchbox(token)
    limit = max(1, min(limit, 1000))
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM catches WHERE token=? ORDER BY id DESC LIMIT ?",
            (token, limit),
        )
        rows = []
        for r in await cur.fetchall():
            item = dict(r)
            item["headers"] = json.loads(item["headers"])
            rows.append(item)
        return rows


@app.delete("/api/catchboxes/{token}/requests")
async def clear_catches(token: str) -> Dict[str, str]:
    await _get_catchbox(token)
    async with connect() as db:
        await db.execute("DELETE FROM catches WHERE token=?", (token,))
        await db.execute(
            "UPDATE catchboxes SET request_count=0, fail_count=0 WHERE token=?", (token,)
        )
    return {"status": "cleared"}


# --------------------------------------------------------------------------- built-in receiver


@app.post("/catch/{token}")
async def catch_webhook(token: str, request: Request, force_status: Optional[int] = None) -> Response:
    """The built-in local receiver. Accepts any POST and records everything."""
    async with connect() as db:
        cur = await db.execute("SELECT * FROM catchboxes WHERE token=?", (token,))
        box = await cur.fetchone()
        if box is None:
            raise HTTPException(404, "接收端不存在或已删除")
        box = dict(box)

        raw = await request.body()
        body_text = raw[:CATCH_BODY_LIMIT].decode("utf-8", errors="replace")
        if len(raw) > CATCH_BODY_LIMIT:
            body_text += f"\n…（已截断，原始大小 {len(raw)} 字节）"

        status_code = 200
        if force_status is not None and 100 <= force_status <= 599:
            status_code = force_status
        elif box["fail_status"]:
            every = box["fail_every"]
            count = box["request_count"] + 1
            if every <= 1 or (every and ((count - 1) % every) == 0):
                status_code = box["fail_status"]

        headers = {k: v for k, v in request.headers.items()}
        await db.execute(
            "INSERT INTO catches (token, received_at, method, path, headers, body, status_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                token,
                time.time(),
                request.method,
                str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
                dumps(headers),
                body_text,
                status_code,
            ),
        )
        await db.execute(
            "UPDATE catchboxes SET request_count=request_count+1, "
            "fail_count=fail_count+? WHERE token=?",
            (1 if status_code >= 400 else 0, token),
        )

    return JSONResponse(
        status_code=status_code,
        content={"ok": 200 <= status_code < 300, "status": status_code, "received": len(raw)},
    )


@app.get("/catch/{token}")
async def catch_info(token: str) -> Dict[str, str]:
    return {"token": token, "hint": "向此 URL 发送 POST 请求即可被接收端记录"}


# --------------------------------------------------------------------------- meta / static


@app.get("/api/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
