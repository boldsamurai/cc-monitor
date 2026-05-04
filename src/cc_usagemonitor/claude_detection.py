"""Heuristic check for whether Claude Code is set up on this machine.

cc-monitor is useful in three modes:

  1. Claude Code is installed AND in active use — normal case, both the
     `claude` binary is on PATH and ~/.claude/projects/ has data.
  2. Claude Code installed but never used — binary present, no data
     yet. Onboarding territory.
  3. Archive-only — data exists (copied from another host, restored
     from backup, etc.) but no `claude` binary. Lets users analyse
     historical sessions on a machine they don't run Claude Code on.

When neither the binary nor the data is present we surface a warning
modal at startup explaining the situation; the user picks "Continue"
to proceed (e.g. they're about to install Claude Code) or "Quit" to
exit cleanly.

Detection is intentionally conservative — false positives ("Claude is
installed!") are worse than false negatives because we'd skip the
warning for a user who really does need to install Claude Code first.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from .paths import PROJECTS_DIR


@dataclass(frozen=True)
class ClaudeStatus:
    binary_in_path: bool
    has_project_data: bool

    @property
    def is_installed(self) -> bool:
        """`claude` binary on PATH is the only signal we treat as
        proof of install. Data alone (copied from another host, restored
        from backup) doesn't mean Claude Code is usable on THIS machine
        — it just means there's something to look at."""
        return self.binary_in_path

    @property
    def is_missing(self) -> bool:
        """Used to gate the startup warning. The modal still shows when
        data exists without the binary; the wording adapts to explain
        the archive-viewer use case."""
        return not self.is_installed


def _has_project_data() -> bool:
    """True if ~/.claude/projects/ exists and contains at least one
    .jsonl file. We don't recurse arbitrarily deep — Claude Code's
    layout puts JSONLs directly under each project subdirectory."""
    if not PROJECTS_DIR.is_dir():
        return False
    try:
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.iterdir():
                if f.suffix == ".jsonl" and f.is_file():
                    return True
    except OSError:
        return False
    return False


def detect_claude_install() -> ClaudeStatus:
    """One-shot probe for Claude Code presence. Cheap (single PATH
    lookup + a couple of stat calls), safe to run before the TUI
    boots."""
    return ClaudeStatus(
        binary_in_path=shutil.which("claude") is not None,
        has_project_data=_has_project_data(),
    )
