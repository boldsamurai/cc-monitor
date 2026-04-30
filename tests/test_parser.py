"""Tests for parser pure helpers and JSONL line parsing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_usagemonitor.parser import (
    humanize_model_name,
    normalize_model_name,
    parse_hook_event_line,
    parse_session_line,
    project_slug_from_path,
)


# ----- normalize_model_name -----

@pytest.mark.parametrize("raw,want", [
    ("claude-opus-4-7", "claude-opus-4-7"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("", ""),
    ("opus-4-7", "opus-4-7"),  # No date suffix → unchanged.
])
def test_normalize_model_name(raw, want):
    assert normalize_model_name(raw) == want


# ----- humanize_model_name -----

@pytest.mark.parametrize("raw,want", [
    ("claude-opus-4-7", "Opus 4.7"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-haiku-4-5", "Haiku 4.5"),
    ("claude-opus-4-7[1m]", "Opus 4.7[1m]"),
    # Older 3.x ids don't fit the family-major-minor shape — pass through.
    ("claude-3-5-sonnet", "claude-3-5-sonnet"),
    ("(unknown)", "(unknown)"),
    ("", ""),
])
def test_humanize_model_name(raw, want):
    assert humanize_model_name(raw) == want


# ----- parse_session_line -----

def _assistant_line(**overrides) -> str:
    """Build a minimal valid assistant JSONL line for the parser."""
    payload = {
        "type": "assistant",
        "sessionId": "sess-1",
        "timestamp": "2026-04-30T12:00:00.000Z",
        "uuid": "u-1",
        "parentUuid": None,
        "isSidechain": False,
        "cwd": "/tmp/proj",
        "message": {
            "model": "claude-opus-4-7-20260301",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 5,
                },
            },
        },
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_session_line_happy_path():
    rec = parse_session_line(_assistant_line(), project_slug="-tmp-proj")
    assert rec is not None
    assert rec.session_id == "sess-1"
    assert rec.project_slug == "-tmp-proj"
    # Date suffix gets stripped on the way in.
    assert rec.model == "claude-opus-4-7"
    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.cache_read_tokens == 200
    assert rec.cache_write_5m_tokens == 10
    assert rec.cache_write_1h_tokens == 5
    assert rec.cwd == "/tmp/proj"


def test_parse_session_line_skips_non_assistant():
    line = json.dumps({"type": "user", "message": {}})
    assert parse_session_line(line, "-x") is None


def test_parse_session_line_skips_synthetic():
    line = _assistant_line(message={
        "model": "<synthetic>", "usage": {"input_tokens": 0},
    })
    assert parse_session_line(line, "-x") is None


def test_parse_session_line_skips_no_usage():
    line = json.dumps({
        "type": "assistant",
        "message": {"model": "claude-opus-4-7"},
    })
    assert parse_session_line(line, "-x") is None


def test_parse_session_line_handles_malformed_json():
    assert parse_session_line("{not json", "-x") is None
    assert parse_session_line("", "-x") is None
    assert parse_session_line("   ", "-x") is None


def test_parse_session_line_lump_cache_creation_unsupported():
    # Lump-sum cache_creation_input_tokens is *not* expanded by parser
    # — only PricingTable.cost() understands the legacy fallback. Make
    # sure the parser doesn't accidentally pick it up.
    line = _assistant_line(message={
        "model": "claude-opus-4-7",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 999,
            # Empty cache_creation dict → ephemeral_5m/1h are 0.
        },
    })
    rec = parse_session_line(line, "-x")
    assert rec is not None
    assert rec.cache_write_5m_tokens == 0
    assert rec.cache_write_1h_tokens == 0


# ----- parse_hook_event_line -----

def test_parse_hook_event_tool_start():
    line = json.dumps({
        "event": "tool_start",
        "ts": "2026-04-30T12:00:00.000Z",
        "session_id": "s-1",
        "tool": "Bash",
        "span_id": "tool-use-1",
        "cwd": "/tmp/p",
    })
    ev = parse_hook_event_line(line)
    assert ev is not None
    assert ev.event == "tool_start"
    assert ev.session_id == "s-1"
    assert ev.tool == "Bash"
    assert ev.span_id == "tool-use-1"


def test_parse_hook_event_unknown_event():
    line = json.dumps({"event": "garbage", "ts": "2026-04-30T12:00:00Z"})
    assert parse_hook_event_line(line) is None


def test_parse_hook_event_missing_ts():
    line = json.dumps({"event": "tool_start", "session_id": "s-1"})
    assert parse_hook_event_line(line) is None


# ----- project_slug_from_path -----

def test_project_slug_from_path_normal():
    p = Path("/home/u/.claude/projects/-tmp-proj/abc.jsonl")
    assert project_slug_from_path(p) == "-tmp-proj"


def test_project_slug_from_path_subagent():
    # Subagent JSONLs nest under <slug>/<sess>/subagents/agent-N.jsonl.
    p = Path(
        "/home/u/.claude/projects/-tmp-proj/sess/subagents/agent-1.jsonl"
    )
    assert project_slug_from_path(p) == "-tmp-proj"


def test_project_slug_from_path_no_projects_segment():
    # Falls back to the parent dir name when the conventional layout
    # doesn't match.
    p = Path("/some/unrelated/dir/file.jsonl")
    assert project_slug_from_path(p) == "dir"
