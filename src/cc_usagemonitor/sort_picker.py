"""Modal column picker for keyboard-driven sort.

Pushed from the main view via the 'S' binding. Lets the user pick a
column + direction with arrow keys + Enter instead of clicking the
header. Returns one of:

  - ``(col_key_value, reverse)`` tuple on Apply
  - ``"reset"`` string when the user picks the Reset option
  - ``None`` when cancelled (Esc / Cancel button)

The caller (UsageMonitorApp.action_open_sort_picker) wires the result
into _apply_sort_for / _reset_sort_for.
"""
from __future__ import annotations

from typing import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static


class SortPickerScreen(ModalScreen):
    """Modal sort picker — column list + direction toggle."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel", show=False),
        # 'r' shortcut wires the Reset button to the keyboard so the
        # user can drop the active sort without tabbing across the
        # buttons row.
        Binding("r", "reset", "Reset"),
    ]

    CSS = """
    SortPickerScreen {
        align: center middle;
        background: $background 60%;
    }
    #sort-box {
        width: 60;
        height: auto;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }
    .sort-title {
        text-style: bold underline;
        padding: 0 0 1 0;
    }
    .sort-section {
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
    }
    /* Inherit the compact RadioSet treatment from settings_screen so
       the modal feels consistent with the rest of the app. */
    RadioSet {
        background: $panel;
        border: none;
        padding: 0 0 1 0;
        height: auto;
    }
    RadioSet:focus { border: none; }
    RadioButton {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: $text-muted;
    }
    RadioButton.-on {
        color: $primary;
        text-style: bold;
        background: $primary 15%;
    }
    /* Direction is binary — render it inline so it doesn't eat
       vertical space the way the column list will. */
    #sort-dir-radio {
        layout: horizontal;
        height: 1;
        padding: 0;
        margin: 0 0 1 0;
        overflow: hidden;
        scrollbar-size: 0 0;
    }
    #sort-dir-radio RadioButton {
        width: auto;
        margin: 0 1 0 0;
    }
    #sort-buttons {
        height: auto;
        padding: 1 0 0 0;
        align-horizontal: center;
    }
    #sort-buttons Button {
        margin: 0 1;
        min-width: 12;
    }
    /* Modal footer with key hints — same style as the rest of the
       app's footers (see tui.py / project_detail.py). */
    .sort-footer {
        padding: 1 0 0 0;
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        columns: Sequence[tuple[str, str]],
        current_col: str | None = None,
        current_reverse: bool = False,
    ) -> None:
        super().__init__()
        self._columns = list(columns)
        self._current_col = current_col
        self._current_reverse = current_reverse

    def compose(self) -> ComposeResult:
        with Vertical(id="sort-box"):
            yield Static("Sort by column", classes="sort-title")
            with RadioSet(id="sort-col-radio"):
                for label, key in self._columns:
                    yield RadioButton(
                        label, value=(key == self._current_col),
                        name=key,
                    )
            yield Static("Direction", classes="sort-section")
            with RadioSet(id="sort-dir-radio"):
                yield RadioButton(
                    "Ascending",
                    value=not self._current_reverse,
                    name="asc",
                )
                yield RadioButton(
                    "Descending",
                    value=self._current_reverse,
                    name="desc",
                )
            with Horizontal(id="sort-buttons"):
                yield Button(
                    "Apply", id="sort-apply", variant="primary",
                )
                yield Button(
                    "Reset", id="sort-reset", variant="warning",
                )
                yield Button("Cancel", id="sort-cancel")
            yield Static(
                "[b]Tab[/b] / [b]shift+Tab[/b] focus   "
                "[b]↵[/b] activate   [b]r[/b] reset   "
                "[b]esc[/b] cancel",
                classes="sort-footer",
            )

    # ----- actions -----

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_reset(self) -> None:
        self.dismiss("reset")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "sort-cancel":
            self.dismiss(None)
        elif bid == "sort-reset":
            self.dismiss("reset")
        elif bid == "sort-apply":
            self._apply()

    def _apply(self) -> None:
        col_radio = self.query_one("#sort-col-radio", RadioSet)
        dir_radio = self.query_one("#sort-dir-radio", RadioSet)
        col_key = None
        for child in col_radio.query(RadioButton):
            if child.value:
                col_key = child.name
                break
        if col_key is None:
            # No column selected somehow — treat as cancel.
            self.dismiss(None)
            return
        reverse = False
        for child in dir_radio.query(RadioButton):
            if child.value and child.name == "desc":
                reverse = True
                break
        self.dismiss((col_key, reverse))
