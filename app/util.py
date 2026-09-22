"""Event parsing and timestamp helpers.

Accepted input forms
--------------------
* a JSON array of objects:  ``[{"timestamp": ..., ...}, ...]``
* JSONL: one JSON object per line
* a single JSON object

Each event must contain a timestamp under one of the common keys
(``timestamp`` / ``time`` / ``ts`` / ``@timestamp`` / ``date``).  Numeric
values are interpreted as unix epoch seconds (or milliseconds when > 1e11).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from dateutil import parser as dateparser

MAX_EVENTS = 1000

_TS_KEYS = ("timestamp", "time", "ts", "@timestamp", "date")
_ID_KEYS = ("id", "event_id", "eventId", "event-id")
_TYPE_KEYS = ("type", "event", "name", "event_type", "eventType")


class EventParseError(ValueError):
    pass


def parse_events_text(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if not text:
        raise EventParseError("事件内容为空")

    raw_events: List[Any]
    try:
        loaded = json.loads(text)
        raw_events = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        # Fall back to JSON-Lines parsing so the error message can point at
        # the offending line instead of just saying "invalid JSON".
        raw_events = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                raw_events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EventParseError(f"第 {line_no} 行不是合法 JSON: {exc.msg}") from exc

    if not raw_events:
        raise EventParseError("没有可回放的事件")
    if len(raw_events) > MAX_EVENTS:
        raise EventParseError(f"事件数量超过上限 {MAX_EVENTS}")

    normalized: List[Dict[str, Any]] = []
    for i, ev in enumerate(raw_events):
        if not isinstance(ev, dict):
            raise EventParseError(f"第 {i + 1} 个事件不是 JSON 对象")
        ts_raw = _first_key(ev, _TS_KEYS)
        if ts_raw is None:
            raise EventParseError(f"第 {i + 1} 个事件缺少时间戳字段 (timestamp/ts/time/date)")
        try:
            dt = parse_timestamp(ts_raw)
        except Exception as exc:  # noqa: BLE001 - surfaced to user as-is
            raise EventParseError(f"第 {i + 1} 个事件时间戳无法解析: {ts_raw!r} ({exc})") from exc

        event_id = _first_key(ev, _ID_KEYS)
        event_type = _first_key(ev, _TYPE_KEYS)
        normalized.append(
            {
                "event_id": event_id if event_id is not None else i,
                "type": event_type if event_type is not None else "event",
                "timestamp": dt,
                "payload": ev,
            }
        )

    normalized.sort(key=lambda e: e["timestamp"])
    return normalized


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if abs(ts) > 1e11:  # milliseconds (or finer)
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        return dateparser.parse(value).astimezone(timezone.utc)
    raise ValueError("时间戳必须是数字或字符串")


def _first_key(obj: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    # case-insensitive fallback
    lower = {k.lower(): v for k, v in obj.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
