"""Idempotent installer for the Claude Code hook entries.

Called from `cc-monitor` startup. Inspects ~/.claude/settings.json
and ensures PreToolUse / PostToolUse / Stop hooks pointing at our
`cc-monitor-hook` console entry exist. Does nothing (and prints a
quiet skip line) if anything referencing 'cc-monitor' (or the legacy
'cc-usagemonitor' name) already appears in those hook arrays — we
never overwrite a user's existing config, even if it points at the
legacy scripts/hook.py.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
# Match either the new package name or the pre-rename one so an upgrade
# from a development install doesn't add duplicate hook entries.
HOOK_MARKERS = ("cc-monitor", "cc-usagemonitor")


def _hook_command_path() -> str | None:
    """Locate the cc-monitor-hook binary. Falls back to the legacy
    cc-usagemonitor-hook name (in case the user is mid-upgrade), then to
    invoking the module via the current interpreter when no entry-point
    is on PATH (developer installs without a console_scripts entry)."""
    for name in ("cc-monitor-hook", "cc-usagemonitor-hook"):
        found = shutil.which(name)
        if found:
            return found
    # Fallback: same Python that's running cc-monitor.
    return f"{sys.executable} -m cc_usagemonitor.hook"


def _has_marker(hooks_array) -> bool:
    """Recursively scan a PreToolUse / PostToolUse / Stop hooks block for
    any command containing the marker string. Tolerant of malformed
    entries — anything we can't introspect counts as 'not ours'."""
    if not isinstance(hooks_array, list):
        return False
    for entry in hooks_array:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks") or []:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command") or ""
            if any(marker in cmd for marker in HOOK_MARKERS):
                return True
    return False


def _make_entries(hook_cmd: str) -> dict:
    """The three hook arrays we want. Matcher '*' on PreToolUse/Post
    catches every tool; the script filters to Skill/Agent itself so
    we don't depend on Claude Code's regex-matcher behavior."""
    return {
        "PreToolUse": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": f"{hook_cmd} pre"}],
        }],
        "PostToolUse": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": f"{hook_cmd} post"}],
        }],
        "Stop": [{
            "hooks": [{"type": "command", "command": f"{hook_cmd} stop"}],
        }],
    }


def ensure_installed() -> None:
    """Add cc-monitor hook entries to ~/.claude/settings.json if none
    exist. Never modifies an existing entry — the user owns that config.
    Prints a single status line to stderr so users who run cc-monitor
    from a terminal know what (if anything) changed."""
    hook_cmd = _hook_command_path()
    if hook_cmd is None:
        return  # nothing we can install

    try:
        if SETTINGS_PATH.exists():
            text = SETTINGS_PATH.read_text(encoding="utf-8") or "{}"
            settings = json.loads(text)
        else:
            settings = {}
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"cc-monitor: skipping hook auto-install — couldn't read "
            f"{SETTINGS_PATH}: {e}",
            file=sys.stderr,
        )
        return

    hooks = settings.setdefault("hooks", {})
    desired = _make_entries(hook_cmd)
    added: list[str] = []

    for kind, entries in desired.items():
        existing = hooks.get(kind)
        if _has_marker(existing):
            continue  # user (or a previous run) already wired ours up
        if isinstance(existing, list):
            existing.extend(entries)
        else:
            hooks[kind] = list(entries)
        added.append(kind)

    if not added:
        log.debug("hook auto-install: already configured, skipping")
        return  # everything was already in place

    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as e:
        print(
            f"cc-monitor: couldn't write {SETTINGS_PATH}: {e}",
            file=sys.stderr,
        )
        return

    log.info(
        "hook auto-install: added entries %s in %s", added, SETTINGS_PATH,
    )
    print(
        f"cc-monitor: installed Claude Code hook for {', '.join(added)} "
        f"in {SETTINGS_PATH}. Restart your Claude Code sessions to pick it up.",
        file=sys.stderr,
    )
