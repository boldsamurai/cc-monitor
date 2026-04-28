from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from .aggregator import Aggregator, TokenSums
from .pricing import PricingTable
from .tailer import Tailer


def _fmt_int(n: int) -> str:
    return f"{n:>10,}"


def _fmt_usd(v: float) -> str:
    return f"${v:>8.4f}"


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%H:%M:%S")


def _human(n: int) -> str:
    """Compact integer formatting: 1234 -> 1.2K, 1_234_567 -> 1.23M, etc."""
    if n < 1_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n/1_000:.1f}K"
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
    """Top panel: totals, rate, current 5h block."""

    sums: reactive[TokenSums] = reactive(TokenSums)
    block_sums: reactive[TokenSums] = reactive(TokenSums)
    block_start: reactive[datetime | None] = reactive(None)
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
        for _ in range(7):
            table.add_column(justify="right")

        table.add_row(
            "",
            Text("Input", style="dim"),
            Text("Output", style="dim"),
            Text("Cache R", style="dim"),
            Text("Cache W", style="dim"),
            Text("Cost", style="dim"),
            Text("Turns", style="dim"),
            Text("", style="dim"),
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
            "",
        )

        b = self.block_sums
        block_age = ""
        if self.block_start is not None:
            now = datetime.now(tz=self.block_start.tzinfo or timezone.utc)
            delta = now - self.block_start
            mins = int(delta.total_seconds() // 60)
            block_age = f"started {_fmt_ts(self.block_start)} ({mins}m ago)"
        table.add_row(
            "5h block",
            _human(b.input),
            _human(b.output),
            _human(b.cache_read),
            _human(b.cache_write_5m + b.cache_write_1h),
            _human_usd(b.cost_usd),
            f"{b.turns:,}",
            Text(block_age, style="dim"),
        )

        return Group(header, Text(""), table)


class UsageMonitorApp(App):
    CSS = """
    Screen { layout: vertical; }
    SummaryPanel {
        height: 7;
        padding: 1 2;
        background: $boost;
        border-bottom: solid $primary;
    }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
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
        with TabbedContent(initial="sessions"):
            with TabPane("Sessions [1]", id="sessions"):
                yield DataTable(id="t-sessions", cursor_type="row", zebra_stripes=True)
            with TabPane("Models [2]", id="models"):
                yield DataTable(id="t-models", cursor_type="row", zebra_stripes=True)
            with TabPane("Skills [3]", id="skills"):
                yield DataTable(id="t-skills", cursor_type="row", zebra_stripes=True)
            with TabPane("Agents [4]", id="agents"):
                yield DataTable(id="t-agents", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self.run_worker(self._consume_queue(), exclusive=False)
        self.run_worker(self._tailer_runner(), exclusive=False)
        self.set_interval(0.5, self._refresh_view)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # The newly-shown tab may be stale (we only refresh the active tab on
        # the timer). Force a refresh now so the user sees current numbers.
        self._refresh_view()

    SESSIONS_COLS = [
        ("Session", "sid"),
        ("Project", "proj"),
        ("Last", "last"),
        ("In", "in"),
        ("Out", "out"),
        ("CacheR", "cache_r"),
        ("Sidechain%", "side"),
        ("Cost", "cost"),
        ("Turns", "turns"),
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
        # Cache last-rendered cell tuple per (table_id, row_key) to skip
        # update_cell calls when nothing changed.
        self._row_cache: dict[tuple[str, str], tuple[str, ...]] = {}

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
        summary.rate_tokens = agg.recent_token_rate_per_min()
        summary.rate_cost = agg.recent_cost_per_min()
        summary.rate_turns = agg.recent_turns_per_min()
        summary.block_sums = agg.block_sums
        summary.block_start = agg.block_start
        summary.session_count = len(agg.sessions)
        summary.active_count = agg.active_session_count()
        summary.refresh()

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

    def _diff_update(
        self,
        table_id: str,
        cols: list[tuple[str, str]],
        rows: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        """Apply (row_key, cell_tuple) list to a DataTable without clearing.

        - Adds new rows (in given order, appended at the end)
        - Updates only cells that changed
        - Removes rows no longer present
        - Preserves cursor position and scroll offset
        """
        table = self.query_one(table_id, DataTable)
        col_keys = [k for _, k in cols]
        desired: dict[str, tuple[str, ...]] = dict(rows)

        # Remove rows that are gone.
        existing_keys = [str(rk.value) for rk in list(table.rows.keys())]
        for k in existing_keys:
            if k not in desired:
                try:
                    table.remove_row(k)
                except Exception:
                    pass
                self._row_cache.pop((table_id, k), None)

        # Add or update.
        existing_keys_set = {str(rk.value) for rk in table.rows.keys()}
        for key, cells in rows:
            cache_key = (table_id, key)
            if key not in existing_keys_set:
                table.add_row(*cells, key=key)
                self._row_cache[cache_key] = cells
                continue
            prev = self._row_cache.get(cache_key)
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
            self._row_cache[cache_key] = cells

    def _refresh_sessions_table(self) -> None:
        # Stable order: oldest first by first_seen — keeps rows from jumping
        # around under the cursor when last_seen ticks.
        rows: list[tuple[str, tuple[str, ...]]] = []
        sorted_sessions = sorted(
            self.aggregator.sessions.values(),
            key=lambda s: s.first_seen or datetime.min.replace(tzinfo=timezone.utc),
        )
        for s in sorted_sessions:
            total_main = s.sums_main.total_tokens
            total_side = s.sums_sidechain.total_tokens
            total = total_main + total_side
            side_pct = (total_side / total * 100) if total else 0.0
            cells = (
                s.session_id[:8],
                s.project_slug[-30:] if len(s.project_slug) > 30 else s.project_slug,
                _fmt_ts(s.last_seen),
                _fmt_int(s.sums.input),
                _fmt_int(s.sums.output),
                _fmt_int(s.sums.cache_read),
                f"{side_pct:>5.1f}%",
                _fmt_usd(s.sums.cost_usd),
                str(s.sums.turns),
            )
            rows.append((s.session_id, cells))
        self._diff_update("#t-sessions", self.SESSIONS_COLS, rows)

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
        for model, sums in sorted(per_model.items()):
            cells = (
                model or "(unknown)",
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_int(sums.cache_write_5m + sums.cache_write_1h),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
            )
            rows.append((model or "(unknown)", cells))
        self._diff_update("#t-models", self.MODELS_COLS, rows)

    def _refresh_skills_table(self) -> None:
        rows: list[tuple[str, tuple[str, ...]]] = []
        for name, sums in sorted(self.aggregator.by_skill.items()):
            cells = (
                name,
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
            )
            rows.append((name, cells))
        self._diff_update("#t-skills", self.SKILLS_COLS, rows)

    def _refresh_agents_table(self) -> None:
        rows: list[tuple[str, tuple[str, ...]]] = []
        for name, sums in sorted(self.aggregator.by_agent.items()):
            cells = (
                name,
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
            )
            rows.append((name, cells))
        self._diff_update("#t-agents", self.AGENTS_COLS, rows)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self._refresh_view()


def run_app(aggregator: Aggregator, tailer: Tailer, queue: asyncio.Queue) -> None:
    UsageMonitorApp(aggregator, tailer, queue).run()
