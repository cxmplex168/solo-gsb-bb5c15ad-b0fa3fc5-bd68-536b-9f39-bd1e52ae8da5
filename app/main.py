"""Webhook 事件回放沙箱 — FastAPI 入口。"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import random
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .engine import engine, new_id, normalize_events, manual_retry, do_manual_retry

app = FastAPI(title="Webhook 事件回放沙箱", version="1.0.0")


# ---------- 启动恢复：把中断的运行态安全落回暂停 ----------

@app.on_event("startup")
def recover_state():
    db.get_conn()
    db.execute("UPDATE sessions SET status='paused' WHERE status='running'")
    db.execute("UPDATE events SET status='pending' WHERE status IN ('delivering','retrying')")


# ---------- 请求模型 ----------

class SessionCreate(BaseModel):
    name: str = "replay-session"
    secret: str = Field(min_length=1)
    target_url: str = Field(min_length=1)
    speed: float = Field(default=60.0, gt=0, le=100000)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_base_delay: float = Field(default=1.0, ge=0, le=60)
    events: list[dict] = Field(min_length=1, max_length=5000)


class ReceiverConfig(BaseModel):
    mode: str = "ok"            # ok / fail / flaky / slow
    fail_code: int = 500
    fail_rate: float = 0.5
    latency_ms: int = 0
    secret: str = ""            # 配置后接收端会校验签名


# ---------- 本地接收端（内存态） ----------

receiver_configs: dict[str, dict] = defaultdict(lambda: ReceiverConfig().model_dump())
receiver_logs: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))


# ---------- 会话 API ----------

@app.post("/api/sessions", status_code=201)
def create_session(payload: SessionCreate):
    try:
        events = normalize_events(payload.events)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    sid = new_id()
    now = db.now()
    db.execute(
        """INSERT INTO sessions (id, name, secret, target_url, speed, max_retries,
           retry_base_delay, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sid, payload.name, payload.secret, payload.target_url, payload.speed,
         payload.max_retries, payload.retry_base_delay, "idle", now, now),
    )
    db.executemany(
        "INSERT INTO events (id, session_id, seq, ts, body) VALUES (?,?,?,?,?)",
        [(new_id(), sid, e["seq"], e["ts"], e["body"]) for e in events],
    )
    return get_session(sid)


@app.get("/api/sessions")
def list_sessions():
    rows = db.query("""
        SELECT s.*,
               SUM(CASE WHEN e.status='delivered' THEN 1 ELSE 0 END) AS delivered,
               SUM(CASE WHEN e.status='failed' THEN 1 ELSE 0 END) AS failed,
               COUNT(e.id) AS total
        FROM sessions s LEFT JOIN events e ON e.session_id = s.id
        GROUP BY s.id ORDER BY s.created_at DESC
    """)
    return {"sessions": rows}


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not session:
        raise HTTPException(404, "会话不存在")
    events = db.query("SELECT * FROM events WHERE session_id=? ORDER BY seq", (sid,))
    attempts = db.query(
        "SELECT * FROM attempts WHERE session_id=? ORDER BY id", (sid,))
    by_event: dict[str, list] = defaultdict(list)
    for a in attempts:
        by_event[a["event_id"]].append(a)
    for e in events:
        e["attempt_list"] = by_event.get(e["id"], [])
    total = len(events)
    delivered = sum(1 for e in events if e["status"] == "delivered")
    failed = sum(1 for e in events if e["status"] == "failed")
    return {
        "session": session,
        "events": events,
        "progress": {"total": total, "delivered": delivered, "failed": failed,
                     "pending": total - delivered - failed},
    }


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    await engine.stop(sid)
    if db.execute("DELETE FROM sessions WHERE id=?", (sid,)) == 0:
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


def _require_session(sid: str) -> dict:
    s = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")
    return s


@app.post("/api/sessions/{sid}/start")
async def start_session(sid: str):
    s = _require_session(sid)
    if s["status"] == "completed":
        raise HTTPException(409, "回放已完成，请先重置")
    status = engine.start(sid)
    return {"status": status}


@app.post("/api/sessions/{sid}/pause")
def pause_session(sid: str):
    _require_session(sid)
    if not engine.pause(sid):
        raise HTTPException(409, "会话未在运行")
    return {"status": "paused"}


@app.post("/api/sessions/{sid}/resume")
async def resume_session(sid: str):
    _require_session(sid)
    return {"status": engine.start(sid)}


@app.post("/api/sessions/{sid}/step")
async def step_session(sid: str):
    s = _require_session(sid)
    if s["status"] == "completed":
        raise HTTPException(409, "回放已完成，请先重置")
    if not engine.step(sid):
        raise HTTPException(409, "回放运行中，请先暂停再单步")
    return {"status": "stepped"}


@app.post("/api/sessions/{sid}/reset")
async def reset_session(sid: str):
    _require_session(sid)
    await engine.stop(sid)
    db.execute("DELETE FROM attempts WHERE session_id=?", (sid,))
    db.execute(
        """UPDATE events SET status='pending', attempts=0, last_response_code=NULL,
           last_error=NULL, delivered_at=NULL WHERE session_id=?""", (sid,))
    db.execute("UPDATE sessions SET status='idle', updated_at=? WHERE id=?", (db.now(), sid))
    return {"status": "idle"}


@app.post("/api/sessions/{sid}/events/{event_id}/retry")
async def retry_event(sid: str, event_id: str):
    ok, msg = manual_retry(sid, event_id)
    if not ok:
        raise HTTPException(409, msg)
    return await do_manual_retry(sid, event_id)


@app.get("/api/sessions/{sid}/export")
def export_session(sid: str, format: str = Query("json", pattern="^(json|csv)$")):
    data = get_session(sid)
    ts = int(time.time())
    if format == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return Response(
            content=content, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="replay-{sid}-{ts}.json"'})
    # CSV：每次投递尝试一行
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["event_seq", "event_id", "event_ts", "event_status", "attempt_no",
                     "kind", "started_at", "delay_before_s", "duration_ms",
                     "signature", "sig_ts", "response_code", "error"])
    for e in data["events"]:
        for a in e["attempt_list"]:
            writer.writerow([e["seq"], e["id"], e["ts"], e["status"], a["attempt_no"],
                             a["kind"], a["started_at"], a["delay_before"], a["duration_ms"],
                             a["signature"], a["sig_ts"], a["response_code"], a["error"] or ""])
    return Response(
        content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="replay-{sid}-{ts}.csv"'})


# ---------- 本地接收端 ----------

@app.api_route("/receiver/{name}", methods=["POST"])
async def receiver(name: str, request: Request):
    cfg = receiver_configs[name]
    body = await request.body()
    if cfg["latency_ms"] > 0:
        await asyncio.sleep(cfg["latency_ms"] / 1000)

    signature_valid = None
    sig_header = request.headers.get("x-webhook-signature", "")
    ts_header = request.headers.get("x-webhook-timestamp", "")
    if cfg["secret"] and sig_header.startswith("sha256="):
        expected = hmac.new(cfg["secret"].encode(), f"{ts_header}.".encode() + body,
                            hashlib.sha256).hexdigest()
        signature_valid = hmac.compare_digest(expected, sig_header[7:])

    receiver_logs[name].appendleft({
        "received_at": time.time(),
        "event_id": request.headers.get("x-webhook-event-id"),
        "attempt": request.headers.get("x-webhook-delivery-attempt"),
        "signature": sig_header,
        "sig_ts": ts_header,
        "signature_valid": signature_valid,
        "body": body.decode("utf-8", "replace")[:500],
    })

    mode = cfg["mode"]
    fail = mode == "fail" or (mode == "flaky" and random.random() < cfg["fail_rate"])
    if fail:
        return JSONResponse({"error": "receiver 按配置返回失败"}, status_code=cfg["fail_code"])
    return {"received": True, "receiver": name, "signature_valid": signature_valid}


@app.get("/api/receivers/{name}")
def get_receiver(name: str):
    return {"name": name, "config": receiver_configs[name],
            "log": list(receiver_logs[name])}


@app.put("/api/receivers/{name}/config")
def set_receiver_config(name: str, cfg: ReceiverConfig):
    if cfg.mode not in ("ok", "fail", "flaky", "slow"):
        raise HTTPException(422, "mode 必须是 ok/fail/flaky/slow")
    receiver_configs[name] = cfg.model_dump()
    return {"ok": True, "config": receiver_configs[name]}


@app.delete("/api/receivers/{name}/log")
def clear_receiver_log(name: str):
    receiver_logs[name].clear()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- 前端静态页面 ----------

import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
