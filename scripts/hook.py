#!/usr/bin/env python3
"""cc-usagemonitor Claude Code hook.

Reads a hook payload from stdin and appends one event line to
~/.claude/usagemonitor-events.jsonl. Stdlib only — must run under any Python
present on the system, no venv.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

EVENT_LOG = Path.home() / ".claude" / "usagemonitor-events.jsonl"


def _name_for(tool: str, tool_input: dict) -> str | None:
    if tool == "Skill":
        return tool_input.get("skill")
    if tool == "Agent":
        return tool_input.get("subagent_type")
    return None


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    kind = sys.argv[1]  # 'pre' | 'post' | 'stop'

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never break the harness on bad input

    now = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    session_id = payload.get("session_id", "")

    if kind == "stop":
        event = {"ts": now, "event": "stop", "session_id": session_id}
    elif kind in ("pre", "post"):
        tool = payload.get("tool_name") or ""
        # Only Skill and Agent tools matter for usage attribution. The
        # matcher in settings.json may be widened to "*" / ".*" to dodge
        # Claude Code's regex-matcher quirks across versions; filter at
        # the script level so the event log doesn't fill up with every
        # Bash/Read/Edit invocation.
        if tool not in ("Skill", "Agent"):
            return 0
        tool_input = payload.get("tool_input") or {}
        span_id = payload.get("tool_use_id") or str(uuid.uuid4())
        event = {
            "ts": now,
            "event": "tool_start" if kind == "pre" else "tool_end",
            "session_id": session_id,
            "tool": tool,
            "name": _name_for(tool, tool_input),
            "span_id": span_id,
        }
        if kind == "pre":
            event["cwd"] = payload.get("cwd")
    else:
        return 0

    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
