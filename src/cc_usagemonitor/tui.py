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
from textual.widgets import DataTable, Header, Static, TabbedContent, TabPane

from .aggregator import Aggregator, TokenSums
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
            table.add_column(justify="left")

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
        with TabbedContent(initial="sessions"):
            with TabPane("Sessions [1]", id="sessions"):
                yield DataTable(id="t-sessions", cursor_type="row", zebra_stripes=True)
            with TabPane("Models [2]", id="models"):
                yield DataTable(id="t-models", cursor_type="row", zebra_stripes=True)
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
                str(s.sums.turns),
                f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}",
                _fmt_int(s.sums.input),
                _fmt_int(s.sums.output),
                _fmt_int(s.sums.cache_read),
                _fmt_int(s.sums.cache_write_5m + s.sums.cache_write_1h),
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
        self._apply_rows("#t-models", self.MODELS_COLS, rows)

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
        self._apply_rows("#t-skills", self.SKILLS_COLS, rows)

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
        self._apply_rows("#t-agents", self.AGENTS_COLS, rows)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self._refresh_view()


def run_app(aggregator: Aggregator, tailer: Tailer, queue: asyncio.Queue) -> None:
    UsageMonitorApp(aggregator, tailer, queue).run()
