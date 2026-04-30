"""Settings screen — global app preferences.

Pushed onto the screen stack from the main view via the ',' binding.
Sections: Appearance / Behavior / Diagnostics / Hook integration /
Maintenance / Paths. Persists to config.json on every interactive
change. Some settings are live (theme, date format), some only take
effect on next launch (API toggle, plan, default tab, filter
preferences) — the latter carry a '(restart required)' note.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, RadioButton, RadioSet, Static

from . import __version__
from .config import CONFIG_FILE, load_config, save_config
from .formatting import DATE_FORMATS, apply_config, format_time
from .install_hook import HOOK_MARKER, SETTINGS_PATH, ensure_installed
from .launchers import open_file, open_in_file_manager
from .logger import LOG_FILE, get_logger
from .paths import PROJECTS_DIR

log = get_logger(__name__)


_DEFAULT_TABS = ["sessions", "projects", "models"]


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
    .setting-note {
        height: auto;
        padding: 0 0 1 2;
        color: $text-muted;
        text-style: italic;
    }
    /* Default RadioSet has a heavy border that boxes off the panel
       into disconnected slabs — drop it so the options sit inline.
       Compact spacing + color-coded selection so the active option
       stands out against the muted ones. */
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
    /* Horizontal flavor for short option lists — Date format / Default
       tab fit on one row, no need to stack them vertically. Explicit
       height: 2 (one row of buttons + one row of bottom padding) so
       the container doesn't reserve scroll-area space below. */
    .radio-horizontal {
        layout: horizontal;
        height: 2;
        padding: 0 0 1 0;
    }
    .radio-horizontal RadioButton {
        width: auto;
        margin: 0 1 0 0;
    }
    /* Checkboxes for boolean prefs — text-based, fit naturally inline
       with the surrounding settings rows. Match RadioButton styling
       so the Behavior section reads as one consistent block. */
    Checkbox {
        background: $panel;
        border: none;
        height: 1;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $text-muted;
    }
    Checkbox:focus { border: none; }
    Checkbox.-on {
        color: $primary;
        text-style: bold;
        background: $primary 15%;
    }
    #hook-status-text, #diagnostics-text {
        padding: 1 0;
        height: auto;
    }
    .button-row { height: auto; padding: 0 0 1 0; }
    .path-row { height: auto; padding: 0 0 0 0; }
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
                # ===== Appearance =====
                yield Static("Appearance", classes="settings-heading")

                yield Static("Theme", classes="settings-row")
                current_theme = self._cfg.get("theme", "textual-dark")
                themes = self._available_themes()
                if current_theme not in themes:
                    themes = [current_theme] + themes
                with RadioSet(id="theme-radio"):
                    for name in themes:
                        yield RadioButton(name, value=(name == current_theme))

                yield Static("Date format", classes="settings-row")
                current_date_fmt = self._cfg.get("date_format", "DD-MM-YYYY")
                with RadioSet(
                    id="date-format-radio", classes="radio-horizontal"
                ):
                    for fmt in DATE_FORMATS:
                        yield RadioButton(
                            fmt, value=(fmt == current_date_fmt)
                        )

                # ===== Behavior =====
                yield Static("Behavior", classes="settings-heading")

                yield Static("Default tab on startup", classes="settings-row")
                current_default_tab = self._cfg.get("default_tab", "sessions")
                with RadioSet(
                    id="default-tab-radio", classes="radio-horizontal"
                ):
                    for t in _DEFAULT_TABS:
                        yield RadioButton(t, value=(t == current_default_tab))

                yield Checkbox(
                    "Persist filters between sessions",
                    value=self._cfg.get("persist_filters", False),
                    id="persist-filters-check",
                )
                yield Checkbox(
                    "Hide missing projects/sessions by default",
                    value=self._cfg.get("hide_missing_by_default", False),
                    id="hide-missing-check",
                )

                # ===== Diagnostics =====
                yield Static("Diagnostics", classes="settings-heading")
                yield Static(
                    self._build_diagnostics_text(),
                    id="diagnostics-text",
                )

                # ===== Hook integration =====
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

                # ===== Maintenance =====
                yield Static("Maintenance", classes="settings-heading")
                yield Static(
                    "Re-scan clears the in-memory archive and re-reads "
                    "every JSONL from the start. Useful when stats look "
                    "stale or after restoring deleted sessions.",
                    classes="setting-note",
                )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Force re-scan", id="rescan-btn",
                        variant="warning",
                    )

                # ===== Paths =====
                yield Static("Paths", classes="settings-heading")
                for label, path in self._paths():
                    with Horizontal(classes="path-row"):
                        yield Static(label, classes="path-label")
                        yield Static(str(path), classes="path-value")
                        yield Button(
                            "Open", classes="path-open-btn",
                            name=str(path),
                        )

            with Horizontal(id="settings-footer"):
                yield Static(
                    "[b]Tab[/b] / [b]shift+Tab[/b] focus   "
                    "[b]esc[/b] back",
                    id="settings-footer-right",
                )

    # ----- compose helpers -----

    def _available_themes(self) -> list[str]:
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

    def _build_diagnostics_text(self) -> str:
        agg = getattr(self.app, "aggregator", None)
        lines: list[str] = [f"[b]Version:[/b]          {__version__}"]

        # Plan: prefer authoritative API data, fall back to current
        # CLI/config setting. Tells the user where the displayed plan
        # name actually came from.
        plan_local = self._cfg.get("plan", "none")
        if agg is not None and agg.api_usage is not None and not agg.api_usage.api_unavailable:
            api_plan = agg.api_usage.plan_name or "?"
            lines.append(
                f"[b]Plan detected:[/b]   {api_plan}  "
                f"[dim](via Anthropic API)[/dim]"
            )
        else:
            lines.append(
                f"[b]Plan detected:[/b]   {plan_local}  "
                f"[dim](from config / CLI)[/dim]"
            )

        # API status — auto-detected based on OAuth credentials.
        use_api = getattr(self.app, "use_api", False)
        has_oauth = getattr(self.app, "has_oauth", False)
        if not use_api and not has_oauth:
            lines.append(
                "[b]Anthropic API:[/b]   [yellow]pay-as-you-go[/yellow]  "
                "[dim](no OAuth credentials — API-key user; "
                "/api/oauth/usage doesn't apply)[/dim]"
            )
        elif not use_api:
            lines.append(
                "[b]Anthropic API:[/b]   [yellow]disabled[/yellow]  "
                "[dim](--no-api flag — local mode)[/dim]"
            )
        elif agg is None or agg.api_usage is None:
            lines.append(
                "[b]Anthropic API:[/b]   [yellow]waiting for first "
                "fetch…[/yellow]"
            )
        else:
            usage = agg.api_usage
            if usage.api_unavailable:
                lines.append(
                    f"[b]Anthropic API:[/b]   [red]✗ unavailable[/red]  "
                    f"[dim]{usage.error or 'no details'}[/dim]"
                )
            else:
                # fetched_at is epoch seconds — convert via datetime.
                fetched = datetime.fromtimestamp(
                    usage.fetched_at, tz=timezone.utc
                )
                lines.append(
                    "[b]Anthropic API:[/b]   [green]✓ connected[/green]  "
                    f"[dim](last fetch {format_time(fetched)})[/dim]"
                )

        # Sessions tracked.
        if agg is not None:
            total = len(agg.sessions)
            active = agg.active_session_count()
            lines.append(
                f"[b]Sessions:[/b]        {total} tracked  "
                f"[dim]({active} active, <30m idle)[/dim]"
            )
            archive = len(agg._long_window)
            lines.append(
                f"[b]Long-window:[/b]     {archive} records  "
                "[dim](rolling 8d archive used for P90 limits)[/dim]"
            )
        return "\n".join(lines)

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
        elif rs_id == "default-tab-radio":
            self._cfg["default_tab"] = label
            save_config(self._cfg)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id
        if cid == "persist-filters-check":
            self._cfg["persist_filters"] = bool(event.value)
            save_config(self._cfg)
        elif cid == "hide-missing-check":
            self._cfg["hide_missing_by_default"] = bool(event.value)
            save_config(self._cfg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button
        if btn.id == "hook-reinstall-btn":
            ensure_installed()
            self._refresh_hook_status()
            self.app.bell()
            return
        if btn.id == "rescan-btn":
            self._force_rescan()
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

    # ----- persistence helpers -----

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

    def _refresh_hook_status(self) -> None:
        try:
            label = self.query_one("#hook-status-text", Static)
        except Exception:
            return
        label.update(self._build_hook_status_text())

    def _force_rescan(self) -> None:
        """Drop in-memory state on both Aggregator and Tailer so the
        next polling tick re-reads every JSONL from the start. Refreshes
        the diagnostics block to show the cleared counts."""
        agg = getattr(self.app, "aggregator", None)
        tailer = getattr(self.app, "tailer", None)
        if agg is None or tailer is None:
            log.warning("force re-scan: app is missing aggregator/tailer")
            return
        agg.reset_state()
        tailer.reset_tails()
        try:
            text = self.query_one("#diagnostics-text", Static)
            text.update(self._build_diagnostics_text())
        except Exception:
            pass
        self.app.bell()
        log.info("force re-scan triggered from Settings")
