"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class SessionCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    events_text: str = Field(min_length=1, description="JSON array / JSONL / single object")
    secret: str = Field(default="", max_length=4096)
    target_url: str = Field(min_length=1, max_length=2048)
    signature_scheme: str = Field(default="hmac-sha256")
    speed: float = Field(default=1.0, gt=0, le=1000)
    retry_max: int = Field(default=3, ge=0, le=20)
    retry_base_ms: int = Field(default=1000, ge=0, le=3_600_000)
    retry_factor: float = Field(default=2.0, ge=1.0, le=10.0)
    on_failure: str = Field(default="continue")
    header_prefix: str = Field(default="X-Webhook", max_length=64)
    autostart: bool = True

    @field_validator("signature_scheme")
    @classmethod
    def _scheme(cls, v: str) -> str:
        if v not in {"hmac-sha256", "hmac-sha1", "hmac-sha512", "none"}:
            raise ValueError("unsupported signature scheme")
        return v

    @field_validator("on_failure")
    @classmethod
    def _failure(cls, v: str) -> str:
        if v not in {"continue", "abort"}:
            raise ValueError("on_failure must be continue or abort")
        return v

    @field_validator("target_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must start with http:// or https://")
        return v


class SessionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    secret: Optional[str] = Field(default=None, max_length=4096)
    target_url: Optional[str] = None
    speed: Optional[float] = Field(default=None, gt=0, le=1000)
    retry_max: Optional[int] = Field(default=None, ge=0, le=20)
    retry_base_ms: Optional[int] = Field(default=None, ge=0, le=3_600_000)
    retry_factor: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    on_failure: Optional[str] = None
    header_prefix: Optional[str] = Field(default=None, max_length=64)


class CatchBoxCreate(BaseModel):
    name: str = Field(default="receiver", max_length=120)
    fail_status: int = Field(default=0, ge=0, le=599)
    fail_every: int = Field(default=0, ge=0, le=10_000)


class CatchBoxUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    fail_status: Optional[int] = Field(default=None, ge=0, le=599)
    fail_every: Optional[int] = Field(default=None, ge=0, le=10_000)


class EventOut(BaseModel):
    idx: int
    event_id: Any
    type: Any
    timestamp: str
    payload: Any


class DeliveryOut(BaseModel):
    event_idx: int
    event_id: Any
    type: Any
    event_ts: Optional[str]
    virtual_ts: Optional[str]
    target_url: str
    signature: Optional[str]
    status_code: Optional[int]
    state: str
    attempt_count: int
    max_attempts: int
    error: Optional[str]
    delivered_at: Optional[float]
    attempts: List[Any]
