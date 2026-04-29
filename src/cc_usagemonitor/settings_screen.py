"""Settings screen — global app preferences.

Pushed onto the screen stack from the main view via the ',' binding.
Three categories: Appearance (theme / date format / time zone), Hook
integration (status + reinstall action), and Paths (read-only with
Open buttons). Persists to config.json on every change.
"""
from __future__ import annotations

import json
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from .config import CONFIG_FILE, load_config, save_config
from .formatting import DATE_FORMATS, apply_config
from .install_hook import HOOK_MARKER, SETTINGS_PATH, ensure_installed
from .launchers import open_file, open_in_file_manager
from .logger import LOG_FILE, get_logger
from .paths import PROJECTS_DIR

log = get_logger(__name__)


# Built-in time-zone presets shown as direct radio options. Anything
# else goes through the Custom radio + IANA-name input.
_TIME_ZONE_PRESETS = ["Local", "UTC"]


class SettingsScreen(Screen):
    """Settings overlay. Esc/q to close."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]

    CSS = """
    SettingsScreen { background: $panel; }
    #settings-scroll {
        padding: 1 2;
        background: $panel;
    }
    .settings-heading {
        padding: 1 0 0 0;
        text-style: bold underline;
    }
    .settings-row { height: auto; padding: 0 0 1 0; }
    /* Default RadioSet has a heavy border that eats vertical space and
       boxes off the panel into disconnected slabs — drop it so the
       options just sit in the surrounding panel flow. */
    RadioSet {
        background: $panel;
        border: none;
        padding: 0 0 1 0;
        height: auto;
    }
    RadioSet:focus {
        border: none;
    }
    #tz-custom-input {
        width: 40;
        margin: 0 0 1 0;
    }
    #hook-status-text {
        padding: 1 0;
        height: auto;
    }
    .button-row { height: auto; padding: 0 0 1 0; }
    .path-row {
        height: auto;
        padding: 0 0 0 0;
    }
    .path-label { width: 18; }
    .path-value { width: 1fr; color: $text-muted; }
    .path-open-btn { width: 10; min-width: 10; }
    #settings-footer {
        height: 1;
        dock: bottom;
        background: $panel;
    }
    #settings-footer-right {
        width: 1fr;
        padding: 0 1;
        text-align: right;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._cfg = load_config()

    # ----- compose -----

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="settings-scroll"):
                # ----- Appearance -----
                yield Static("Appearance", classes="settings-heading")

                yield Static("Theme", classes="settings-row")
                current_theme = self._cfg.get("theme", "textual-dark")
                themes = self._available_themes()
                # Make sure the current theme is in the list — user may
                # have edited config.json by hand to a custom one.
                if current_theme not in themes:
                    themes = [current_theme] + themes
                with RadioSet(id="theme-radio"):
                    for name in themes:
                        yield RadioButton(name, value=(name == current_theme))

                yield Static("Date format", classes="settings-row")
                current_date_fmt = self._cfg.get("date_format", "DD-MM-YYYY")
                with RadioSet(id="date-format-radio"):
                    for fmt in DATE_FORMATS:
                        yield RadioButton(
                            fmt, value=(fmt == current_date_fmt)
                        )

                yield Static("Time zone", classes="settings-row")
                current_tz = self._cfg.get("time_zone", "Local")
                tz_is_custom = current_tz not in _TIME_ZONE_PRESETS
                with RadioSet(id="tz-radio"):
                    for tz in _TIME_ZONE_PRESETS:
                        yield RadioButton(tz, value=(tz == current_tz))
                    yield RadioButton("Custom", value=tz_is_custom)
                yield Input(
                    placeholder="IANA TZ name (e.g. Europe/Warsaw)",
                    value=current_tz if tz_is_custom else "",
                    id="tz-custom-input",
                )

                # ----- Hook integration -----
                yield Static("Hook integration", classes="settings-heading")
                yield Static(
                    self._build_hook_status_text(),
                    id="hook-status-text",
                )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Reinstall hook", id="hook-reinstall-btn",
                        variant="primary",
                    )

                # ----- Paths -----
                yield Static("Paths", classes="settings-heading")
                for label, path in self._paths():
                    with Horizontal(classes="path-row"):
                        yield Static(label, classes="path-label")
                        yield Static(str(path), classes="path-value")
                        # Button name carries the path so the shared
                        # on_button_pressed handler can dispatch by
                        # button identity rather than parent inspection.
                        btn = Button(
                            "Open", classes="path-open-btn",
                            name=str(path),
                        )
                        yield btn

            with Horizontal(id="settings-footer"):
                yield Static(
                    "[b]Tab[/b] / [b]shift+Tab[/b] focus   "
                    "[b]esc[/b] back",
                    id="settings-footer-right",
                )

    # ----- compose helpers -----

    def _available_themes(self) -> list[str]:
        """Best-effort list of theme names registered with the App.

        Textual's API for listing themes has changed across versions —
        try a couple of attribute names and fall back to a hardcoded
        baseline so the screen never renders empty.
        """
        for attr in ("themes", "available_themes"):
            obj = getattr(self.app, attr, None)
            if isinstance(obj, dict):
                return sorted(obj.keys())
            if isinstance(obj, list):
                return sorted(obj)
        return [
            "textual-dark", "textual-light", "nord", "gruvbox",
            "dracula", "tokyo-night", "monokai", "solarized-light",
        ]

    def _paths(self) -> list[tuple[str, Path]]:
        """The (label, path) pairs surfaced in the Paths section. Order
        matters — most-frequently-opened first."""
        return [
            ("Log file", LOG_FILE),
            ("Config", CONFIG_FILE),
            ("Claude settings", SETTINGS_PATH),
            ("Projects dir", PROJECTS_DIR),
        ]

    def _build_hook_status_text(self) -> str:
        installed, where = self._inspect_hook_status()
        if installed:
            return (
                f"[green]✓ Installed[/green] in {where}\n"
                "Pre/Post/Stop hooks pointing at our cc-usagemonitor-hook "
                "command — fires on every Skill/Agent invocation."
            )
        return (
            f"[red]✗ Not installed[/red] (would write to {where})\n"
            "Without the hook, Skill / Agent attribution falls back to "
            "best-effort heuristics from JSONL alone."
        )

    def _inspect_hook_status(self) -> tuple[bool, Path]:
        """True if any cc-usagemonitor entry exists in ~/.claude/settings.json
        hooks blocks. Returns (installed, settings_path)."""
        if not SETTINGS_PATH.exists():
            return False, SETTINGS_PATH
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, SETTINGS_PATH
        hooks = data.get("hooks") or {}
        for arr in hooks.values():
            if not isinstance(arr, list):
                continue
            for entry in arr:
                if not isinstance(entry, dict):
                    continue
                for h in entry.get("hooks") or []:
                    if HOOK_MARKER in (h.get("command") or ""):
                        return True, SETTINGS_PATH
        return False, SETTINGS_PATH

    # ----- event handlers -----

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.pressed is None:
            return
        label = str(event.pressed.label)
        rs_id = event.radio_set.id
        if rs_id == "theme-radio":
            self._set_theme(label)
        elif rs_id == "date-format-radio":
            self._set_date_format(label)
        elif rs_id == "tz-radio":
            if label == "Custom":
                # Read whatever's in the Input; empty stays as 'Custom'
                # selection but doesn't apply until the user submits.
                tz_input = self.query_one("#tz-custom-input", Input)
                value = tz_input.value.strip()
                if value:
                    self._set_time_zone(value)
            else:
                self._set_time_zone(label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "tz-custom-input":
            return
        value = event.value.strip()
        if not value:
            return
        # Validate via zoneinfo before persisting — invalid IANA names
        # would degrade to local-tz fallback at render time, but it's
        # nicer to reject here so the user knows immediately.
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(value)
        except Exception as e:
            log.warning("rejected invalid TZ %r: %s", value, e)
            self.app.bell()
            return
        # Flip the radio to Custom (in case it wasn't already) so the
        # UI state matches the saved config.
        try:
            tz_radio = self.query_one("#tz-radio", RadioSet)
            for child in tz_radio.query(RadioButton):
                if str(child.label) == "Custom":
                    child.value = True
                    break
        except Exception:
            pass
        self._set_time_zone(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button
        if btn.id == "hook-reinstall-btn":
            ensure_installed()
            self._refresh_hook_status()
            self.app.bell()  # confirm action ran
            return
        if "path-open-btn" in btn.classes and btn.name:
            path = Path(btn.name)
            if path.is_dir():
                ok, msg = open_in_file_manager(str(path))
            else:
                ok, msg = open_file(str(path))
            if not ok:
                log.warning("settings: open failed: %s", msg)
            return

    # ----- persistence -----

    def _set_theme(self, name: str) -> None:
        try:
            self.app.theme = name
        except Exception as e:
            log.warning("theme %r couldn't be applied: %s", name, e)
            return
        self._cfg["theme"] = name
        save_config(self._cfg)

    def _set_date_format(self, fmt: str) -> None:
        if fmt not in DATE_FORMATS:
            return
        apply_config(date_format=fmt)
        self._cfg["date_format"] = fmt
        save_config(self._cfg)

    def _set_time_zone(self, tz: str) -> None:
        apply_config(time_zone=tz)
        self._cfg["time_zone"] = tz
        save_config(self._cfg)

    def _refresh_hook_status(self) -> None:
        try:
            label = self.query_one("#hook-status-text", Static)
        except Exception:
            return
        label.update(self._build_hook_status_text())
