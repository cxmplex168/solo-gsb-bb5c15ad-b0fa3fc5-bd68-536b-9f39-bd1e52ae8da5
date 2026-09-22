"""回放引擎：按虚拟时间轴排序投递事件，支持暂停/继续/单步与指数退避重试。"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import httpx

from . import db

# 单次虚拟等待折算成真实时间的上限（秒），避免稀疏事件长时间空等
MAX_REAL_WAIT = 3.0
HTTP_TIMEOUT = 10.0


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def compute_signature(secret: str, ts_header: str, body: str) -> str:
    """签名算法：HMAC-SHA256(secret, "{timestamp}.{body}")，与主流 webhook 方案一致。"""
    msg = f"{ts_header}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def parse_timestamp(value) -> float:
    """接受 epoch 秒（数字或数字字符串）或 ISO 8601 字符串。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip()
        try:
            return float(v)
        except ValueError:
            pass
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"无法解析时间戳: {value!r}")


def normalize_events(raw_events: list[dict]) -> list[dict]:
    """从用户输入提取 (ts, body)，按 (ts, 原始顺序) 排序并赋予投递序号 seq。"""
    parsed = []
    for i, item in enumerate(raw_events):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 个事件不是 JSON 对象")
        item = dict(item)
        ts_raw = None
        for key in ("timestamp", "ts", "time"):
            if key in item:
                ts_raw = item.pop(key)
                ts_key = key
                break
        else:
            ts_key = None
        if ts_raw is None:
            raise ValueError(f"第 {i + 1} 个事件缺少 timestamp/ts/time 字段")
        ts = parse_timestamp(ts_raw)
        # 报文体：优先 payload / data 字段，否则取整个对象（去掉时间戳字段）
        if isinstance(item.get("payload"), (dict, list)):
            payload = item["payload"]
        elif isinstance(item.get("data"), (dict, list)):
            payload = item["data"]
        else:
            payload = item
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        parsed.append({"ts": ts, "body": body, "orig": i})
    parsed.sort(key=lambda e: (e["ts"], e["orig"]))
    for seq, e in enumerate(parsed, start=1):
        e["seq"] = seq
    return parsed


class Runner:
    """单个会话的回放协程控制器。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.pause = asyncio.Event()   # set=运行中, clear=已暂停
        self.pause.set()
        self.step_budget = 0           # 暂停状态下可单步放行的事件数
        self.cancelled = False
        self.task: asyncio.Task | None = None

    # ---- 内部工具 ----

    async def _sleep_while_running(self, seconds: float):
        """可暂停的睡眠（用于重试退避）：暂停期间倒计时冻结。"""
        remaining = seconds
        while remaining > 0:
            if self.cancelled:
                raise asyncio.CancelledError()
            if not self.pause.is_set():
                await asyncio.sleep(0.1)
                continue
            step = min(0.1, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _wait_for_turn(self, seconds: float):
        """事件间虚拟等待 + 暂停闸门。暂停时获得单步预算会立即跳过剩余等待。"""
        remaining = seconds
        while True:
            if self.cancelled:
                raise asyncio.CancelledError()
            if self.pause.is_set():
                if remaining <= 0:
                    return
                step = min(0.1, remaining)
                await asyncio.sleep(step)
                remaining -= step
            else:
                if self.step_budget > 0:
                    self.step_budget -= 1
                    return  # 单步放行：跳过剩余虚拟等待
                await asyncio.sleep(0.1)

    # ---- 投递 ----

    async def _do_attempt(self, session: dict, event: dict, attempt_no: int,
                          kind: str, delay_before: float) -> bool:
        ts_header = str(int(time.time()))
        signature = compute_signature(session["secret"], ts_header, event["body"])
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Timestamp": ts_header,
            "X-Webhook-Event-Id": event["id"],
            "X-Webhook-Delivery-Attempt": str(attempt_no),
        }
        started = time.time()
        response_code = None
        response_body = None
        error = None
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(session["target_url"], content=event["body"], headers=headers)
            response_code = resp.status_code
            response_body = resp.text[:500]
            ok = 200 <= resp.status_code < 300
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            ok = False
        duration_ms = (time.time() - started) * 1000

        db.execute(
            """INSERT INTO attempts
               (session_id, event_id, attempt_no, kind, started_at, duration_ms,
                delay_before, signature, sig_ts, response_code, response_body, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session["id"], event["id"], attempt_no, kind, started, duration_ms,
             delay_before, signature, ts_header, response_code, response_body, error),
        )
        db.execute(
            """UPDATE events SET attempts = attempts + 1, last_response_code = ?,
               last_error = ? WHERE id = ?""",
            (response_code, error, event["id"]),
        )
        return ok

    async def _deliver_event(self, session: dict, event: dict):
        """对单个事件执行首次投递 + 指数退避自动重试。"""
        max_retries = session["max_retries"]
        base = session["retry_base_delay"]
        total = max_retries + 1
        for attempt in range(1, total + 1):
            if self.cancelled:
                raise asyncio.CancelledError()
            delay = 0.0
            if attempt > 1:
                delay = base * (2 ** (attempt - 2))
                db.execute("UPDATE events SET status='retrying' WHERE id=?", (event["id"],))
                await self._sleep_while_running(delay)
            db.execute("UPDATE events SET status='delivering' WHERE id=?", (event["id"],))
            ok = await self._do_attempt(session, event, attempt, "auto", delay)
            if ok:
                db.execute(
                    "UPDATE events SET status='delivered', delivered_at=? WHERE id=?",
                    (db.now(), event["id"]),
                )
                return True
        db.execute("UPDATE events SET status='failed' WHERE id=?", (event["id"],))
        return False

    # ---- 主循环 ----

    async def run(self):
        sid = self.session_id
        try:
            session = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
            if not session:
                return
            db.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                       ("running" if self.pause.is_set() else "paused", db.now(), sid))
            events = db.query(
                "SELECT * FROM events WHERE session_id=? AND status IN ('pending','failed') ORDER BY seq",
                (sid,))
            # 上一个已处理事件的时间戳，用于计算虚拟间隔
            first_seq = events[0]["seq"] if events else 1
            prev = db.query_one(
                "SELECT ts FROM events WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
                (sid, first_seq))
            prev_ts = prev["ts"] if prev else (events[0]["ts"] if events else 0.0)

            for event in events:
                if self.cancelled:
                    raise asyncio.CancelledError()
                # 若事件在暂停期间被手动重试成功，则跳过
                fresh = db.query_one("SELECT status FROM events WHERE id=?", (event["id"],))
                if not fresh or fresh["status"] not in ("pending", "failed"):
                    prev_ts = event["ts"]
                    continue
                wait = min(max(0.0, event["ts"] - prev_ts) / session["speed"], MAX_REAL_WAIT)
                await self._wait_for_turn(wait)
                await self._deliver_event(session, event)
                prev_ts = event["ts"]

            if not self.cancelled:
                db.execute("UPDATE sessions SET status='completed', updated_at=? WHERE id=?",
                           (db.now(), sid))
        except asyncio.CancelledError:
            # 被停止/重置：把进行中的事件放回待投递
            db.execute(
                "UPDATE events SET status='pending' WHERE session_id=? AND status IN ('delivering','retrying')",
                (sid,))
            raise
        finally:
            engine.runners.pop(sid, None)


class Engine:
    def __init__(self):
        self.runners: dict[str, Runner] = {}

    def get(self, sid: str) -> Runner | None:
        return self.runners.get(sid)

    def start(self, sid: str) -> str:
        """从 idle 启动或从 paused 继续。返回启动后的状态。"""
        runner = self.runners.get(sid)
        if runner and runner.task and not runner.task.done():
            if not runner.pause.is_set():
                runner.pause.set()
                db.execute("UPDATE sessions SET status='running', updated_at=? WHERE id=?",
                           (db.now(), sid))
            return "running"
        runner = Runner(sid)
        self.runners[sid] = runner
        runner.task = asyncio.create_task(runner.run())
        return "running"

    def pause(self, sid: str) -> bool:
        runner = self.runners.get(sid)
        if not runner:
            return False
        runner.pause.clear()
        db.execute("UPDATE sessions SET status='paused', updated_at=? WHERE id=?",
                   (db.now(), sid))
        return True

    def step(self, sid: str) -> bool:
        """单步：暂停状态下精确放行一个事件；空闲时以暂停模式启动并放行一个。"""
        runner = self.runners.get(sid)
        if runner and runner.task and not runner.task.done():
            if runner.pause.is_set():
                return False  # 运行中不允许单步，请先暂停
            runner.step_budget += 1
            return True
        runner = Runner(sid)
        runner.pause.clear()
        runner.step_budget = 1
        self.runners[sid] = runner
        runner.task = asyncio.create_task(runner.run())
        db.execute("UPDATE sessions SET status='paused', updated_at=? WHERE id=?",
                   (db.now(), sid))
        return True

    async def stop(self, sid: str):
        runner = self.runners.pop(sid, None)
        if runner:
            runner.cancelled = True
            runner.pause.set()  # 解除闸门阻塞，让协程尽快退出
            if runner.task and not runner.task.done():
                runner.task.cancel()
                try:
                    await runner.task
                except (asyncio.CancelledError, Exception):
                    pass


engine = Engine()


def manual_retry(sid: str, event_id: str) -> tuple[bool, str]:
    """同步触发一次手动重试（在异步上下文中以协程运行）。"""
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
    event = db.query_one("SELECT * FROM events WHERE id=? AND session_id=?", (event_id, sid))
    if not session or not event:
        return False, "会话或事件不存在"
    if event["status"] in ("delivering", "retrying"):
        return False, "事件正在投递中"
    runner = engine.get(sid)
    if runner and runner.task and not runner.task.done() and runner.pause.is_set():
        return False, "回放运行中，请先暂停再手动重试"
    return True, ""


async def do_manual_retry(sid: str, event_id: str) -> dict:
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
    event = db.query_one("SELECT * FROM events WHERE id=? AND session_id=?", (event_id, sid))
    attempt_no = event["attempts"] + 1
    db.execute("UPDATE events SET status='delivering' WHERE id=?", (event_id,))
    runner = Runner(sid)  # 临时 runner，仅复用 _do_attempt
    ok = await runner._do_attempt(session, event, attempt_no, "manual", 0.0)
    db.execute(
        "UPDATE events SET status=?, delivered_at=CASE WHEN ? THEN ? ELSE delivered_at END WHERE id=?",
        ("delivered" if ok else "failed", ok, db.now(), event_id),
    )
    return {"ok": ok, "event_id": event_id, "attempt_no": attempt_no}
