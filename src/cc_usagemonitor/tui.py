from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Header, Static, TabbedContent, TabPane

from .aggregator import Aggregator, BlockInfo, TokenSums
from .config import load_config, save_config
from .pricing import PricingTable
from .project_slug import decode_project_slug
from .tailer import Tailer


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_usd(v: float) -> str:
    return f"${v:.4f}"


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%H:%M:%S")


def _fmt_datetime(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%d-%m-%Y %H:%M")


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


def _human_usd(v: float) -> str:
    if v < 10:
        return f"${v:.4f}"
    if v < 1_000:
        return f"${v:,.2f}"
    return f"${v:,.0f}"


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
    """Live status of the current 5-hour Anthropic session block."""

    info: reactive[BlockInfo | None] = reactive(None, layout=True)

    BAR_W = 30

    def render(self) -> RenderableType:
        info = self.info
        if info is None:
            return Group(
                Text("⏱  5h block", style="bold"),
                Text("No active block (no records in the last 5h)", style="dim italic"),
            )

        start_local = info.start.astimezone()
        end_local = info.end.astimezone()
        elapsed_str = _fmt_minutes(info.minutes_elapsed)
        remaining_str = _fmt_minutes(info.minutes_remaining)

        header = Text.from_markup(
            f"[b]⏱  5h block[/b]   "
            f"started [b]{start_local.strftime('%H:%M')}[/b]  ·  "
            f"ends [b]{end_local.strftime('%H:%M')}[/b]  ·  "
            f"elapsed [b]{elapsed_str}[/b]  ·  "
            f"remaining [b]{remaining_str}[/b]"
        )

        lines: list[Text] = [header]

        # Tokens line
        sums = info.sums
        if info.token_limit:
            lines.append(self._progress_line("Tokens", info.pct_tokens or 0.0,
                f"{_human(sums.total_tokens)} / {_human(info.token_limit)}"))
        else:
            lines.append(Text.from_markup(
                f"[b]Tokens[/b]   {_human(sums.total_tokens)}  "
                f"[dim](no limit set; pass --plan or --max-5h-tokens)[/dim]"
            ))

        # Cost line
        if info.cost_limit:
            lines.append(self._progress_line("Cost  ", info.pct_cost or 0.0,
                f"${sums.cost_usd:,.2f} / ${info.cost_limit:,.2f}"))
        else:
            lines.append(Text.from_markup(
                f"[b]Cost  [/b]   ${sums.cost_usd:,.2f}  "
                f"[dim](no limit set; pass --plan or --max-5h-cost)[/dim]"
            ))

        # Burn + ETA
        burn_line = Text.from_markup(
            f"[b]Burn[/b]    {_human(int(info.burn_tokens_per_min))} tok/min  ·  "
            f"${info.burn_cost_per_min:,.2f}/min"
        )
        lines.append(burn_line)

        eta_parts = []
        if info.eta_to_token_limit_min is not None:
            t_label, t_color = self._eta_verdict(info.eta_to_token_limit_min, info.minutes_remaining)
            eta_parts.append(
                f"tokens limit in [{t_color}]{_fmt_minutes(info.eta_to_token_limit_min)}[/{t_color}] {t_label}"
            )
        if info.eta_to_cost_limit_min is not None:
            c_label, c_color = self._eta_verdict(info.eta_to_cost_limit_min, info.minutes_remaining)
            eta_parts.append(
                f"cost limit in [{c_color}]{_fmt_minutes(info.eta_to_cost_limit_min)}[/{c_color}] {c_label}"
            )
        if eta_parts:
            lines.append(Text.from_markup("[b]ETA[/b]     " + "  ·  ".join(eta_parts)))

        return Group(*lines)

    def _progress_line(self, label: str, pct: float, suffix: str) -> Text:
        pct = max(0.0, pct)
        if pct < 80:
            color = "green"
        elif pct < 95:
            color = "yellow"
        else:
            color = "red"
        filled = min(int(round(pct / 100 * self.BAR_W)), self.BAR_W)
        bar = "█" * filled + "·" * (self.BAR_W - filled)
        line = Text()
        line.append_text(Text.from_markup(f"[b]{label}[/b]  "))
        line.append(bar, style=color)
        line.append(f"  {suffix}  ({pct:.1f}%)")
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


class BarChart(Static):
    """Horizontal bar chart with one row per item.

    items: list of (label, value, suffix) tuples. Bars are scaled to the
    largest value in the list. Suffix is a free-form string shown to the
    right of the bar (e.g. '$4,197  38%').
    """

    title: reactive[str] = reactive("", layout=True)
    items: reactive[list[tuple[str, float, str]]] = reactive(list, layout=True)

    BAR_CHAR = "█"
    EMPTY_CHAR = "·"
    LABEL_W = 24
    SUFFIX_W = 18

    def render(self) -> RenderableType:
        title = Text(self.title, style="bold")
        if not self.items:
            return Group(title, Text("No data yet", style="dim italic"))

        max_val = max((v for _, v, _ in self.items), default=0.0) or 1.0
        total_w = self.size.width or 60
        # Reserve space for borders/padding handled by CSS.
        bar_w = max(8, total_w - self.LABEL_W - self.SUFFIX_W - 2)

        lines: list[Text] = [title]
        for label, value, suffix in self.items:
            ratio = max(0.0, value / max_val)
            n = int(round(ratio * bar_w))
            bar_str = self.BAR_CHAR * n + self.EMPTY_CHAR * (bar_w - n)
            label_str = label[: self.LABEL_W - 1].ljust(self.LABEL_W)
            line = Text()
            line.append(label_str)
            line.append(bar_str, style="cyan")
            line.append(" " + suffix.rjust(self.SUFFIX_W - 1))
            lines.append(line)
        return Group(*lines)


class UsageMonitorApp(App):
    CSS = """
    Screen { layout: vertical; }
    SummaryPanel {
        height: 6;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $primary;
    }
    BlockPanel {
        height: 6;
        padding: 0 2;
        background: $boost;
        border-bottom: solid $primary;
    }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #t-models { height: 1fr; }
    #models-charts { height: 50%; }
    #models-charts > BarChart {
        width: 1fr;
        padding: 1 1;
        border: round $primary;
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
        Binding("2", "show_tab('models')", "Models"),
        Binding("3", "show_tab('skills')", "Skills"),
        Binding("4", "show_tab('agents')", "Agents"),
    ]

    def __init__(self, aggregator: Aggregator, tailer: Tailer, queue: asyncio.Queue):
        super().__init__()
        self.aggregator = aggregator
        self.tailer = tailer
        self.queue = queue

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryPanel(id="summary")
        yield BlockPanel(id="block")
        with TabbedContent(initial="sessions"):
            with TabPane("Sessions [1]", id="sessions"):
                yield DataTable(id="t-sessions", cursor_type="row", zebra_stripes=True)
            with TabPane("Models [2]", id="models"):
                yield DataTable(id="t-models", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="models-charts"):
                    yield BarChart(id="chart-cost")
                    yield BarChart(id="chart-cache")
            with TabPane("Skills [3]", id="skills"):
                yield DataTable(id="t-skills", cursor_type="row", zebra_stripes=True)
            with TabPane("Agents [4]", id="agents"):
                yield DataTable(id="t-agents", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="status-bar"):
            yield Static(
                "[b]1[/b] Sessions  [b]2[/b] Models  [b]3[/b] Skills  [b]4[/b] Agents",
                id="status-left",
            )
            yield Static("[b]r[/b] Refresh  [b]q[/b] Quit", id="status-right")

    def on_mount(self) -> None:
        cfg = load_config()
        saved_theme = cfg.get("theme")
        if saved_theme:
            try:
                self.theme = saved_theme
            except Exception:
                pass
        self.watch(self, "theme", self._on_theme_change)

        self._setup_tables()
        self.run_worker(self._consume_queue(), exclusive=False)
        self.run_worker(self._tailer_runner(), exclusive=False)
        self.set_interval(0.5, self._refresh_view)

    def _on_theme_change(self, new_theme: str) -> None:
        cfg = load_config()
        cfg["theme"] = new_theme
        save_config(cfg)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # The newly-shown tab may be stale (we only refresh the active tab on
        # the timer). Force a refresh now so the user sees current numbers.
        self._refresh_view()
        # Move keyboard focus into the table of the newly-activated tab so
        # arrow keys keep working — without this, switching with 1-4 leaves
        # focus on the previous (now hidden) table and the user has to Tab.
        active = event.tabbed_content.active
        try:
            self.query_one(f"#t-{active}", DataTable).focus()
        except Exception:
            pass

    SESSIONS_COLS = [
        ("Session", "sid"),
        ("Project", "proj"),
        ("Last", "last"),
        ("Duration", "dur"),
        ("Cost", "cost"),
        ("Turns", "turns"),
        ("$/turn", "per_turn"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("CacheW", "cache_w"),
        ("Cache%", "cache_pct"),
    ]
    MODELS_COLS = [
        ("Model", "model"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("CacheW", "cache_w"),
        ("Cost", "cost"),
        ("Turns", "turns"),
    ]
    SKILLS_COLS = [
        ("Skill", "skill"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("Cost", "cost"),
        ("Calls", "calls"),
    ]
    AGENTS_COLS = [
        ("Agent (subagent_type)", "agent"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("Cost", "cost"),
        ("Calls", "calls"),
    ]

    def _setup_tables(self) -> None:
        for table_id, cols in (
            ("#t-sessions", self.SESSIONS_COLS),
            ("#t-models", self.MODELS_COLS),
            ("#t-skills", self.SKILLS_COLS),
            ("#t-agents", self.AGENTS_COLS),
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
            "#t-skills": {},
            "#t-agents": {},
        }

    async def _tailer_runner(self) -> None:
        try:
            await self.tailer.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.notify(f"Tailer error: {e}", severity="error")

    async def _consume_queue(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                self.aggregator.ingest(item)
            except Exception as e:
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
        block_panel.refresh()

        # Refresh only the visible tab — keeps the UI responsive even at
        # hundreds of rows. The other tables are still in-sync from previous
        # refreshes; they'll catch up the moment the user switches tabs.
        active = self.query_one(TabbedContent).active
        if active == "sessions":
            self._refresh_sessions_table()
        elif active == "models":
            self._refresh_models_table()
        elif active == "skills":
            self._refresh_skills_table()
        elif active == "agents":
            self._refresh_agents_table()

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
        # Sort by last_seen DESC (newest activity on top). The active session
        # naturally stays pinned to row 0 — its last_seen ticks but it's
        # already the largest, so the order doesn't change and cursor doesn't
        # jump. Reorder only happens when ranking truly shifts (new session,
        # different session becomes most-recent); _apply_rows preserves the
        # cursor's row_key across that rebuild.
        rows: list[tuple[str, tuple[str, ...]]] = []
        sorted_sessions = sorted(
            self.aggregator.sessions.values(),
            key=lambda s: s.last_seen or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        for s in sorted_sessions:
            total_in = (
                s.sums.cache_read
                + s.sums.input
                + s.sums.cache_write_5m
                + s.sums.cache_write_1h
            )
            cache_pct = (s.sums.cache_read / total_in * 100) if total_in else 0.0
            per_turn = (s.sums.cost_usd / s.sums.turns) if s.sums.turns else 0.0
            project_name = decode_project_slug(s.project_slug)
            cells = (
                s.session_id[:8],
                project_name[-30:] if len(project_name) > 30 else project_name,
                _fmt_datetime(s.last_seen),
                _fmt_duration(s.first_seen, s.last_seen),
                _fmt_usd(s.sums.cost_usd),
                _human(s.sums.turns),
                f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}",
                _human(s.sums.input),
                _human(s.sums.output),
                _human(s.sums.cache_read),
                _human(s.sums.cache_write_5m + s.sums.cache_write_1h),
                f"{cache_pct:.1f}%",
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
            cells = (
                model or "(unknown)",
                _human(sums.input),
                _human(sums.output),
                _human(sums.cache_read),
                _human(sums.cache_write_5m + sums.cache_write_1h),
                _fmt_usd(sums.cost_usd),
                _human(sums.turns),
            )
            rows.append((model or "(unknown)", cells))
        self._apply_rows("#t-models", self.MODELS_COLS, rows)
        self._refresh_models_charts(per_model)

    def _refresh_models_charts(self, per_model: dict[str, TokenSums]) -> None:
        total_cost = sum(s.cost_usd for s in per_model.values()) or 1.0

        cost_items: list[tuple[str, float, str]] = []
        cache_items: list[tuple[str, float, str]] = []
        for model, sums in per_model.items():
            label = model or "(unknown)"
            pct_cost = sums.cost_usd / total_cost * 100
            cost_suffix = f"${sums.cost_usd:,.0f} ({pct_cost:.0f}%)"
            cost_items.append((label, sums.cost_usd, cost_suffix))

            input_total = (
                sums.input
                + sums.cache_read
                + sums.cache_write_5m
                + sums.cache_write_1h
            )
            cache_pct = (
                sums.cache_read / input_total * 100 if input_total else 0.0
            )
            cache_items.append((label, cache_pct, f"{cache_pct:.1f}%"))

        cost_items.sort(key=lambda t: -t[1])
        cache_items.sort(key=lambda t: -t[1])

        try:
            chart_cost = self.query_one("#chart-cost", BarChart)
            chart_cost.title = "Cost share"
            chart_cost.items = cost_items
            chart_cache = self.query_one("#chart-cache", BarChart)
            chart_cache.title = "Cache hit %"
            chart_cache.items = cache_items
        except Exception:
            pass

    def _refresh_skills_table(self) -> None:
        rows: list[tuple[str, tuple[str, ...]]] = []
        for name, sums in sorted(self.aggregator.by_skill.items()):
            cells = (
                name,
                _human(sums.input),
                _human(sums.output),
                _human(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                _human(sums.turns),
            )
            rows.append((name, cells))
        self._apply_rows("#t-skills", self.SKILLS_COLS, rows)

    def _refresh_agents_table(self) -> None:
        rows: list[tuple[str, tuple[str, ...]]] = []
        for name, sums in sorted(self.aggregator.by_agent.items()):
            cells = (
                name,
                _human(sums.input),
                _human(sums.output),
                _human(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                _human(sums.turns),
            )
            rows.append((name, cells))
        self._apply_rows("#t-agents", self.AGENTS_COLS, rows)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self._refresh_view()


def run_app(aggregator: Aggregator, tailer: Tailer, queue: asyncio.Queue) -> None:
    UsageMonitorApp(aggregator, tailer, queue).run()
