from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass
class UsageRecord:
    """One assistant turn's usage, parsed from a session JSONL file."""
    ts: datetime
    session_id: str
    project_slug: str
    model: str
    is_sidechain: bool
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    raw_usage: dict = field(repr=False)
    uuid: str | None = None
    parent_uuid: str | None = None


@dataclass
class HookEvent:
    """One event emitted by a hook in settings.json."""
    ts: datetime
    event: Literal["tool_start", "tool_end", "stop"]
    session_id: str
    cwd: str | None = None
    tool: str | None = None
    name: str | None = None
    span_id: str | None = None  # tool_use_id
    duration_ms: int | None = None


def _parse_ts(value: str) -> datetime:
    # Claude Code writes ISO-8601 with Z suffix
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def parse_session_line(line: str, project_slug: str) -> UsageRecord | None:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None

    if d.get("type") != "assistant":
        return None

    msg = d.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return None

    # Skip synthetic entries — Claude Code's placeholder for non-API events
    # (interrupts, stop-hook firings without a turn, API errors). They carry
    # zero-token usage but would otherwise inflate turn counts.
    if msg.get("model") == "<synthetic>":
        return None

    ts_str = d.get("timestamp")
    try:
        ts = _parse_ts(ts_str) if ts_str else datetime.now().astimezone()
    except ValueError:
        ts = datetime.now().astimezone()

    cc = usage.get("cache_creation") or {}
    return UsageRecord(
        ts=ts,
        session_id=d.get("sessionId", ""),
        project_slug=project_slug,
        model=msg.get("model", ""),
        is_sidechain=bool(d.get("isSidechain")),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_5m_tokens=cc.get("ephemeral_5m_input_tokens", 0),
        cache_write_1h_tokens=cc.get("ephemeral_1h_input_tokens", 0),
        raw_usage=usage,
        uuid=d.get("uuid"),
        parent_uuid=d.get("parentUuid"),
    )


def parse_hook_event_line(line: str) -> HookEvent | None:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None

    event = d.get("event")
    if event not in ("tool_start", "tool_end", "stop"):
        return None

    try:
        ts = _parse_ts(d["ts"])
    except (KeyError, ValueError):
        return None

    return HookEvent(
        ts=ts,
        event=event,
        session_id=d.get("session_id", ""),
        cwd=d.get("cwd"),
        tool=d.get("tool"),
        name=d.get("name"),
        span_id=d.get("span_id"),
        duration_ms=d.get("duration_ms"),
    )


def project_slug_from_path(path: Path) -> str:
    """Given .../projects/<slug>/<session>.jsonl, return <slug>."""
    return path.parent.name
