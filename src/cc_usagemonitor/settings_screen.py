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


class CircleCheckbox(Checkbox):
    """Checkbox rendered as a bullet — ● when checked, ○ when not.

    Stock Textual Checkbox keeps BUTTON_INNER constant ('X') and only
    flips its color between states, which clashes visually with the
    RadioButton bullets used elsewhere in Settings. We override the
    _button property so the actual glyph changes on toggle.
    """

    BUTTON_LEFT = ""
    BUTTON_RIGHT = ""

    @property
    def _button(self):  # type: ignore[override]
        # Local import — textual.content lives at the top level on
        # newer Textual versions; the import path may shift between
        # releases, so keep it scoped to the call.
        from textual.content import Content

        button_style = self.get_visual_style("toggle--button")
        symbol = "●" if self.value else "○"
        return Content.assemble((symbol, button_style))

from . import __version__
from .config import CONFIG_FILE, load_config, save_config
from .formatting import DATE_FORMATS, apply_config, format_time
from .install_hook import HOOK_MARKER, SETTINGS_PATH, ensure_installed
from .launchers import open_file, open_in_file_manager
from .logger import LOG_FILE, get_logger
from .paths import PROJECTS_DIR

log = get_logger(__name__)


_DEFAULT_TABS = ["sessions", "projects", "models"]
# UI refresh tick. Strings (not floats) so they map cleanly to
# RadioButton labels; parsed back to float at save time.
_REFRESH_INTERVALS = ["0.5s", "1s", "2s", "5s"]


def _parse_interval(label: str) -> float:
    return float(label.rstrip("s"))


def _format_interval(value: float) -> str:
    # 0.5 → '0.5s', 1.0 → '1s'
    if value == int(value):
        return f"{int(value)}s"
    return f"{value}s"


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
       tab fit on one row. height: 1 + overflow: hidden + scrollbar
       sizes zeroed kill the auto-reserved scrollbar gutter that was
       puffing the row to 4-5 lines. */
    .radio-horizontal {
        layout: horizontal;
        height: 1;
        padding: 0;
        margin: 0 0 1 0;
        overflow: hidden;
        scrollbar-size: 0 0;
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

                yield Static("Refresh interval (UI tick)", classes="settings-row")
                current_interval = _format_interval(
                    float(self._cfg.get("refresh_interval", 0.5))
                )
                if current_interval not in _REFRESH_INTERVALS:
                    # Honor manually-edited config values that don't
                    # match a preset (e.g., 3s) by inserting them at
                    # the front so the active option stays visible.
                    _REFRESH_INTERVALS.insert(0, current_interval)
                with RadioSet(
                    id="refresh-interval-radio",
                    classes="radio-horizontal",
                ):
                    for opt in _REFRESH_INTERVALS:
                        yield RadioButton(
                            opt, value=(opt == current_interval)
                        )

                yield CircleCheckbox(
                    "Persist filters between sessions",
                    value=self._cfg.get("persist_filters", False),
                    id="persist-filters-check",
                )
                yield CircleCheckbox(
                    "Hide missing projects/sessions by default",
                    value=self._cfg.get("hide_missing_by_default", False),
                    id="hide-missing-check",
                )
                yield CircleCheckbox(
                    "Confirm before quit",
                    value=self._cfg.get("confirm_on_quit", True),
                    id="confirm-quit-check",
                )
                yield CircleCheckbox(
                    "Confirm before destructive actions (Force re-scan)",
                    value=self._cfg.get("confirm_destructive", True),
                    id="confirm-destructive-check",
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

                # ===== Export =====
                yield Static("Export", classes="settings-heading")
                yield Static(
                    "Dump the in-memory archive to ~/.cache/cc-usagemonitor/"
                    "exports/. CSV writes three files (sessions, projects, "
                    "models); JSON writes one combined file. Numbers are "
                    "raw — no $-signs or K/M shortening — so pandas / "
                    "Excel can sort and sum directly.",
                    classes="setting-note",
                )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Export CSV", id="export-csv-btn",
                        variant="primary",
                    )
                    yield Button(
                        "Export JSON", id="export-json-btn",
                        variant="primary",
                    )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Open export folder", id="export-open-btn",
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
        elif rs_id == "refresh-interval-radio":
            try:
                self._cfg["refresh_interval"] = _parse_interval(label)
                save_config(self._cfg)
            except ValueError:
                log.warning("rejected refresh interval %r", label)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id
        if cid == "persist-filters-check":
            self._cfg["persist_filters"] = bool(event.value)
            save_config(self._cfg)
        elif cid == "hide-missing-check":
            self._cfg["hide_missing_by_default"] = bool(event.value)
            save_config(self._cfg)
        elif cid == "confirm-quit-check":
            self._cfg["confirm_on_quit"] = bool(event.value)
            save_config(self._cfg)
        elif cid == "confirm-destructive-check":
            self._cfg["confirm_destructive"] = bool(event.value)
            save_config(self._cfg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button
        if btn.id == "hook-reinstall-btn":
            ensure_installed()
            self._refresh_hook_status()
            self.app.bell()
            return
        if btn.id == "rescan-btn":
            if self._cfg.get("confirm_destructive", True):
                from .confirm_screen import ConfirmScreen
                self.app.push_screen(
                    ConfirmScreen(
                        "Force re-scan? This clears the in-memory "
                        "cache and re-reads every JSONL from the start.",
                        yes_label="Re-scan", no_label="Cancel",
                    ),
                    self._handle_rescan_confirm,
                )
            else:
                self._force_rescan()
            return
        if btn.id == "export-csv-btn":
            self._run_export("csv")
            return
        if btn.id == "export-json-btn":
            self._run_export("json")
            return
        if btn.id == "export-open-btn":
            self._open_export_folder()
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

    def _handle_rescan_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._force_rescan()

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

    def _open_export_folder(self) -> None:
        """Open ~/.cache/cc-usagemonitor/exports/ in the file manager.

        Mkdir first — the dir doesn't exist before the first export, and
        most file managers refuse to open a missing path.
        """
        from .export import export_dir
        path = export_dir()
        path.mkdir(parents=True, exist_ok=True)
        ok, msg = open_in_file_manager(str(path))
        if not ok:
            log.warning("could not open export dir: %s", msg)
            self.app.notify(
                f"Could not open {path}: {msg}", severity="error",
            )

    def _run_export(self, fmt: str) -> None:
        """Dump aggregator state to disk and toast the resulting path.

        Single-shot synchronous write — even at thousands of sessions the
        CSV / JSON output is small enough (KBs) that doing it in a worker
        is overkill. If the export ever grows, move it onto a Textual
        worker thread.
        """
        agg = getattr(self.app, "aggregator", None)
        if agg is None:
            log.warning("export: app missing aggregator")
            self.app.notify(
                "Export failed: aggregator unavailable.",
                severity="error",
            )
            return
        from .export import export_csv, export_json
        try:
            result = export_csv(agg) if fmt == "csv" else export_json(agg)
        except Exception as e:
            log.exception("export failed")
            self.app.notify(
                f"Export failed: {e}", severity="error",
            )
            return
        # Toast: file count + directory. The directory is the most
        # actionable bit (user can open it in their file manager).
        if len(result.paths) == 1:
            msg = f"Wrote {result.paths[0].name} to {result.directory}"
        else:
            msg = (
                f"Wrote {len(result.paths)} files to {result.directory}"
            )
        self.app.notify(
            msg,
            title=f"Export ({result.fmt.upper()}) complete",
            severity="information",
            timeout=8,
        )

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
        # Drop the on-disk snapshot too — otherwise a crash before the
        # next clean quit would resurrect the pre-rescan archive on
        # the next launch.
        from . import state as state_io
        state_io.discard()
        try:
            text = self.query_one("#diagnostics-text", Static)
            text.update(self._build_diagnostics_text())
        except Exception:
            pass
        self.app.bell()
        log.info("force re-scan triggered from Settings")
