"""Generic yes/no confirmation modal.

Pushed onto the screen stack from anywhere with a callback that
receives the user's answer (True for Yes / Enter, False for No / Esc).
Used by the main view's quit action and Settings' Force re-scan
button — anything else that needs 'are you sure?' should reuse this
rather than rolling its own dialog.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmScreen(ModalScreen[bool]):
    """Modal yes/no dialog. Dismisses with True (Yes) or False (No)."""

    BINDINGS = [
        # Esc / 'n' => No, Enter / 'y' => Yes. Both pairs are common
        # TUI muscle memory; covering both makes the dialog harmless
        # to land on accidentally.
        Binding("escape", "dismiss_no", "No", show=False),
        Binding("n", "dismiss_no", "No", show=False),
        Binding("y", "dismiss_yes", "Yes", show=False),
        Binding("enter", "dismiss_yes", "Yes", show=False),
    ]

    CSS = """
    ConfirmScreen {
        align: center middle;
        background: $background 60%;
    }
    #confirm-box {
        width: auto;
        max-width: 80;
        height: auto;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }
    #confirm-message {
        height: auto;
        padding: 0 0 1 0;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: center;
    }
    #confirm-buttons Button {
        margin: 0 1;
        min-width: 12;
    }
    """

    def __init__(
        self,
        message: str,
        yes_label: str = "Yes",
        no_label: str = "No",
    ) -> None:
        super().__init__()
        self.message = message
        self.yes_label = yes_label
        self.no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    self.yes_label, id="confirm-yes", variant="primary"
                )
                yield Button(self.no_label, id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_dismiss_yes(self) -> None:
        self.dismiss(True)

    def action_dismiss_no(self) -> None:
        self.dismiss(False)
