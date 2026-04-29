from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


_MODEL_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model_name(model: str) -> str:
    """Strip the trailing -YYYYMMDD snapshot stamp.

    Anthropic exposes both family aliases (claude-sonnet-4-6) and dated
    snapshots (claude-sonnet-4-5-20250929). Claude Code records whichever
    one was active for that turn, so the same family ends up in multiple
    rows. Normalize to the alias for aggregation.
    """
    if not model:
        return model
    return _MODEL_DATE_SUFFIX.sub("", model)


def humanize_model_name(model: str) -> str:
    """Render 'claude-opus-4-7' as 'Opus 4.7' for display.

    Strictly a presentational helper — never use the result as a
    dict key (storage uses normalize_model_name). Preserves the
    optional context-window suffix '[1m]' so users running the
    1M variant still see it. Falls back to the raw name when the
    pattern doesn't match (older Claude 3.x ids, '(unknown)', etc.)
    so we never silently lose an unrecognized identifier.
    """
    if not model:
        return model
    suffix = ""
    base = model
    if "[" in base:
        idx = base.index("[")
        suffix = base[idx:]
        base = base[:idx]
    if not base.startswith("claude-"):
        return model
    parts = base[len("claude-"):].split("-")
    # Expected shape: <family>-<major>-<minor> where major/minor are
    # plain integers. Anything else (e.g., 'claude-3-5-sonnet') stays
    # as-is — better readable raw than mangled.
    if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
        family = "-".join(parts[:-2]).title()
        version = f"{parts[-2]}.{parts[-1]}"
        return f"{family} {version}{suffix}"
    return model


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
    # Real working directory captured by Claude Code on this turn — the
    # ground-truth project path we can't reliably reconstruct from the
    # slug alone (slug encoder collapses '/', '_', '.' all to '-').
    cwd: str | None = None


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
    except json.JSONDecodeError as e:
        from .logger import get_logger
        get_logger(__name__).debug(
            "skipping malformed session line in %s: %s", project_slug, e,
        )
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
        model=normalize_model_name(msg.get("model", "")),
        is_sidechain=bool(d.get("isSidechain")),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_5m_tokens=cc.get("ephemeral_5m_input_tokens", 0),
        cache_write_1h_tokens=cc.get("ephemeral_1h_input_tokens", 0),
        raw_usage=usage,
        uuid=d.get("uuid"),
        parent_uuid=d.get("parentUuid"),
        cwd=d.get("cwd"),
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
    """Given .../projects/<slug>/<session>.jsonl, return <slug>.

    Subagent JSONLs live one (or more) directories deeper at
    .../projects/<slug>/<session_id>/subagents/agent-<id>.jsonl —
    walk up to the parent of <slug> to find the right segment.
    """
    parts = path.parts
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return path.parent.name
