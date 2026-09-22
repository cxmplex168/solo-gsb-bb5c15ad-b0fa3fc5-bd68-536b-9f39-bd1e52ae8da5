"""Virtual-timeline replay engine.

One asyncio worker task runs per active session.  The worker walks the
sorted event list and, between events, sleeps for the *scaled* interval
found in the original timestamps.  Pause / resume / step / stop arrive
through a small per-session command channel and are honoured even while
the worker is sleeping or waiting between retries.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
import httpx

from .db import DB_PATH, dumps
from .util import iso

# Commands pushed onto a session's command queue.
CMD_STEP = "step"
CMD_RESUME = "resume"
CMD_STOP = "stop"

SCHEME_HASH = {
    "hmac-sha256": hashlib.sha256,
    "hmac-sha1": hashlib.sha1,
    "hmac-sha512": hashlib.sha512,
}


class EngineError(Exception):
    """Raised for invalid control operations; mapped to HTTP 4xx by routes."""


@dataclass
class Runtime:
    session_id: str
    cmd_queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    cmd_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False
    task: Optional[asyncio.Task] = None
    speed: float = 1.0


class ReplayEngine:
    def __init__(self) -> None:
        self._runtimes: Dict[str, Runtime] = {}
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        # Sessions left mid-flight by a process restart can never resume;
        # mark them so the UI does not pretend they are still running.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sessions SET status='stopped', error='服务重启导致回放中断' "
                "WHERE status IN ('running','paused','pausing')"
            )
            await db.commit()

    async def shutdown(self) -> None:
        for rt in list(self._runtimes.values()):
            rt.stop_event.set()
            rt.cmd_event.set()
        if self._runtimes:
            await asyncio.gather(
                *(rt.task for rt in self._runtimes.values() if rt.task),
                return_exceptions=True,
            )
        if self._client is not None:
            await self._client.aclose()

    # ------------------------------------------------------------------ helpers

    def get_runtime(self, session_id: str) -> Optional[Runtime]:
        return self._runtimes.get(session_id)

    async def _push_command(self, rt: Runtime, cmd: str) -> None:
        await rt.cmd_queue.put(cmd)
        rt.cmd_event.set()

    async def _next_command(self, rt: Runtime) -> str:
        """Block until a command is available (stop short-circuits)."""
        while not rt.stop_event.is_set():
            if not rt.cmd_queue.empty():
                return rt.cmd_queue.get_nowait()
            rt.cmd_event.clear()
            if not rt.cmd_queue.empty():
                return rt.cmd_queue.get_nowait()
            try:
                await asyncio.wait_for(rt.cmd_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        return CMD_STOP

    async def _drain_command(self, rt: Runtime) -> Optional[str]:
        """Pop one command without blocking."""
        return rt.cmd_queue.get_nowait() if not rt.cmd_queue.empty() else None

    # ------------------------------------------------------------------ control

    async def start(self, session_id: str) -> None:
        async with self._lock:
            if session_id in self._runtimes:
                raise EngineError("该会话已在运行中")
            rt = Runtime(session_id=session_id)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                row = await db.execute(
                    "SELECT speed, status FROM sessions WHERE id=?", (session_id,)
                )
                row = await row.fetchone()
                if row is None:
                    raise EngineError("会话不存在")
                rt.speed = row["speed"]
                if row["status"] not in ("created", "stopped", "failed", "succeeded"):
                    raise EngineError(f"当前状态 {row['status']} 无法启动")
            self._runtimes[session_id] = rt
            rt.task = asyncio.create_task(self._run(rt))

    async def pause(self, session_id: str) -> None:
        rt = self._runtimes.get(session_id)
        if rt is None:
            raise EngineError("会话未在运行")
        if rt.paused:
            return
        rt.paused = True  # sleeping loops observe this on their next poll
        rt.cmd_event.set()  # wake _next_command if idle at first event
        await self._set_status(session_id, "paused")

    async def resume(self, session_id: str) -> None:
        rt = self._runtimes.get(session_id)
        if rt is None:
            raise EngineError("会话未在运行")
        if not rt.paused:
            return
        rt.paused = False
        await self._push_command(rt, CMD_RESUME)
        await self._set_status(session_id, "running")

    async def step(self, session_id: str) -> None:
        """Deliver exactly one more event, then stay paused."""
        rt = self._runtimes.get(session_id)
        if rt is None:
            raise EngineError("会话未在运行")
        rt.paused = True  # works both from paused and while playing: after the
        await self._push_command(rt, CMD_STEP)  # single step the loop parks
        await self._set_status(session_id, "paused")

    async def stop(self, session_id: str) -> None:
        rt = self._runtimes.get(session_id)
        if rt is None:
            # already finished — nothing to do
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE sessions SET status='stopped', finished_at=? "
                    "WHERE id=? AND status NOT IN ('stopped','succeeded','failed')",
                    (time.time(), session_id),
                )
                await db.commit()
            return
        rt.stop_event.set()
        await self._push_command(rt, CMD_STOP)

    async def set_speed(self, session_id: str, speed: float) -> None:
        rt = self._runtimes.get(session_id)
        if rt is not None:
            rt.speed = speed

    async def _release(self, session_id: str) -> None:
        async with self._lock:
            self._runtimes.pop(session_id, None)

    @staticmethod
    async def _set_status(session_id: str, status: str, **extra: Any) -> None:
        sets = ["status=?"]
        params: List[Any] = [status]
        for key, val in extra.items():
            sets.append(f"{key}=?")
            params.append(val)
        params.append(session_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)
            await db.commit()

    # ------------------------------------------------------------------ worker

    async def _run(self, rt: Runtime) -> None:
        session_id = rt.session_id
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            cfg = await self._load_config(db, session_id)
            events = await self._load_events(db, session_id)
            await db.execute(
                "UPDATE sessions SET status='running', started_at=COALESCE(started_at, ?), "
                "finished_at=NULL, error=NULL WHERE id=?",
                (time.time(), session_id),
            )
            await db.commit()

            origin: Optional[datetime] = (
                datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
                if events
                else None
            )
            origin_wall = time.time()
            cursor = cfg["cursor"] or 0

            if cursor >= len(events):
                await self._finish(db, session_id, "succeeded")
                return

            aborted = False
            while cursor < len(events):
                # Single parking point: before every event, wait while paused
                # for a STEP (deliver one) or RESUME (play until the end).
                while rt.paused and not rt.stop_event.is_set():
                    cmd = await self._await_command(rt)
                    if cmd == CMD_STOP:
                        await self._finish(db, session_id, "stopped")
                        return
                    if cmd == CMD_RESUME:
                        rt.paused = False
                    # STEP: leave paused=True and deliver exactly one event

                if rt.stop_event.is_set():
                    await self._finish(db, session_id, "stopped")
                    return

                # Gap between previous and current event on the virtual axis.
                # A resumed session delivers its next event immediately.
                if cursor > 0:
                    prev_ts = _parse_dt(events[cursor - 1]["timestamp"])
                    cur_ts = _parse_dt(events[cursor]["timestamp"])
                    gap = (cur_ts - prev_ts).total_seconds()
                    if gap > 0:
                        preempt = await self._sleep(rt, gap / max(rt.speed, 1e-9))
                        if preempt == CMD_STOP:
                            await self._finish(db, session_id, "stopped")
                            return

                await db.execute(
                    "UPDATE sessions SET status=? WHERE id=?",
                    ("paused" if rt.paused else "running", session_id),
                )
                await db.commit()

                ok = await self._deliver(db, rt, cfg, events[cursor], origin, origin_wall)
                cursor += 1
                await db.execute(
                    "UPDATE sessions SET cursor=? WHERE id=?", (cursor, session_id)
                )
                await db.commit()

                if not ok and cfg["on_failure"] == "abort":
                    aborted = True
                    break

            if aborted:
                await self._finish(db, session_id, "failed", error="事件投递失败且策略为 abort，回放中止")
            elif rt.stop_event.is_set():
                await self._finish(db, session_id, "stopped")
            else:
                await self._finish(db, session_id, "succeeded")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - persist unexpected failure
            await self._finish(db, session_id, "failed", error=f"引擎异常: {exc}")
        finally:
            await db.close()
            await self._release(session_id)

    async def _await_command(self, rt: Runtime) -> str:
        """Block while paused until step / resume / stop arrives."""
        while True:
            cmd = await self._next_command(rt)
            if cmd in (CMD_STEP, CMD_RESUME, CMD_STOP):
                return cmd

    async def _sleep(self, rt: Runtime, seconds: float) -> Optional[str]:
        """Scaled, interruptible sleep.

        Returns ``CMD_STOP`` when stopped, ``CMD_STEP`` when a single step
        preempts the gap.  RESUME only clears the paused flag; the remaining
        gap keeps playing.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if rt.stop_event.is_set():
                return CMD_STOP
            cmd = await self._drain_command(rt)
            if cmd == CMD_STOP:
                return CMD_STOP
            if cmd == CMD_STEP:
                return CMD_STEP
            if cmd == CMD_RESUME:
                rt.paused = False
            if rt.paused:
                # park here, but keep the deadline so resume does not replay
                # the portion of the gap that already elapsed
                parked = await self._next_command(rt)
                if parked == CMD_STOP:
                    return CMD_STOP
                if parked == CMD_STEP:
                    return CMD_STEP
                if parked == CMD_RESUME:
                    rt.paused = False
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return None

    # ------------------------------------------------------------------ delivery

    async def _deliver(
        self,
        db: aiosqlite.Connection,
        rt: Runtime,
        cfg: aiosqlite.Row,
        event: aiosqlite.Row,
        origin: Optional[datetime],
        origin_wall: float,
    ) -> bool:
        payload_bytes = event["payload"].encode("utf-8")
        sign_ts = str(int(time.time()))
        signature = _sign(cfg, payload_bytes, sign_ts)
        prefix = cfg["header_prefix"] or "X-Webhook"
        headers = {
            "Content-Type": "application/json",
            f"{prefix}-Event": str(event["type"]),
            f"{prefix}-Event-Id": str(event["event_id"]),
            f"{prefix}-Timestamp": sign_ts,
            f"{prefix}-Delivery": uuid.uuid4().hex,
        }
        if signature is not None:
            headers[f"{prefix}-Signature"] = signature

        virtual_ts = None
        if origin is not None:
            virtual_ts = iso(
                datetime.fromtimestamp(origin_wall, tz=timezone.utc)
                + (_parse_dt(event["timestamp"]) - origin)
            )

        max_attempts = cfg["retry_max"] + 1
        cursor = await db.execute(
            "INSERT INTO deliveries (session_id, event_idx, event_id, type, event_ts, "
            "virtual_ts, target_url, signature, state, attempt_count, max_attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cfg["id"],
                event["idx"],
                str(event["event_id"]),
                str(event["type"]),
                event["timestamp"],
                virtual_ts,
                cfg["target_url"],
                signature,
                "delivering",
                0,
                max_attempts,
            ),
        )
        await db.commit()
        delivery_id = cursor.lastrowid

        attempts: List[Dict[str, Any]] = []
        last_error = ""
        success = False

        for attempt_no in range(1, max_attempts + 1):
            if rt.stop_event.is_set():
                last_error = "已停止"
                break

            attempt: Dict[str, Any] = {
                "attempt": attempt_no,
                "started_at": time.time(),
                "scheduled_in_ms": 0,
            }
            if attempt_no > 1:
                backoff_ms = int(
                    cfg["retry_base_ms"]
                    * (cfg["retry_factor"] ** (attempt_no - 2))
                )
                attempt["scheduled_in_ms"] = backoff_ms
                preempt = await self._interruptible_sleep(rt, backoff_ms / 1000.0)
                if preempt == CMD_STOP or rt.stop_event.is_set():
                    last_error = "重试等待期间被停止"
                    attempt["state"] = "skipped"
                    attempts.append(attempt)
                    break

            try:
                resp = await self._client.post(  # type: ignore[union-attr]
                    cfg["target_url"], content=payload_bytes, headers=headers
                )
                attempt["status_code"] = resp.status_code
                attempt["response_excerpt"] = resp.text[:500]
                attempt["finished_at"] = time.time()
                if 200 <= resp.status_code < 300:
                    attempt["state"] = "success"
                    attempts.append(attempt)
                    success = True
                    break
                attempt["state"] = "http_error"
                last_error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                attempt["state"] = "network_error"
                attempt["error"] = str(exc)[:500]
                attempt["finished_at"] = time.time()
                last_error = f"网络错误: {exc}"

            attempts.append(attempt)
            await db.execute(
                "UPDATE deliveries SET state='retrying', attempt_count=?, attempts_json=? "
                "WHERE id=?",
                (attempt_no, dumps(attempts), delivery_id),
            )
            await db.commit()

        state = "success" if success else ("stopped" if last_error == "已停止" else "failed")
        status_code = attempts[-1].get("status_code") if attempts else None
        await db.execute(
            "UPDATE deliveries SET state=?, status_code=?, attempt_count=?, attempts_json=?, "
            "error=?, delivered_at=? WHERE id=?",
            (
                state,
                status_code,
                len(attempts),
                dumps(attempts),
                None if success else last_error,
                time.time() if success else None,
                delivery_id,
            ),
        )
        await db.commit()
        return success

    async def _interruptible_sleep(self, rt: Runtime, seconds: float) -> Optional[str]:
        """Backoff sleep that reacts to pause / resume / stop.

        A STEP arriving during a retry wait has no extra event to send, so it
        behaves like pause: the deadline is preserved and RESUME continues.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if rt.stop_event.is_set():
                return CMD_STOP
            cmd = await self._drain_command(rt)
            if cmd == CMD_STOP:
                return CMD_STOP
            if cmd == CMD_RESUME:
                rt.paused = False
            elif cmd == CMD_STEP:
                rt.paused = True
            if rt.paused:
                parked = await self._next_command(rt)
                if parked == CMD_STOP:
                    return CMD_STOP
                if parked == CMD_RESUME:
                    rt.paused = False
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return None

    # ------------------------------------------------------------------ loading

    async def _load_config(self, db: aiosqlite.Connection, session_id: str) -> aiosqlite.Row:
        cur = await db.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        row = await cur.fetchone()
        if row is None:
            raise EngineError("会话不存在")
        return row

    async def _load_events(self, db: aiosqlite.Connection, session_id: str) -> List[aiosqlite.Row]:
        cur = await db.execute(
            "SELECT idx, event_id, type, timestamp, payload FROM events "
            "WHERE session_id=? ORDER BY idx",
            (session_id,),
        )
        return list(await cur.fetchall())

    async def _finish(
        self,
        db: aiosqlite.Connection,
        session_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        await db.execute(
            "UPDATE sessions SET status=?, finished_at=?, error=? WHERE id=?",
            (status, time.time(), error, session_id),
        )
        await db.commit()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sign(cfg: aiosqlite.Row, payload: bytes, timestamp: str) -> Optional[str]:
    scheme = cfg["signature_scheme"]
    if scheme == "none":
        return None
    algo = SCHEME_HASH[scheme]
    mac = hmac.new(cfg["secret"].encode("utf-8"), digestmod=algo)
    mac.update(timestamp.encode("ascii"))
    mac.update(b".")
    mac.update(payload)
    digest = base64.b64encode(mac.digest()).decode("ascii")
    return f"{scheme}={digest}"
