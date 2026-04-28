from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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


class SummaryPanel(Static):
    """Top panel: totals, rate, current 5h block."""

    sums: reactive[TokenSums] = reactive(TokenSums)
    rate: reactive[float] = reactive(0.0)
    block_sums: reactive[TokenSums] = reactive(TokenSums)
    block_start: reactive[datetime | None] = reactive(None)
    session_count: reactive[int] = reactive(0)
    active_count: reactive[int] = reactive(0)

    def render(self) -> str:
        s = self.sums
        b = self.block_sums
        block_age = ""
        if self.block_start is not None:
            now = datetime.now(tz=self.block_start.tzinfo or timezone.utc)
            delta = now - self.block_start
            mins = int(delta.total_seconds() // 60)
            block_age = f"  (started {_fmt_ts(self.block_start)}, {mins}m ago)"

        return (
            f"[b]Sessions:[/b] {self.session_count} "
            f"([b green]{self.active_count} active[/b green], <30m idle)    "
            f"[b]Rate:[/b] {self.rate:,.0f} tok/min  [dim](last 60s ingest)[/dim]\n"
            f"[b]Total (all-time):[/b]  in={_fmt_int(s.input)}  out={_fmt_int(s.output)}  "
            f"cache_r={_fmt_int(s.cache_read)}  cache_w={_fmt_int(s.cache_write_5m + s.cache_write_1h)}  "
            f"cost={_fmt_usd(s.cost_usd)}  turns={s.turns}\n"
            f"[b]5h block:[/b] in={_fmt_int(b.input)}  out={_fmt_int(b.output)}  "
            f"cache_r={_fmt_int(b.cache_read)}  cost={_fmt_usd(b.cost_usd)}{block_age}\n"
            f"[dim]in=input · out=output · cache_r=cache reads · cache_w=cache writes (5m+1h) · "
            f"cost=USD · turns=API calls[/dim]"
        )


class UsageMonitorApp(App):
    CSS = """
    Screen { layout: vertical; }
    SummaryPanel {
        height: 6;
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

    def _setup_tables(self) -> None:
        sessions = self.query_one("#t-sessions", DataTable)
        sessions.add_columns("Session", "Project", "Last", "In", "Out", "CacheR", "Sidechain%", "Cost", "Turns")

        models = self.query_one("#t-models", DataTable)
        models.add_columns("Model", "In", "Out", "CacheR", "CacheW", "Cost", "Turns")

        skills = self.query_one("#t-skills", DataTable)
        skills.add_columns("Skill", "In", "Out", "CacheR", "Cost", "Calls")

        agents = self.query_one("#t-agents", DataTable)
        agents.add_columns("Agent (subagent_type)", "In", "Out", "CacheR", "Cost", "Calls")

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
        summary.rate = agg.recent_token_rate_per_min()
        summary.block_sums = agg.block_sums
        summary.block_start = agg.block_start
        summary.session_count = len(agg.sessions)
        summary.active_count = agg.active_session_count()
        summary.refresh()

        self._refresh_sessions_table()
        self._refresh_models_table()
        self._refresh_skills_table()
        self._refresh_agents_table()

    def _refresh_sessions_table(self) -> None:
        t = self.query_one("#t-sessions", DataTable)
        t.clear()
        rows = sorted(
            self.aggregator.sessions.values(),
            key=lambda s: s.last_seen or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        for s in rows:
            total_main = s.sums_main.total_tokens
            total_side = s.sums_sidechain.total_tokens
            total = total_main + total_side
            side_pct = (total_side / total * 100) if total else 0.0
            t.add_row(
                s.session_id[:8],
                s.project_slug[-30:] if len(s.project_slug) > 30 else s.project_slug,
                _fmt_ts(s.last_seen),
                _fmt_int(s.sums.input),
                _fmt_int(s.sums.output),
                _fmt_int(s.sums.cache_read),
                f"{side_pct:>5.1f}%",
                _fmt_usd(s.sums.cost_usd),
                str(s.sums.turns),
                key=s.session_id,
            )

    def _refresh_models_table(self) -> None:
        t = self.query_one("#t-models", DataTable)
        t.clear()
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
        for model, sums in sorted(per_model.items(), key=lambda kv: -kv[1].cost_usd):
            t.add_row(
                model or "(unknown)",
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_int(sums.cache_write_5m + sums.cache_write_1h),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
                key=model,
            )

    def _refresh_skills_table(self) -> None:
        t = self.query_one("#t-skills", DataTable)
        t.clear()
        for name, sums in sorted(self.aggregator.by_skill.items(), key=lambda kv: -kv[1].cost_usd):
            t.add_row(
                name,
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
                key=name,
            )

    def _refresh_agents_table(self) -> None:
        t = self.query_one("#t-agents", DataTable)
        t.clear()
        for name, sums in sorted(self.aggregator.by_agent.items(), key=lambda kv: -kv[1].cost_usd):
            t.add_row(
                name,
                _fmt_int(sums.input),
                _fmt_int(sums.output),
                _fmt_int(sums.cache_read),
                _fmt_usd(sums.cost_usd),
                str(sums.turns),
                key=name,
            )

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self._refresh_view()


def run_app(aggregator: Aggregator, tailer: Tailer, queue: asyncio.Queue) -> None:
    UsageMonitorApp(aggregator, tailer, queue).run()
