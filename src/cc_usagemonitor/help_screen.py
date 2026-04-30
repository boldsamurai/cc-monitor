"""Keyboard shortcut cheatsheet — pushed from any screen via '?'.

Single source of truth for the app's bindings. Update SECTIONS below
when adding/removing keybinds anywhere; one screen to look at instead
of trawling each binding list.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static


# (heading, [(keys, description)]) pairs. Order is the on-screen order.
_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Navigation", [
        ("Tab / shift+Tab", "Move focus between widgets"),
        ("esc", "Close current screen / cancel modal"),
        ("q", "Quit cc-usagemonitor (with confirm)"),
        ("ctrl+h", "This help"),
    ]),
    ("Main view", [
        ("1, 2, 3", "Switch to Sessions / Projects / Models"),
        ("/", "Focus search input"),
        ("h", "Toggle hide missing projects/sessions"),
        ("d", "Cycle date filter (all / 24h / 7d / 30d)"),
        ("c", "Cycle cost filter ($ thresholds)"),
        ("m", "Cycle model filter (all / opus / sonnet / haiku)"),
        ("o", "Open project directory in file manager"),
        ("n", "Open new Claude Code session in project (Projects tab)"),
        ("s", "Resume last session (Projects) or selected session (Sessions)"),
        ("↵ (Enter)", "Drill into session/project detail"),
        ("Click header", "Sort by column (cycle asc / desc / reset)"),
        ("ctrl+s", "Open sort picker modal (column + direction)"),
        ("l", "Tail the log file (less +F in a new terminal)"),
        (",", "Open Settings"),
        ("r", "Force-refresh visible tab"),
    ]),
    ("Project detail", [
        ("1, 2, 3", "Switch to Sessions / Usage / Activity"),
        ("o", "Open project dir in file manager"),
        ("n", "Open new Claude Code session in project"),
        ("s", "Resume last session"),
        ("p", "Copy project path to clipboard"),
        ("i", "Copy session ID (Sessions tab only)"),
        ("↵ (Enter)", "Drill into session detail (Sessions tab)"),
    ]),
    ("Session detail", [
        ("1, 2, 3, 4", "Switch to Usage / Time / Turn / Distribution"),
        ("o", "Open project dir in file manager"),
        ("s", "Resume this session in a new terminal"),
        ("i", "Copy session ID to clipboard"),
        ("p", "Copy project path to clipboard"),
    ]),
    ("Settings", [
        ("esc / q", "Close Settings"),
        ("Click / arrows + Enter", "Toggle radios / checkboxes"),
    ]),
    ("Confirmations & modals", [
        ("y / Enter", "Confirm (Yes)"),
        ("n / esc", "Cancel (No)"),
    ]),
]


class HelpScreen(Screen):
    """Modal-style help overlay. Esc/q/? to close."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
        # Press ctrl+h again to close — toggle-style discoverability.
        Binding("ctrl+h", "app.pop_screen", "Back", show=False),
    ]

    CSS = """
    HelpScreen { background: $panel; }
    #help-scroll {
        padding: 1 2;
        background: $panel;
    }
    .help-title {
        text-style: bold underline;
        padding: 0 0 1 0;
    }
    .help-section-heading {
        text-style: bold;
        color: $primary;
        padding: 1 0 0 0;
    }
    .help-row {
        padding: 0 0 0 2;
        height: auto;
    }
    .help-key { width: 24; color: $accent; text-style: bold; }
    .help-desc { width: 1fr; }
    #screen-header {
        height: 1;
        dock: top;
        background: $panel;
    }
    #help-footer {
        height: 1;
        dock: bottom;
        background: $panel;
    }
    #help-footer-right {
        width: 1fr;
        padding: 0 1;
        text-align: right;
    }
    /* Compact top-left back button. Mouse users get a clickable hit
       target in the conventional 'back' position; keyboard users
       still see the same 'esc back' hint in the bottom footer. */
    .back-btn {
        width: auto;
        min-width: 10;
        height: 1;
        padding: 0 1;
        margin: 0 1 0 1;
        border: none;
        background: $boost;
        color: $text;
    }
    .back-btn:hover { background: $primary 30%; }
    .back-btn:focus { background: $primary 30%; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="screen-header"):
                yield Button("← Back", id="back-btn", classes="back-btn")
            with VerticalScroll(id="help-scroll"):
                yield Static("Keyboard shortcuts", classes="help-title")
                for section_title, items in _SECTIONS:
                    yield Static(
                        section_title, classes="help-section-heading"
                    )
                    for keys, desc in items:
                        with Horizontal(classes="help-row"):
                            yield Static(keys, classes="help-key")
                            yield Static(desc, classes="help-desc")
            with Horizontal(id="help-footer"):
                yield Static(
                    "[b]esc[/b] / [b]ctrl+h[/b] back",
                    id="help-footer-right",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self.app.pop_screen()
