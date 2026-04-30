"""Blocking modal shown during the initial JSONL replay.

Pushed at on_mount and popped from _refresh_view once
``Tailer.initial_scan_done`` flips. Sits over the whole UI so the
user can see the layout but not interact until data is loaded —
clearer than a frozen-looking page or a banner that flashes by.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import LoadingIndicator, Static


class LoadingScreen(ModalScreen[None]):
    """Modal shown during the initial archive replay.

    Intentionally has NO bindings — user can't dismiss manually.
    The app pops this screen from _refresh_view once the tailer's
    first sweep over all JSONLs completes.
    """

    # Class-level flag toggled once the modal is dismissed so popping
    # twice (in case _refresh_view runs another tick before the pop
    # propagates) doesn't tear down something else off the screen
    # stack.
    BINDINGS = []  # type: ignore[var-annotated]

    CSS = """
    LoadingScreen {
        align: center middle;
        /* Solid backdrop so the modal is visible on cold start before
           any of the underlying screen has had a chance to render
           (event loop is busy with the first JSONL replay sweep). */
        background: $surface;
    }
    #loading-box {
        width: 60;
        height: auto;
        background: $panel;
        border: round $primary;
        padding: 2 3;
    }
    #loading-title {
        text-style: bold;
        text-align: center;
        padding: 0 0 1 0;
    }
    #loading-sub {
        text-align: center;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    LoadingIndicator {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="loading-box"):
            yield Static(
                "⏳  Loading historical sessions…",
                id="loading-title",
            )
            yield LoadingIndicator()
            yield Static(
                "Replaying JSONLs from ~/.claude/projects/. "
                "This is a one-time scan on each launch.",
                id="loading-sub",
            )
