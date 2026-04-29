from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Header,
    Input,
    Static,
    Tab,
    Tabs,
)
from textual_plotext import PlotextPlot

from .aggregator import Aggregator, BlockInfo, TokenSums
from .parser import humanize_model_name
from .anthropic_usage import UsageData, get_usage
from .config import load_config, save_config
from .formatting import (
    apply_config as _apply_format_config,
    format_datetime as _fmt_datetime,
    format_time as _fmt_ts,
)
from .launchers import open_file, open_in_file_manager, open_terminal_with
from .logger import LOG_DIR, LOG_FILE, get_logger
from .pricing import PricingTable

log = get_logger(__name__)
from .project_slug import decode_project_path, decode_project_slug
from .project_detail import ProjectDetailScreen
from .session_detail import (
    SessionDetailScreen,
    _PLOTEXT_COLOR_CYCLE,
    _PLOTEXT_THEME_NAME,
)
from .tailer import Tailer


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_usd(v: float) -> str:
    return f"${v:.4f}"


def _fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "-"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    total_s = int((end - start).total_seconds())
    if total_s < 0:
        return "-"
    if total_s < 60:
        return f"{total_s}s"
    if total_s < 3600:
        return f"{total_s // 60}m"
    if total_s < 86400:
        h, m = divmod(total_s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(total_s, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def _human(n: int) -> str:
    """Compact integer formatting: 1234 -> 1.23K, 1_234_567 -> 1.23M, etc."""
    if n < 1_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n/1_000:.2f}K"
    if n < 1_000_000_000:
        return f"{n/1_000_000:.2f}M"
    if n < 1_000_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    return f"{n/1_000_000_000_000:.2f}T"


def _context_limit_for(model: str, observed_max: int = 0) -> int:
    """Best-effort context window size for a Claude session.

    The Anthropic API only returns the canonical model id (e.g.
    'claude-opus-4-7') even when the user is running the 1M-context
    variant; the '[1m]' suffix is purely a Claude Code local marker.
    Strategy:
      - explicit '[1m]' in the model string -> 1M
      - we've observed a single turn larger than 200K in this session
        -> must be the 1M variant -> 1M
      - otherwise fall back to the standard 200K window
    """
    if model and "[1m]" in model:
        return 1_000_000
    if observed_max > 200_000:
        return 1_000_000
    return 200_000


class FilterInput(Input):
    """Search Input with extra word-deletion bindings.

    Textual's stock Input already maps ctrl+w to delete_left_word, but
    Notepad-style ctrl+backspace / alt+backspace are conventional in
    GUI editors. Adding both as aliases — modern terminals (Kitty,
    WezTerm, alacritty in CSI-u mode) report them as distinct keys."""

    BINDINGS = [
        Binding("ctrl+backspace", "delete_left_word", "Delete word"),
        Binding("alt+backspace", "delete_left_word", "Delete word"),
    ]


def _ctx_cell(used: int, limit: int) -> Text:
    """Render '████░░░░ 22%' as a Text cell for DataTable.

    Filled blocks use the bar color as BOTH foreground and background so
    the bar is visible even when DataTable inverts foreground on the
    selected row (bg color survives the inversion). Empty blocks use a
    dim '░'. Percentage suffix is colored separately.
    """
    if limit <= 0 or used <= 0:
        return Text("-", style="dim")
    pct = used / limit * 100
    bar_w = 8
    filled = min(int(round(min(pct, 100.0) / 100 * bar_w)), bar_w)
    if pct < 60:
        color = "green"
    elif pct < 85:
        color = "yellow"
    else:
        color = "red"
    cell = Text()
    cell.append("█" * filled, style=f"{color} on {color}")
    cell.append("░" * (bar_w - filled), style="grey50")
    cell.append(" ")
    cell.append(f"{pct:.0f}%", style=color)
    return cell


def _human_usd(v: float) -> str:
    if v < 10:
        return f"${v:.4f}"
    if v < 1_000:
        return f"${v:,.2f}"
    return f"${v:,.0f}"


def _fmt_dollar_tick(v: float) -> str:
    """Format a y-axis cost tick. Aims for short, scannable strings:
    $1.3K instead of '1292.8', $25 instead of '25.0', $0 for zero."""
    if v <= 0:
        return "$0"
    if v >= 1_000:
        kv = v / 1_000
        return f"${kv:.1f}K" if kv < 10 else f"${kv:.0f}K"
    if v >= 10:
        return f"${v:.0f}"
    return f"${v:.2f}"


def _fmt_token_tick(v: float) -> str:
    """Format a y-axis token tick: 1.2M / 5K / 0. Mirrors _fmt_dollar_tick
    in shape (no currency prefix, K/M/B suffix instead)."""
    if v <= 0:
        return "0"
    if v >= 1_000_000_000:
        gv = v / 1_000_000_000
        return f"{gv:.1f}B" if gv < 10 else f"{gv:.0f}B"
    if v >= 1_000_000:
        mv = v / 1_000_000
        return f"{mv:.1f}M" if mv < 10 else f"{mv:.0f}M"
    if v >= 1_000:
        kv = v / 1_000
        return f"{kv:.1f}K" if kv < 10 else f"{kv:.0f}K"
    return f"{int(v)}"


class SummaryPanel(Static):
    """Top panel: totals + rolling weekly aggregate + live rate."""

    sums: reactive[TokenSums] = reactive(TokenSums)
    sums_7d: reactive[TokenSums] = reactive(TokenSums)
    session_count: reactive[int] = reactive(0)
    active_count: reactive[int] = reactive(0)
    rate_tokens: reactive[float] = reactive(0.0)
    rate_cost: reactive[float] = reactive(0.0)
    rate_turns: reactive[float] = reactive(0.0)

    def render(self) -> RenderableType:
        header = Text.from_markup(
            f"[b]Sessions:[/b] {self.session_count} "
            f"([b green]{self.active_count} active[/b green], <30m idle)    "
            f"[b]Rate (last 60s):[/b] "
            f"{self.rate_turns:.1f} turns/min · "
            f"${self.rate_cost:,.2f}/min · "
            f"{_human(int(self.rate_tokens))} tok/min"
        )

        table = Table.grid(padding=(0, 2), pad_edge=False)
        table.add_column(justify="left", style="bold")
        for _ in range(6):
            table.add_column(justify="left")

        table.add_row(
            "",
            Text("Input", style="dim"),
            Text("Output", style="dim"),
            Text("Cache R", style="dim"),
            Text("Cache W", style="dim"),
            Text("Cost", style="dim"),
            Text("Turns", style="dim"),
        )
        s = self.sums
        table.add_row(
            "Total (all-time)",
            _human(s.input),
            _human(s.output),
            _human(s.cache_read),
            _human(s.cache_write_5m + s.cache_write_1h),
            _human_usd(s.cost_usd),
            f"{s.turns:,}",
        )
        w = self.sums_7d
        table.add_row(
            "Last 7d",
            _human(w.input),
            _human(w.output),
            _human(w.cache_read),
            _human(w.cache_write_5m + w.cache_write_1h),
            _human_usd(w.cost_usd),
            f"{w.turns:,}",
        )

        return Group(header, Text(""), table)


class BlockPanel(Static):
    """Live status of the current 5-hour Anthropic session block.

    Prefers authoritative utilization data from Anthropic's /api/oauth/usage
    when available (set via .api_usage); otherwise falls back to a local
    estimate based on parsed JSONL records and a configured token/cost limit.
    """

    info: reactive[BlockInfo | None] = reactive(None, layout=True)
    api_usage: reactive[UsageData | None] = reactive(None, layout=True)

    BAR_W = 30

    def render(self) -> RenderableType:
        api = self.api_usage
        info = self.info

        # No API data at all (likely API user without OAuth or first-startup
        # before the initial fetch returns).
        if api is None:
            return Group(
                Text("⏱  Anthropic API", style="bold"),
                Text("Waiting for first API response…", style="dim italic"),
            )

        # We have *some* API data — either fresh or a cached failure marker.
        # The header always shows plan + reset times so the user knows when
        # the next window opens, even when we're temporarily rate-limited.
        plan_str = f" · plan: [b]{api.plan_name}[/b]" if api.plan_name else ""

        now = datetime.now(tz=timezone.utc)
        reset_parts: list[str] = []
        if api.five_hour is not None:
            five_local = api.five_hour.resets_at.astimezone()
            five_remaining = (api.five_hour.resets_at - now).total_seconds() / 60.0
            reset_parts.append(
                f"5h resets [b]{five_local.strftime('%H:%M')}[/b] (in {_fmt_minutes(five_remaining)})"
            )
        if api.seven_day is not None:
            seven_local = api.seven_day.resets_at.astimezone()
            seven_remaining = (api.seven_day.resets_at - now).total_seconds() / 60.0
            reset_parts.append(
                f"7d resets [b]{seven_local.strftime('%d-%m %H:%M')}[/b] "
                f"(in {_fmt_duration_minutes(seven_remaining)})"
            )

        header_text = f"[b]⏱  Anthropic API[/b]{plan_str}"
        if reset_parts:
            header_text += "  ·  " + "  ·  ".join(reset_parts)
        header = Text.from_markup(header_text)

        lines: list[Text] = [header]

        # Progress bars (only if we have actual values; empty cached failure
        # without prior data leaves them out).
        if api.five_hour is not None:
            lines.append(self._progress_line("5h ", api.five_hour.utilization, ""))
        if api.seven_day is not None:
            lines.append(self._progress_line("7d ", api.seven_day.utilization, ""))

        # Local context line: burn rate + raw tokens/cost from the JSONL
        # archive. Useful even when the API is happy because the API does
        # not give burn rate.
        if info is not None:
            sums = info.sums
            lines.append(Text.from_markup(
                f"[b]Local[/b]   tokens={_human(sums.total_tokens)}  ·  "
                f"cost=${sums.cost_usd:,.2f}  ·  "
                f"burn={_human(int(info.burn_tokens_per_min))} tok/min · "
                f"${info.burn_cost_per_min:,.2f}/min"
            ))

        # Surface API failures inline rather than dropping back to a
        # local-only view.
        if api.api_unavailable:
            stale_age = max(0, int(time.time() - api.fetched_at))
            if api.retry_after_epoch:
                wait_s = max(0, int(api.retry_after_epoch - time.time()))
                wait_str = f"retry in {_fmt_minutes(wait_s/60)}"
            else:
                wait_str = "retrying with backoff"
            lines.append(Text.from_markup(
                f"[yellow]API: {api.error}[/yellow]  ·  "
                f"last update {stale_age}s ago  ·  {wait_str}"
            ))

        return Group(*lines)

    def _progress_line(self, label: str, pct: float, suffix: str) -> Text:
        pct = max(0.0, pct)
        if pct < 80:
            color = "green"
        elif pct < 100:
            color = "yellow"
        else:
            color = "red"
        # Cap visual fill at 100%; numeric pct is shown separately so the
        # truth still leaks through for over-plan users.
        filled = min(int(round(min(pct, 100.0) / 100 * self.BAR_W)), self.BAR_W)
        bar = "█" * filled + "·" * (self.BAR_W - filled)
        line = Text()
        line.append_text(Text.from_markup(f"[b]{label}[/b]  "))
        line.append(bar, style=color)
        # Compact pct display: 12%, 99%, 234%, 12K%
        if pct < 1000:
            pct_str = f"{pct:.0f}%"
        else:
            pct_str = f"{pct/1000:.0f}K%"
        if suffix:
            line.append(f"  {suffix}  {pct_str}")
        else:
            line.append(f"  {pct_str}")
        return line

    @staticmethod
    def _eta_verdict(eta_min: float, block_remaining_min: float) -> tuple[str, str]:
        """Return (label, rich-style-color) comparing ETA to block end."""
        if eta_min >= block_remaining_min:
            return ("(after block ends ✓)", "green")
        slack = block_remaining_min - eta_min
        if slack < 30:
            return ("(before block ends ⚠)", "yellow")
        return ("(before block ends ✗)", "red")


def _fmt_minutes(m: float) -> str:
    if m < 0:
        return "0m"
    if m < 60:
        return f"{int(m)}m"
    h, mm = divmod(int(m), 60)
    return f"{h}h {mm}m" if mm else f"{h}h"


def _fmt_duration_minutes(m: float) -> str:
    """Like _fmt_minutes but spans up to days (used for 7d reset countdown)."""
    if m < 0:
        return "0m"
    if m < 60:
        return f"{int(m)}m"
    if m < 60 * 24:
        h, mm = divmod(int(m), 60)
        return f"{h}h {mm}m" if mm else f"{h}h"
    d, rem = divmod(int(m), 60 * 24)
    h = rem // 60
    return f"{d}d {h}h" if h else f"{d}d"


class UsageMonitorApp(App):
    CSS = """
    /* Whole main view painted with $panel — same approach as the detail
       screen, so summary, block panel, tabs, table content, and status
       bar all read as one unified surface. */
    Screen { layout: vertical; background: $panel; }
    SummaryPanel {
        height: 8;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $primary;
    }
    BlockPanel {
        height: 7;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $primary;
    }
    /* Tabs + filter bar + content switcher — three vertical bands.
       Tabs on top, filter bar one line below them, table content fills
       the rest. */
    #main-tabs { background: $panel; }
    #main-content { height: 1fr; background: $panel; }
    #main-content > Container { background: $panel; }
    #filter-bar {
        height: 1;
        padding: 0 2;
        background: $panel;
    }
    #filter-search {
        width: 24;
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel-lighten-1;
        margin-right: 2;
    }
    #filter-controls {
        width: 1fr;
        height: 1;
        content-align: left middle;
        color: $text;
    }
    DataTable { height: 1fr; background: $panel; }
    #t-models { height: 1fr; }
    /* Two plotext stacked-bars side by side — tokens and cost over the
       same 7-day window, splitting horizontal space 50/50. A single
       shared legend (#chart-legend) sits below both rows since the
       same models drive both charts. */
    #models-charts { height: 1fr; }
    #chart-tokens-time, #chart-cost-time {
        width: 1fr;
        height: 1fr;
        padding: 1 1;
        border: round $primary;
        background: $panel;
    }
    .chart-legend {
        height: auto;
        padding: 0 2;
        background: $panel;
    }
    #status-bar {
        height: 1;
        dock: bottom;
        background: $panel;
        color: $text;
    }
    #status-left { width: 1fr; padding: 0 1; content-align: left middle; }
    #status-right { width: auto; padding: 0 1; content-align: right middle; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "show_tab('sessions')", "Sessions"),
        Binding("2", "show_tab('projects')", "Projects"),
        Binding("3", "show_tab('models')", "Models"),
        # Filters — only fire when search Input doesn't have focus.
        Binding("slash", "focus_search", "Search"),
        Binding("h", "cycle_filter('hide_deleted')", "Hide missing"),
        Binding("d", "cycle_filter('date')", "Date filter"),
        Binding("c", "cycle_filter('cost')", "Cost filter"),
        Binding("m", "cycle_filter('model')", "Model filter"),
        # Open actions — context-aware (cursor row + active tab). Letters
        # rather than F-keys because F1-Fn aren't reliable on laptops
        # (Fn-lock) or remote/web terminals.
        Binding("o", "open_in_explorer", "Open in file manager"),
        Binding("n", "open_claude_primary", "Open Claude Code"),
        Binding("s", "open_claude_resume_last", "Resume last (project)"),
        Binding("f1", "open_in_explorer", "Open in file manager", show=False),
        Binding("f2", "open_claude_primary", "Open Claude Code", show=False),
        Binding("f3", "open_claude_resume_last", "Resume last (project)", show=False),
        Binding("l", "open_log", "Open log file"),
        Binding("comma", "open_settings", "Settings"),
    ]

    # Filter state — watched so any change forces a table refresh.
    filter_search: reactive[str] = reactive("")
    filter_hide_deleted: reactive[bool] = reactive(False)
    _FILTER_CYCLES: dict[str, list[str]] = {
        "date": ["all", "24h", "7d", "30d"],
        "cost": ["all", "1", "10", "100", "1000", "10000"],
        "model": ["all", "opus", "sonnet", "haiku"],
    }
    filter_date: reactive[str] = reactive("all")
    filter_cost: reactive[str] = reactive("all")
    filter_model: reactive[str] = reactive("all")

    def __init__(
        self,
        aggregator: Aggregator,
        tailer: Tailer,
        queue: asyncio.Queue,
        auto_limits: bool = False,
        use_api: bool = True,
    ):
        super().__init__()
        self.aggregator = aggregator
        self.tailer = tailer
        self.queue = queue
        self.auto_limits = auto_limits
        self.use_api = use_api

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryPanel(id="summary")
        yield BlockPanel(id="block")
        # Tabs strip first, then filter bar in its own row, then content
        # switcher with the actual tables. Decoupling Tabs from the
        # bundled TabbedContent lets us slip the filter bar between them
        # without competing for horizontal space.
        yield Tabs(
            Tab("Sessions [1]", id="sessions"),
            Tab("Projects [2]", id="projects"),
            Tab("Models [3]", id="models"),
            id="main-tabs",
        )
        with Horizontal(id="filter-bar"):
            yield FilterInput(
                placeholder="search…  (/)",
                id="filter-search",
            )
            yield Static(
                "[b]h[/b] [ ] hide missing   "
                "[b]d[/b] date: all   "
                "[b]c[/b] cost: all   "
                "[b]m[/b] model: all",
                id="filter-controls",
            )
        with ContentSwitcher(initial="sessions", id="main-content"):
            with Container(id="sessions"):
                yield DataTable(id="t-sessions", cursor_type="row", zebra_stripes=True)
            with Container(id="projects"):
                yield DataTable(id="t-projects", cursor_type="row", zebra_stripes=True)
            with Container(id="models"):
                yield DataTable(id="t-models", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="models-charts"):
                    tokens_plot = PlotextPlot(id="chart-tokens-time")
                    tokens_plot.theme = _PLOTEXT_THEME_NAME
                    yield tokens_plot
                    cost_plot = PlotextPlot(id="chart-cost-time")
                    cost_plot.theme = _PLOTEXT_THEME_NAME
                    yield cost_plot
                # Single shared legend below both charts — same model
                # set drives both, so duplicating per-chart was noisy.
                yield Static("", id="chart-legend", classes="chart-legend")
        with Horizontal(id="status-bar"):
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    def on_mount(self) -> None:
        cfg = load_config()
        saved_theme = cfg.get("theme")
        if saved_theme:
            try:
                self.theme = saved_theme
            except Exception:
                pass
        # Push date format into the formatting module's global state
        # so all _fmt_* helpers see the user's preference on the very
        # first refresh tick rather than after a reload.
        _apply_format_config(date_format=cfg.get("date_format"))
        self.watch(self, "theme", self._on_theme_change)

        self._setup_tables()
        self._update_filter_hint()
        self._update_status_right()
        self.run_worker(self._consume_queue(), exclusive=False)
        self.run_worker(self._tailer_runner(), exclusive=False)
        self.set_interval(0.5, self._refresh_view)
        if self.auto_limits:
            # Recompute P90 limits every 30s — they shift as the rolling
            # 8-day window of historical blocks evolves.
            self.set_interval(30.0, self._recompute_auto_limits)
            self._recompute_auto_limits()

        if self.use_api:
            # Poll Anthropic /api/oauth/usage every 120s (matches the
            # cache TTL). 1 call / 2min is safely under any reasonable
            # account-level rate limit.
            self.set_interval(120.0, self._poll_api_usage)
            self.run_worker(self._poll_api_usage_async, exclusive=False)

    async def _poll_api_usage_async(self) -> None:
        """Initial fetch in a worker — keeps startup non-blocking."""
        try:
            data = await asyncio.to_thread(get_usage, force_refresh=False)
        except Exception:
            log.exception("API usage poll crashed")
            return
        self.aggregator.api_usage = data

    def _poll_api_usage(self) -> None:
        """Periodic refresh — schedules a thread call so the UI doesn't
        block on the HTTP request."""
        self.run_worker(self._poll_api_usage_async, exclusive=False)

    def _recompute_auto_limits(self) -> None:
        result = self.aggregator.auto_detect_limits_p90()
        if result is None:
            return
        token_limit, cost_limit = result
        self.aggregator.token_limit = token_limit
        self.aggregator.cost_limit = cost_limit

    def _on_theme_change(self, new_theme: str) -> None:
        log.info("theme changed to %s", new_theme)
        cfg = load_config()
        cfg["theme"] = new_theme
        save_config(cfg)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a sessions / projects row drills down into the
        # respective detail screen. Both DataTables sit on the same
        # screen; dispatch by widget id.
        if event.row_key is None or event.row_key.value is None:
            return
        table_id = event.data_table.id
        key = str(event.row_key.value)
        if table_id == "t-sessions":
            log.info("drill-in: SessionDetailScreen(%s)", key[:8])
            self.push_screen(SessionDetailScreen(key, self.aggregator))
        elif table_id == "t-projects":
            log.info("drill-in: ProjectDetailScreen(%s)", key)
            self.push_screen(ProjectDetailScreen(key, self.aggregator))

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        # Switch the ContentSwitcher to the newly-activated pane and
        # force-refresh that table.
        if event.tab is None:
            return
        active = event.tab.id
        log.info("tab activated: %s", active)
        try:
            switcher = self.query_one("#main-content", ContentSwitcher)
            switcher.current = active
        except Exception:
            return
        # Models tab is a flat global aggregate; nothing to filter, so
        # hide the filter bar (saves a row, signals the no-op state).
        try:
            bar = self.query_one("#filter-bar")
            bar.display = active != "models"
        except Exception:
            pass
        self._update_status_right()
        self._refresh_view()
        # Move keyboard focus into the table of the newly-activated tab.
        try:
            self.query_one(f"#t-{active}", DataTable).focus()
        except Exception:
            pass

    def _update_status_right(self) -> None:
        """Tab-specific keybinding hints in the status bar. Per-row /
        per-tab actions go on the left; globals on the right. Letters
        rather than F-keys because F1-Fn aren't reliable on laptops
        (Fn-lock) or remote/web terminals."""
        try:
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
        except Exception:
            return
        active = self._active_tab()
        if active == "sessions":
            actions = (
                "[b]o[/b] open dir   "
                "[b]s[/b] resume session   "
                "[b]↵[/b] details"
            )
        elif active == "projects":
            actions = (
                "[b]o[/b] open dir   "
                "[b]n[/b] new claude   "
                "[b]s[/b] resume last   "
                "[b]↵[/b] details"
            )
        else:
            actions = ""
        left.update(actions)
        right.update(
            "[b]Tab[/b] / [b]shift+Tab[/b] focus   "
            "[b],[/b] settings   [b]l[/b] log   [b]q[/b] quit"
        )

    SESSIONS_COLS = [
        ("Session", "sid"),
        ("Project", "proj"),
        ("Exists", "exists"),
        ("Last", "last"),
        ("Duration", "dur"),
        ("Cost", "cost"),
        ("Turns", "turns"),
        ("$/turn", "per_turn"),
        ("Context", "ctx"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("CacheW", "cache_w"),
        ("Cache%", "cache_pct"),
    ]
    MODELS_COLS = [
        ("Model", "model"),
        ("Turns", "turns"),
        ("In", "in"),
        ("Out", "out"),
        ("Out/In", "out_in"),
        ("CacheR", "cache_r"),
        ("CacheW", "cache_w"),
        ("Cache%", "cache_pct"),
        ("Total", "total"),
        ("Cost", "cost"),
        ("$/turn", "per_turn"),
    ]
    PROJECTS_COLS = [
        ("Project", "project"),
        ("Exists", "exists"),
        ("Sessions", "sessions"),
        ("First seen", "first"),
        ("Last activity", "last"),
        ("Cost", "cost"),
        ("Last 7d", "last_7d"),
        ("$/session", "per_session"),
        ("Tokens", "tokens"),
    ]

    def _setup_tables(self) -> None:
        for table_id, cols in (
            ("#t-sessions", self.SESSIONS_COLS),
            ("#t-models", self.MODELS_COLS),
            ("#t-projects", self.PROJECTS_COLS),
        ):
            t = self.query_one(table_id, DataTable)
            for label, key in cols:
                t.add_column(label, key=key)
        # Cache last-rendered cell tuple per row, indexed by table_id then
        # row_key, so we can skip update_cell calls for unchanged rows and
        # drop the whole table's cache on rebuild.
        self._row_cache: dict[str, dict[str, tuple[str, ...]]] = {
            "#t-sessions": {},
            "#t-models": {},
            "#t-projects": {},
        }

    async def _tailer_runner(self) -> None:
        try:
            await self.tailer.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("tailer crashed")
            self.notify(f"Tailer error: {e}", severity="error")

    async def _consume_queue(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                self.aggregator.ingest(item)
            except Exception as e:
                log.exception("ingest crashed")
                self.notify(f"Ingest error: {e}", severity="error")

    def _refresh_view(self) -> None:
        agg = self.aggregator
        summary = self.query_one("#summary", SummaryPanel)
        summary.sums = agg.total_sums()
        summary.sums_7d = agg.sums_in_window(timedelta(days=7))
        summary.rate_tokens = agg.recent_token_rate_per_min()
        summary.rate_cost = agg.recent_cost_per_min()
        summary.rate_turns = agg.recent_turns_per_min()
        summary.session_count = len(agg.sessions)
        summary.active_count = agg.active_session_count()
        summary.refresh()

        block_panel = self.query_one("#block", BlockPanel)
        block_panel.info = agg.block_info()
        block_panel.api_usage = agg.api_usage
        block_panel.refresh()

        # Refresh only the visible tab — keeps the UI responsive even at
        # hundreds of rows. The other tables are still in-sync from previous
        # refreshes; they'll catch up the moment the user switches tabs.
        try:
            active = self.query_one("#main-tabs", Tabs).active
        except Exception:
            active = "sessions"
        if active == "sessions":
            self._refresh_sessions_table()
        elif active == "models":
            self._refresh_models_table()
        elif active == "projects":
            self._refresh_projects_table()

    def _apply_rows(
        self,
        table_id: str,
        cols: list[tuple[str, str]],
        rows: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        """Reconcile a DataTable to the desired (ordered) row list.

        If the desired order matches the current physical order, only
        cells that changed are written — cursor and scroll stay put.
        If the order differs (rare: new session, ranking change), the
        table is rebuilt and the cursor is restored to the same row_key.
        """
        table = self.query_one(table_id, DataTable)
        cache = self._row_cache[table_id]
        col_keys = [k for _, k in cols]

        desired_order = [k for k, _ in rows]
        desired_cells = dict(rows)
        current_order = [str(rk.value) for rk in table.rows.keys()]

        if current_order == desired_order:
            for key, cells in rows:
                prev = cache.get(key)
                if prev == cells:
                    continue
                for col_key, new_val, old_val in zip(
                    col_keys, cells, prev or (None,) * len(cells)
                ):
                    if new_val != old_val:
                        try:
                            table.update_cell(key, col_key, new_val)
                        except Exception:
                            pass
                cache[key] = cells
            return

        # Order differs — rebuild and restore cursor by row_key.
        saved_key: str | None = None
        try:
            cur_row = table.cursor_coordinate.row
            if 0 <= cur_row < len(current_order):
                saved_key = current_order[cur_row]
        except Exception:
            saved_key = None
        saved_col = table.cursor_coordinate.column if table.row_count else 0

        table.clear()
        cache.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
            cache[key] = cells

        if saved_key is not None and saved_key in desired_cells:
            try:
                new_idx = desired_order.index(saved_key)
                table.move_cursor(row=new_idx, column=saved_col)
            except Exception:
                pass

    def _refresh_sessions_table(self) -> None:
        # Sort: existing-project sessions on top, then deleted ones, both
        # groups internally sorted by last_seen DESC (newest first). Same
        # convention as the Projects tab — the deleted block stays at
        # the bottom, dimmed, but doesn't disappear.
        from pathlib import Path

        def _resolved_path(s) -> str | None:
            # Prefer the captured cwd (ground truth for this session's
            # actual working dir) over slug-decode, which only knows
            # about the project root and would mark deeper subdir
            # sessions as 'existing' even when their cwd is gone.
            return s.cwd or decode_project_path(s.project_slug)

        def _exists(s) -> bool:
            p = _resolved_path(s)
            return bool(p and Path(p).is_dir())

        rows: list[tuple[str, tuple[str, ...]]] = []
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        sorted_sessions = sorted(
            self.aggregator.sessions.values(),
            key=lambda s: (_exists(s), s.last_seen or epoch),
            reverse=True,
        )
        # Resolve filter constants once per refresh.
        date_cutoff = self._date_filter_cutoff()
        cost_min = self._cost_filter_min()
        model_substr = self._model_filter_substr()
        search_q = self.filter_search.strip().lower()
        for s in sorted_sessions:
            if not self._session_matches_filters(
                s, _resolved_path(s), _exists(s),
                date_cutoff, cost_min, model_substr, search_q,
            ):
                continue
            total_in = (
                s.sums.cache_read
                + s.sums.input
                + s.sums.cache_write_5m
                + s.sums.cache_write_1h
            )
            cache_pct = (s.sums.cache_read / total_in * 100) if total_in else 0.0
            per_turn = (s.sums.cost_usd / s.sums.turns) if s.sums.turns else 0.0
            # Use the same resolved path the sort key inspected so the
            # ✓/✗ marker and the row's group always agree.
            real_path = _resolved_path(s)
            if real_path:
                project_name = real_path.rsplit("/", 1)[-1]
            else:
                project_name = decode_project_slug(s.project_slug)
            exists = _exists(s)
            ctx_limit = _context_limit_for(s.last_context_model, s.max_context_tokens)
            style = "" if exists else "dim"

            def _styled(value):
                if not style:
                    return value
                # Wrap plain string cells with dim Text. Renderable
                # values (Text/Group from _ctx_cell) get a copy with the
                # dim style stacked on.
                if isinstance(value, str):
                    return Text(value, style=style)
                return value
            cells = (
                _styled(s.session_id[:8]),
                _styled(project_name[-30:] if len(project_name) > 30 else project_name),
                Text("✓", style="green") if exists else Text("✗", style="red dim"),
                _styled(_fmt_datetime(s.last_seen)),
                _styled(_fmt_duration(s.first_seen, s.last_seen)),
                _styled(_fmt_usd(s.sums.cost_usd)),
                _styled(_human(s.sums.turns)),
                _styled(f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}"),
                _ctx_cell(s.last_context_tokens, ctx_limit),
                _styled(_human(s.sums.input)),
                _styled(_human(s.sums.output)),
                _styled(_human(s.sums.cache_read)),
                _styled(_human(s.sums.cache_write_5m + s.sums.cache_write_1h)),
                _styled(f"{cache_pct:.1f}%"),
            )
            rows.append((s.session_id, cells))
        self._apply_rows("#t-sessions", self.SESSIONS_COLS, rows)

    def _refresh_models_table(self) -> None:
        per_model: dict[str, TokenSums] = {}
        for sess in self.aggregator.sessions.values():
            for model, sums in sess.by_model.items():
                m = per_model.setdefault(model, TokenSums())
                m.input += sums.input
                m.output += sums.output
                m.cache_read += sums.cache_read
                m.cache_write_5m += sums.cache_write_5m
                m.cache_write_1h += sums.cache_write_1h
                m.cost_usd += sums.cost_usd
                m.turns += sums.turns
        rows: list[tuple[str, tuple[str, ...]]] = []
        # Most-used first (by turn count). Tie-break alphabetically so order
        # is deterministic when two models have identical turns.
        for model, sums in sorted(
            per_model.items(), key=lambda kv: (-kv[1].turns, kv[0])
        ):
            cache_w = sums.cache_write_5m + sums.cache_write_1h
            total = sums.input + sums.output + sums.cache_read + cache_w
            # Cache% = share of input that came from cache hits — high is
            # good (cheap reads). Denominator is "input-side" tokens only.
            input_side = sums.input + sums.cache_read + cache_w
            cache_pct = (
                sums.cache_read / input_side * 100 if input_side else 0.0
            )
            # Out/In = how chatty the model is per unit of input. Excludes
            # cache from the denominator so we measure real prompt size.
            out_in = sums.output / sums.input if sums.input else 0.0
            per_turn = (
                sums.cost_usd / sums.turns if sums.turns else 0.0
            )
            cells = (
                humanize_model_name(model) or "(unknown)",
                _human(sums.turns),
                _human(sums.input),
                _human(sums.output),
                f"{out_in:.2f}",
                _human(sums.cache_read),
                _human(cache_w),
                f"{cache_pct:.1f}%",
                _human(total),
                _fmt_usd(sums.cost_usd),
                _fmt_usd(per_turn),
            )
            # Row key stays on the raw model id so DataTable diff-update
            # keeps tracking the right row across renders even when the
            # display name changes.
            rows.append((model or "(unknown)", cells))
        self._apply_rows("#t-models", self.MODELS_COLS, rows)
        self._refresh_models_charts(per_model)

    def _refresh_models_charts(self, per_model: dict[str, TokenSums]) -> None:
        """Render the per-day charts and a single shared legend.

        Both charts pull from the same 7-day _long_window archive but
        one slices on cost and the other on tokens. Model order (and
        therefore colors) is shared across both so the single legend
        below them is unambiguous — without this, sorting each chart
        independently by its own metric would produce inconsistent
        color→model mappings between the two charts.
        """
        days = 7
        cost_dates, cost_per_model = self.aggregator.cost_per_day_per_model(
            days=days
        )
        token_dates, token_per_model = self.aggregator.tokens_per_day_per_model(
            days=days
        )

        # Union filter: keep a model if it crosses 1% of total in
        # *either* cost or tokens. Single-metric filters drop haiku
        # (tiny cost, lots of tokens) or vice versa from the chart
        # where it actually matters.
        cost_total = sum(sum(v) for v in cost_per_model.values()) or 1.0
        token_total = sum(sum(v) for v in token_per_model.values()) or 1.0
        cost_share = {
            m: sum(v) / cost_total for m, v in cost_per_model.items()
        }
        token_share = {
            m: sum(v) / token_total for m, v in token_per_model.items()
        }
        all_models = set(cost_per_model) | set(token_per_model)
        keep = [
            m for m in all_models
            if cost_share.get(m, 0.0) > 0.01
            or token_share.get(m, 0.0) > 0.01
        ] or list(all_models)
        # Sort by cost share desc — same order applied to both charts so
        # the bottom of each stack is consistent across them.
        keep.sort(key=lambda m: -cost_share.get(m, 0.0))

        zeros = [0.0] * days
        cost_series = [cost_per_model.get(m, zeros) for m in keep]
        token_series = [token_per_model.get(m, zeros) for m in keep]
        colors = [
            _PLOTEXT_COLOR_CYCLE[i % len(_PLOTEXT_COLOR_CYCLE)]
            for i in range(len(keep))
        ]

        self._render_stacked_chart(
            "#chart-tokens-time", token_dates, token_series, colors,
            title="Tokens / day per model",
            ylabel="tokens", tick_fmt=_fmt_token_tick,
        )
        self._render_stacked_chart(
            "#chart-cost-time", cost_dates, cost_series, colors,
            title="Cost / day per model",
            ylabel="$", tick_fmt=_fmt_dollar_tick,
        )

        # Single legend, Rich markup with RGB-tuple bullets matching the
        # exact colors plotext used for each stacked-bar segment.
        try:
            legend_widget = self.query_one("#chart-legend", Static)
        except Exception:
            return
        if not keep:
            legend_widget.update("")
            return
        parts = [
            f"[rgb({r},{g},{b})]●[/] {humanize_model_name(name)}"
            for name, (r, g, b) in zip(keep, colors)
        ]
        legend_widget.update("   ".join(parts))

    def _render_stacked_chart(
        self,
        chart_id: str,
        dates,
        series_values: list[list[float]],
        colors: list[tuple[int, int, int]],
        *,
        title: str,
        ylabel: str,
        tick_fmt,
    ) -> None:
        """Draw a plotext stacked_bar with explicit colors so the chart
        matches the shared legend's dot colors. ``colors`` length must
        match ``series_values`` length."""
        try:
            plot = self.query_one(chart_id, PlotextPlot)
        except Exception:
            return

        if not series_values or not any(any(v) for v in series_values):
            p = plot.plt
            p.clear_data()
            p.clear_figure()
            p.title(f"{title} (no data in last 7d)")
            plot.refresh()
            return

        labels = [d.strftime("%m-%d") for d in dates]
        max_stack = max(
            (sum(vals[i] for vals in series_values) for i in range(len(labels))),
            default=0.0,
        )
        tick_positions: list[float] = []
        tick_labels: list[str] = []
        if max_stack > 0:
            tick_positions = [
                0.0, max_stack * 0.25, max_stack * 0.5,
                max_stack * 0.75, max_stack,
            ]
            tick_labels = [tick_fmt(v) for v in tick_positions]

        p = plot.plt
        p.clear_data()
        p.clear_figure()
        # Pass colors explicitly — plotext's theme-registered _sequence
        # isn't honored by stacked_bar in this version (it falls back
        # to its global default cycle), so the legend dots wouldn't
        # match without this kwarg.
        p.stacked_bar(labels, series_values, color=colors)
        p.title(f"{title} (last 7d)")
        p.xlabel("date")
        p.ylabel(ylabel)
        if tick_positions:
            p.yticks(tick_positions, tick_labels)
        plot.refresh()

    def _refresh_projects_table(self) -> None:
        """Aggregate every session by its project_slug. Sums cost /
        tokens / session-count, tracks first/last activity, computes
        last-7d cost from the long_window archive, and probes the
        filesystem to mark deleted projects. Sorted: existing first
        (so live projects sit at the top), deleted ones below — both
        groups internally sorted by last activity desc."""
        from collections import defaultdict
        from pathlib import Path

        agg: dict[str, dict] = defaultdict(
            lambda: {
                "sessions": 0,
                "cost": 0.0,
                "tokens": 0,
                "last_seen": None,
                "first_seen": None,
                "cwd": None,
                # Lower-cased model id set so the Projects model filter
                # ('opus' / 'sonnet' / 'haiku') can do a substring check
                # without re-walking sessions.
                "models": set(),
            }
        )
        for sess in self.aggregator.sessions.values():
            entry = agg[sess.project_slug]
            entry["sessions"] += 1
            entry["cost"] += sess.sums.cost_usd
            entry["tokens"] += sess.sums.total_tokens
            if entry["cwd"] is None and sess.cwd:
                entry["cwd"] = sess.cwd
            entry["models"].update(m.lower() for m in sess.by_model)
            for ts_attr, key, cmp in (
                (sess.last_seen, "last_seen", lambda a, b: a > b),
                (sess.first_seen, "first_seen", lambda a, b: a < b),
            ):
                if ts_attr is None:
                    continue
                ts = ts_attr if ts_attr.tzinfo else ts_attr.replace(tzinfo=timezone.utc)
                if entry[key] is None or cmp(ts, entry[key]):
                    entry[key] = ts

        # Cost in the last 7 days, derived from the long_window archive.
        # Walk once, group by the session's project_slug.
        seven_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=7)
        last_7d: dict[str, float] = defaultdict(float)
        for ts, rec, cost in self.aggregator._long_window:
            if ts < seven_days_ago:
                continue
            sess = self.aggregator.sessions.get(rec.session_id)
            if sess is None:
                continue
            last_7d[sess.project_slug] += cost

        # Use the cwd captured from the session JSONL when available —
        # that's ground truth. Fall back to slug-decode for sessions that
        # haven't surfaced a cwd yet (early-life or hook-only state).
        existence: dict[str, bool] = {}
        for slug, entry in agg.items():
            real_path = entry["cwd"] or decode_project_path(slug)
            existence[slug] = bool(real_path and Path(real_path).is_dir())

        rows: list[tuple[str, tuple[str, ...]]] = []
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        # Sort key: existing first (so True > False under reverse=True),
        # then most-recent activity. Deleted projects fall to the bottom
        # of the table but stay visible.
        ordered = sorted(
            agg.items(),
            key=lambda kv: (
                existence.get(kv[0], False),
                kv[1]["last_seen"] or epoch,
            ),
            reverse=True,
        )
        search_q = self.filter_search.strip().lower()
        for slug, entry in ordered:
            if not self._project_matches_filters(
                slug, entry, existence.get(slug, False), search_q
            ):
                continue
            sessions_n = entry["sessions"]
            per_session = entry["cost"] / sessions_n if sessions_n else 0.0
            exists = existence.get(slug, False)
            # Dim the entire row when the project is gone so it visually
            # recedes; the ✗ marker in Exists is still readable.
            style = "" if exists else "dim"
            def _styled(s: str) -> Text:
                return Text(s, style=style) if style else Text(s)
            # Prefer the basename of the captured cwd (ground-truth name)
            # over the slug guess.
            real_path = entry["cwd"]
            project_name = (
                real_path.rsplit("/", 1)[-1] if real_path else decode_project_slug(slug)
            )
            cells = (
                _styled(project_name),
                Text("✓", style="green") if exists else Text("✗", style="red dim"),
                _styled(_human(sessions_n)),
                _styled(self._fmt_dt(entry["first_seen"])),
                _styled(self._fmt_dt(entry["last_seen"])),
                _styled(_fmt_usd(entry["cost"])),
                _styled(_fmt_usd(last_7d.get(slug, 0.0))),
                _styled(
                    f"${per_session:.2f}"
                    if per_session >= 1
                    else f"${per_session:.4f}"
                ),
                _styled(_human(entry["tokens"])),
            )
            rows.append((slug, cells))
        self._apply_rows("#t-projects", self.PROJECTS_COLS, rows)

    def _fmt_dt(self, ts: datetime | None) -> str:
        # Thin wrapper — Projects tab cells were calling this method form
        # before the formatting module existed. Keeps the call sites
        # readable rather than threading the import everywhere.
        return _fmt_datetime(ts)

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#main-tabs", Tabs).active = tab_id
        except Exception:
            pass

    def action_refresh(self) -> None:
        self._refresh_view()

    # ----- filter bar wiring -----

    def action_focus_search(self) -> None:
        try:
            self.query_one("#filter-search", Input).focus()
        except Exception:
            pass

    # ----- open actions (file manager / Claude Code) -----

    def _active_tab(self) -> str:
        try:
            return self.query_one("#main-tabs", Tabs).active
        except Exception:
            return "sessions"

    def _cursor_row_key(self, table_id: str) -> str | None:
        """row_key.value of the cursor row in the named DataTable, or None."""
        try:
            t = self.query_one(f"#t-{table_id}", DataTable)
        except Exception:
            return None
        keys = list(t.rows.keys())
        idx = t.cursor_row
        if not (0 <= idx < len(keys)):
            return None
        return str(keys[idx].value)

    def _project_path_for_session(self, session_id: str) -> str | None:
        sess = self.aggregator.sessions.get(session_id)
        if sess is None:
            return None
        return sess.cwd or decode_project_path(sess.project_slug)

    def _project_path_for_slug(self, slug: str) -> str | None:
        # The Projects table aggregates per slug; pull the cwd off any
        # session in that project (we already capture it on ingest).
        for sess in self.aggregator.sessions.values():
            if sess.project_slug == slug and sess.cwd:
                return sess.cwd
        return decode_project_path(slug)

    def _last_session_id_in_project(self, slug: str) -> str | None:
        latest = None
        latest_ts: datetime | None = None
        for sess in self.aggregator.sessions.values():
            if sess.project_slug != slug:
                continue
            if sess.last_seen is None:
                continue
            ts = sess.last_seen
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest = sess.session_id
        return latest

    def _open_dir_in_file_manager(self, path: str | None) -> None:
        ok, msg = open_in_file_manager(path)
        self.notify(msg, severity="information" if ok else "warning")

    def _resolve_open_path(self) -> str | None:
        """Path under the cursor for the active tab. Sessions tab uses
        the session's cwd; Projects tab uses any session's cwd in that
        slug. Models tab returns None (nothing path-shaped to open)."""
        active = self._active_tab()
        if active == "sessions":
            sid = self._cursor_row_key("sessions")
            return self._project_path_for_session(sid) if sid else None
        if active == "projects":
            slug = self._cursor_row_key("projects")
            return self._project_path_for_slug(slug) if slug else None
        return None

    def action_open_in_explorer(self) -> None:
        self._open_dir_in_file_manager(self._resolve_open_path())

    def action_open_claude_primary(self) -> None:
        """Sessions tab → resume cursor session.
        Projects tab → start a fresh Claude Code session in the project."""
        active = self._active_tab()
        if active == "sessions":
            sid = self._cursor_row_key("sessions")
            if not sid:
                self.notify("Move the cursor onto a row first", severity="warning")
                return
            path = self._project_path_for_session(sid)
            if not path:
                self.notify("Project path unknown", severity="warning")
                return
            ok, msg = open_terminal_with(path, ["claude", "--resume", sid])
        elif active == "projects":
            slug = self._cursor_row_key("projects")
            if not slug:
                self.notify("Move the cursor onto a row first", severity="warning")
                return
            path = self._project_path_for_slug(slug)
            if not path:
                self.notify("Project path unknown", severity="warning")
                return
            ok, msg = open_terminal_with(path, ["claude"])
        else:
            return
        if ok:
            self.notify(msg, timeout=2)
        else:
            self.notify(msg, severity="error")

    def action_open_log(self) -> None:
        """Spawn a new terminal tailing the log in real time.

        'less +F' behaves like 'tail -f' (auto-scrolls as new entries
        arrive) but lets the user press Ctrl-C to switch to normal less
        mode for scrollback / search. Fallback to the default text
        editor if no terminal emulator is available."""
        ok, msg = open_terminal_with(
            str(LOG_DIR), ["less", "+F", str(LOG_FILE)]
        )
        if ok:
            self.notify(f"Tailing log in {msg.split()[-1]}", timeout=2)
            return
        # Couldn't open a terminal — fall back to xdg-open / default app.
        ok, fallback_msg = open_file(LOG_FILE)
        self.notify(
            fallback_msg, severity="information" if ok else "warning"
        )

    def action_open_settings(self) -> None:
        """Push the Settings overlay onto the screen stack."""
        from .settings_screen import SettingsScreen
        self.push_screen(SettingsScreen())

    def action_open_claude_resume_last(self) -> None:
        """Projects tab only — resume the most recent session of the
        cursor project by id (not the interactive picker)."""
        if self._active_tab() != "projects":
            return
        slug = self._cursor_row_key("projects")
        if not slug:
            self.notify("Move the cursor onto a row first", severity="warning")
            return
        path = self._project_path_for_slug(slug)
        if not path:
            self.notify("Project path unknown", severity="warning")
            return
        last_sid = self._last_session_id_in_project(slug)
        if not last_sid:
            self.notify(
                "No previous session recorded for this project",
                severity="warning",
            )
            return
        ok, msg = open_terminal_with(path, ["claude", "--resume", last_sid])
        if ok:
            self.notify(msg, timeout=2)
        else:
            self.notify(msg, severity="error")

    def action_cycle_filter(self, name: str) -> None:
        """Toggle hide_deleted, or cycle date/cost/model values."""
        if name == "hide_deleted":
            self.filter_hide_deleted = not self.filter_hide_deleted
            log.info("filter hide_deleted -> %s", self.filter_hide_deleted)
            return
        cycle = self._FILTER_CYCLES.get(name)
        if cycle is None:
            return
        attr = f"filter_{name}"
        current = getattr(self, attr)
        try:
            idx = cycle.index(current)
        except ValueError:
            idx = -1
        new_value = cycle[(idx + 1) % len(cycle)]
        setattr(self, attr, new_value)
        log.info("filter %s -> %s", name, new_value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-search":
            self.filter_search = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the search box → drop focus back into the active table
        # so navigation continues without an extra Tab keypress.
        if event.input.id != "filter-search":
            return
        try:
            active = self.query_one("#main-tabs", Tabs).active
            self.query_one(f"#t-{active}", DataTable).focus()
        except Exception:
            pass

    def watch_filter_search(self, _old: str, _new: str) -> None:
        self._refresh_view()
        self._update_filter_hint()

    def watch_filter_hide_deleted(self, _old: bool, _new: bool) -> None:
        self._refresh_view()
        self._update_filter_hint()

    def watch_filter_date(self, _old: str, _new: str) -> None:
        self._refresh_view()
        self._update_filter_hint()

    def watch_filter_cost(self, _old: str, _new: str) -> None:
        self._refresh_view()
        self._update_filter_hint()

    def watch_filter_model(self, _old: str, _new: str) -> None:
        self._refresh_view()
        self._update_filter_hint()

    # Filter helpers ---------------------------------------------------

    def _date_filter_cutoff(self) -> datetime | None:
        """Convert filter_date ('all'/'24h'/'7d'/'30d') to a UTC cutoff."""
        now = datetime.now(tz=timezone.utc)
        return {
            "24h": now - timedelta(hours=24),
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
        }.get(self.filter_date)

    def _cost_filter_min(self) -> float | None:
        if self.filter_cost == "all":
            return None
        try:
            return float(self.filter_cost)
        except ValueError:
            return None

    def _model_filter_substr(self) -> str | None:
        return None if self.filter_model == "all" else self.filter_model

    def _session_matches_filters(
        self, s, resolved_path, exists,
        date_cutoff, cost_min, model_substr, search_q,
    ) -> bool:
        if self.filter_hide_deleted and not exists:
            return False
        if date_cutoff is not None:
            ts = s.last_seen
            if ts is None:
                return False
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < date_cutoff:
                return False
        if cost_min is not None and s.sums.cost_usd < cost_min:
            return False
        if model_substr is not None:
            if not any(model_substr in m.lower() for m in s.by_model):
                return False
        if search_q:
            haystacks = [s.session_id.lower()]
            if resolved_path:
                haystacks.append(resolved_path.lower())
            if not any(search_q in h for h in haystacks):
                return False
        return True

    def _project_matches_filters(
        self, slug: str, entry: dict, exists: bool, search_q: str,
    ) -> bool:
        if self.filter_hide_deleted and not exists:
            return False
        date_cutoff = self._date_filter_cutoff()
        if date_cutoff is not None:
            ts = entry.get("last_seen")
            if ts is None:
                return False
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < date_cutoff:
                return False
        cost_min = self._cost_filter_min()
        if cost_min is not None and entry["cost"] < cost_min:
            return False
        # Model filter at project level keeps a project as long as any
        # of its sessions used a model whose id contains the substring.
        model_substr = self._model_filter_substr()
        if model_substr is not None:
            models = entry.get("models") or set()
            if not any(model_substr in m for m in models):
                return False
        if search_q:
            cwd = entry.get("cwd") or ""
            name = (cwd.rsplit("/", 1)[-1] if cwd else slug).lower()
            if search_q not in name and search_q not in cwd.lower():
                return False
        return True

    def _update_filter_hint(self) -> None:
        try:
            ctrl = self.query_one("#filter-controls", Static)
        except Exception:
            return
        hide_marker = "✓" if self.filter_hide_deleted else " "
        ctrl.update(
            f"[b]h[/b] [{hide_marker}] hide missing   "
            f"[b]d[/b] date: {self.filter_date}   "
            f"[b]c[/b] cost: {self._cost_label()}   "
            f"[b]m[/b] model: {self.filter_model}"
        )

    def _cost_label(self) -> str:
        """Pretty-print the cost filter for the hint: '$1K' instead of
        '$1000', etc. Keeps the cycle values numeric and human readable."""
        if self.filter_cost == "all":
            return "all"
        try:
            n = float(self.filter_cost)
        except ValueError:
            return self.filter_cost
        if n >= 1000:
            return f"≥${int(n / 1000)}K"
        return f"≥${self.filter_cost}"


def run_app(
    aggregator: Aggregator,
    tailer: Tailer,
    queue: asyncio.Queue,
    auto_limits: bool = False,
    use_api: bool = True,
) -> None:
    UsageMonitorApp(
        aggregator, tailer, queue,
        auto_limits=auto_limits,
        use_api=use_api,
    ).run()
