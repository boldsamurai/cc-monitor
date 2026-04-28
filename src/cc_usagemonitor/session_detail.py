"""Full-screen drill-down view for a single session, pushed when the user
hits Enter on a row in the Sessions tab."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static
from textual_plotext import PlotextPlot

from .aggregator import Aggregator, SessionState, TokenSums
from .project_slug import decode_project_path, decode_project_slug


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_dt(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%d-%m-%Y %H:%M:%S")


def _fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "-"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    s = int((end - start).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


class SessionDetailScreen(Screen):
    """Drill-down view for one session. Esc/q to close."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        # Override the App-level 'q'=quit while we're in the detail
        # screen so users coming from the main view don't accidentally
        # exit the program with muscle memory from the table view.
        Binding("q", "app.pop_screen", "Back"),
        Binding("1", "copy_session_id", "Copy session ID"),
        Binding("2", "copy_project_path", "Copy project path"),
    ]

    CSS = """
    SessionDetailScreen { background: $background; }
    #detail-top {
        height: auto;
        padding: 1 2 0 2;
        background: $boost;
    }
    #detail-top > Static {
        width: 1fr;
        padding: 0 2;
    }
    #detail-info { width: 2fr; }
    .chart-plot {
        height: 14;
        margin: 1 2;
        background: $boost;
    }
    #section-skills, #section-agents {
        padding: 0 2;
    }
    #detail-footer {
        height: 1;
        dock: bottom;
        padding: 0 1;
        background: $panel;
        color: $text;
        content-align: left middle;
    }
    """

    def __init__(self, session_id: str, aggregator: Aggregator):
        super().__init__()
        self.session_id = session_id
        self.aggregator = aggregator

    def compose(self) -> ComposeResult:
        sess = self.aggregator.sessions.get(self.session_id)
        # Fast path: the 8-day rolling archive. If the session is older,
        # re-read its JSONL from disk so the user always gets charts.
        turns = (
            self.aggregator.turns_for_session(self.session_id)
            if sess
            else []
        )
        if sess and not turns:
            turns = self.aggregator.load_full_session_turns(self.session_id)

        with VerticalScroll():
            # Top row: Session info, Totals, By model — three columns
            # side by side so the screen feels like a dashboard rather
            # than a long scroll of stacked sections.
            with Horizontal(id="detail-top"):
                yield Static(self._build_info_block(sess), id="detail-info")
                yield Static(self._build_totals_block(sess), id="detail-totals")
                yield Static(self._build_models_block(sess), id="detail-models")

            if turns:
                ctx_plot = PlotextPlot(classes="chart-plot")
                ctx_plot.id = "chart-context"
                yield ctx_plot
                cost_plot = PlotextPlot(classes="chart-plot")
                cost_plot.id = "chart-cost"
                yield cost_plot
                hist_plot = PlotextPlot(classes="chart-plot")
                hist_plot.id = "chart-hist"
                yield hist_plot
            else:
                yield Static(
                    Text(
                        "Charts unavailable — session JSONL not found on disk.",
                        style="dim italic",
                    )
                )

            # Skills / agents (full width, only when relevant).
            if sess and sess.skills:
                yield Static(
                    Group(
                        Text("Skills used in this session", style="bold underline"),
                        self._skills_table(sess),
                    ),
                    id="section-skills",
                )
            if sess and sess.agents:
                yield Static(
                    Group(
                        Text("Agents used in this session", style="bold underline"),
                        self._agents_table(sess),
                    ),
                    id="section-agents",
                )

        yield Static(
            "[b]Esc[/b] back   ·   "
            "[b]1[/b] copy session ID   ·   [b]2[/b] copy project path",
            id="detail-footer",
        )

    def on_mount(self) -> None:
        sess = self.aggregator.sessions.get(self.session_id)
        if sess is None:
            return
        turns = self.aggregator.turns_for_session(self.session_id)
        if not turns:
            turns = self.aggregator.load_full_session_turns(self.session_id)
        if not turns:
            return
        self._populate_charts(turns)

    def _populate_charts(
        self, turns: list[tuple[datetime, "object", float]]
    ) -> None:
        ctx_series: list[float] = []
        cost_series: list[float] = []
        token_series: list[float] = []
        for _ts, rec, cost in turns:
            ctx = (
                rec.input_tokens
                + rec.cache_read_tokens
                + rec.cache_write_5m_tokens
                + rec.cache_write_1h_tokens
            )
            ctx_series.append(ctx / 1000.0)  # K tokens
            cost_series.append(cost)
            token_series.append((ctx + rec.output_tokens) / 1000.0)

        n = len(turns)
        x_turns = list(range(1, n + 1))
        # Force integer x-ticks for both line charts (the default float
        # ticks like '156.2' make no sense for a turn counter).
        tick_positions = sorted(
            {1, max(1, n // 4), max(1, n // 2), max(1, 3 * n // 4), n}
        )
        tick_labels = [str(p) for p in tick_positions]

        # Pick a canvas color that matches Textual's $boost surface so the
        # plot doesn't look detached from the rest of the panel. Falls
        # back to a sensible Catppuccin-Mocha-like dark slate if the
        # active theme doesn't expose 'boost'.
        try:
            boost = self.app.theme_variables.get("boost", "#181825")
        except Exception:
            boost = "#181825"

        def _style(p) -> None:
            p.theme("clear")
            try:
                p.canvas_color(boost)
                p.axes_color(boost)
            except Exception:
                pass

        # Context size line chart.
        ctx_plot = self.query_one("#chart-context", PlotextPlot)
        p = ctx_plot.plt
        p.clear_data()
        _style(p)
        p.plot(x_turns, ctx_series, marker="braille", color="cyan")
        p.title("Context size per turn (K tokens)")
        p.xlabel("turn")
        p.xticks(tick_positions, tick_labels)

        # Cumulative cost line chart.
        cumulative: list[float] = []
        running = 0.0
        for c in cost_series:
            running += c
            cumulative.append(running)
        cost_plot = self.query_one("#chart-cost", PlotextPlot)
        p = cost_plot.plt
        p.clear_data()
        _style(p)
        p.plot(x_turns, cumulative, marker="braille", color="green")
        p.title("Cumulative cost ($) over turns")
        p.xlabel("turn")
        p.xticks(tick_positions, tick_labels)

        # Tokens per turn histogram. Title now spells out what bars mean.
        hist_plot = self.query_one("#chart-hist", PlotextPlot)
        p = hist_plot.plt
        p.clear_data()
        _style(p)
        p.hist(token_series, bins=20, color="orange")
        p.title("How many turns landed in each token-size bucket")
        p.xlabel("K tokens per turn")

    def action_copy_session_id(self) -> None:
        try:
            self.app.copy_to_clipboard(self.session_id)
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error")
            return
        self.app.notify(f"Copied {self.session_id}", timeout=2)

    def action_copy_project_path(self) -> None:
        sess = self.aggregator.sessions.get(self.session_id)
        path = decode_project_path(sess.project_slug) if sess else None
        if not path:
            self.app.notify("Project path unknown", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(path)
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error")
            return
        self.app.notify(f"Copied {path}", timeout=2)

    def _build_info_block(self, sess: SessionState | None) -> RenderableType:
        if sess is None:
            return Text(f"Session {self.session_id} not found", style="bold red")

        project_name = decode_project_slug(sess.project_slug)
        project_path = decode_project_path(sess.project_slug) or "(not found on disk)"

        title = Text()
        title.append("ID: ", style="bold")
        title.append(self.session_id, style="bold cyan")

        sub = Text()
        sub.append("Project:  ", style="dim")
        sub.append(f"{project_name}\n")
        sub.append("Path:     ", style="dim")
        sub.append(f"{project_path}\n")
        sub.append("First:    ", style="dim")
        sub.append(f"{_fmt_dt(sess.first_seen)}\n")
        sub.append("Last:     ", style="dim")
        sub.append(f"{_fmt_dt(sess.last_seen)}\n")
        sub.append("Duration: ", style="dim")
        sub.append(_fmt_duration(sess.first_seen, sess.last_seen))

        return Group(title, Text(""), sub)

    def _build_totals_block(self, sess: SessionState | None) -> RenderableType:
        if sess is None:
            return Text("")
        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_row("Cost", f"${sess.sums.cost_usd:,.4f}")
        stats.add_row("Turns", _fmt_int(sess.sums.turns))
        per_turn = (sess.sums.cost_usd / sess.sums.turns) if sess.sums.turns else 0
        per_turn_str = f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}"
        stats.add_row("$/turn", per_turn_str)
        stats.add_row("", "")
        stats.add_row("Input", _fmt_int(sess.sums.input))
        stats.add_row("Output", _fmt_int(sess.sums.output))
        stats.add_row("Cache R", _fmt_int(sess.sums.cache_read))
        stats.add_row(
            "Cache W",
            _fmt_int(sess.sums.cache_write_5m + sess.sums.cache_write_1h),
        )
        stats.add_row("Total", _fmt_int(sess.sums.total_tokens))

        total_in = (
            sess.sums.input
            + sess.sums.cache_read
            + sess.sums.cache_write_5m
            + sess.sums.cache_write_1h
        )
        cache_pct = (sess.sums.cache_read / total_in * 100) if total_in else 0
        stats.add_row("Cache hit %", f"{cache_pct:.1f}%")
        stats.add_row("", "")

        from .tui import _context_limit_for
        ctx_limit = _context_limit_for(sess.last_context_model, sess.max_context_tokens)
        stats.add_row(
            "Ctx (last)",
            f"{_fmt_int(sess.last_context_tokens)} ({sess.last_context_tokens/ctx_limit*100:.1f}%)",
        )
        stats.add_row(
            "Ctx (peak)",
            f"{_fmt_int(sess.max_context_tokens)} ({sess.max_context_tokens/ctx_limit*100:.1f}%)",
        )

        return Group(Text("Totals", style="bold underline"), Text(""), stats)

    def _build_models_block(self, sess: SessionState | None) -> RenderableType:
        if sess is None:
            return Text("")
        return Group(
            Text("By model", style="bold underline"),
            Text(""),
            self._model_table(sess),
        )

    def _model_table(self, sess: SessionState) -> Table:
        t = Table(show_header=True, header_style="bold dim")
        t.add_column("Model")
        t.add_column("Turns", justify="right")
        t.add_column("Cost", justify="right")
        t.add_column("Total tokens", justify="right")
        for model, sums in sorted(sess.by_model.items(), key=lambda kv: -kv[1].turns):
            t.add_row(
                model or "(unknown)",
                _fmt_int(sums.turns),
                f"${sums.cost_usd:.4f}",
                _fmt_int(sums.total_tokens),
            )
        return t

    def _skills_table(self, sess: SessionState) -> Table | None:
        if not sess.skills:
            return None
        t = Table(show_header=True, header_style="bold dim")
        t.add_column("Skill")
        t.add_column("Calls", justify="right")
        t.add_column("Cost", justify="right")
        t.add_column("Tokens", justify="right")
        for name, sums in sorted(sess.skills.items(), key=lambda kv: -kv[1].cost_usd):
            t.add_row(
                name,
                _fmt_int(sums.turns),
                f"${sums.cost_usd:.4f}",
                _fmt_int(sums.total_tokens),
            )
        return t

    def _agents_table(self, sess: SessionState) -> Table | None:
        if not sess.agents:
            return None
        t = Table(show_header=True, header_style="bold dim")
        t.add_column("Agent")
        t.add_column("Calls", justify="right")
        t.add_column("Cost", justify="right")
        t.add_column("Tokens", justify="right")
        for name, sums in sorted(sess.agents.items(), key=lambda kv: -kv[1].cost_usd):
            t.add_row(
                name,
                _fmt_int(sums.turns),
                f"${sums.cost_usd:.4f}",
                _fmt_int(sums.total_tokens),
            )
        return t
