"""Full-screen drill-down view for a single session, pushed when the user
hits Enter on a row in the Sessions tab."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from .aggregator import Aggregator, SessionState, TokenSums
from .project_slug import decode_project_path, decode_project_slug

# Register a fixed plotext theme matching Textual's $panel (#242F38).
# All surface widgets in the detail screen (info panel, charts, tabs,
# footer) are painted with the same $panel color so the whole screen
# reads as one unified panel rather than a patchwork of shades.
_PANEL_RGB: tuple[int, int, int] = (36, 47, 56)  # #242F38
_PLOTEXT_THEME_NAME = "cc-monitor-panel"
try:
    from plotext._dict import themes as _plotext_themes
    if _PLOTEXT_THEME_NAME not in _plotext_themes:
        _plotext_themes[_PLOTEXT_THEME_NAME] = (
            _PANEL_RGB,        # canvas color
            _PANEL_RGB,        # axes color
            (224, 224, 224),   # ticks/foreground
            "default",         # default style
            [                  # data series color cycle (plotext _sequence)
                (0, 130, 200), (60, 180, 75), (230, 25, 75), (255, 225, 25),
                (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230),
            ],
        )
except Exception:
    pass


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_turn_tick(v: int) -> str:
    """Render a turn-axis tick. Forces 'k' suffix above 999 so plotext
    doesn't sneak in its own '×10³' multiplier and confuse the axis."""
    if v >= 1000:
        kv = v / 1000
        return f"{kv:.0f}k" if kv == int(kv) else f"{kv:.1f}k"
    return str(v)


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
        # Digit keys switch chart tabs (mirrors the main view's pattern).
        Binding("1", "show_tab('tab-usage')", "Usage"),
        Binding("2", "show_tab('tab-time')", "Time"),
        Binding("3", "show_tab('tab-turn')", "Turn"),
        Binding("4", "show_tab('tab-dist')", "Distribution"),
        # Copy actions moved to function keys so the digits stay free.
        Binding("f1", "copy_session_id", "Copy session ID"),
        Binding("f2", "copy_project_path", "Copy project path"),
    ]

    CSS = """
    /* Whole screen painted with $panel — one unified surface across the
       info panel, charts, tabs, and footer so nothing looks like a
       sticker glued onto another shade. */
    SessionDetailScreen { background: $panel; }
    #detail-top {
        height: auto;
        padding: 1 2 0 2;
        background: $panel;
    }
    #detail-top > Static {
        width: 1fr;
        padding: 0 2;
    }
    #detail-info { width: 2fr; }
    .chart-plot {
        height: 14;
        margin: 1 2;
        background: $panel;
    }
    #charts-tabs {
        height: auto;
        background: $panel;
    }
    #charts-tabs Tabs {
        background: $panel;
    }
    #charts-tabs TabPane {
        padding: 0;
        background: $panel;
    }
    #usage-table, #files-table {
        height: auto;
        max-height: 20;
        background: $panel;
    }
    .usage-hint {
        padding: 1 2;
        color: $text-muted;
    }
    .usage-section-heading {
        padding: 1 2 0 2;
        text-style: bold underline;
    }
    #section-skills, #section-agents {
        padding: 0 2;
    }
    #detail-footer {
        height: 1;
        dock: bottom;
        background: $panel;
        color: $text;
    }
    #footer-left {
        width: auto;
        padding: 0 1;
    }
    #footer-right {
        width: 1fr;
        padding: 0 1;
        text-align: right;
    }
    """

    def __init__(self, session_id: str, aggregator: Aggregator):
        super().__init__()
        self.session_id = session_id
        self.aggregator = aggregator

    def _make_plot(self, plot_id: str) -> PlotextPlot:
        p = PlotextPlot(classes="chart-plot")
        p.id = plot_id
        p.theme = _PLOTEXT_THEME_NAME
        return p

    def compose(self) -> ComposeResult:
        sess = self.aggregator.sessions.get(self.session_id)
        # Always re-read the JSONL from disk for charts. The 8-day rolling
        # archive only holds the fresh tail of long sessions (e.g. a session
        # with 4958 assistant turns spread over 2 weeks shows up as ~12
        # records in the archive), so trusting it here would silently
        # truncate the charts.
        turns = (
            self.aggregator.load_full_session_turns(self.session_id)
            if sess
            else []
        )

        with VerticalScroll():
            # Top row: Session info, Totals, By model — three columns
            # side by side so the screen feels like a dashboard rather
            # than a long scroll of stacked sections.
            with Horizontal(id="detail-top"):
                yield Static(self._build_info_block(sess), id="detail-info")
                yield Static(self._build_totals_block(sess), id="detail-totals")
                yield Static(self._build_models_block(sess), id="detail-models")

            if turns:
                # Charts grouped by what their x-axis depends on. All plots
                # use the hand-registered theme so canvas matches widget bg.
                with TabbedContent(id="charts-tabs"):
                    with TabPane("Usage [1]", id="tab-usage"):
                        yield Static(
                            "Skill / Agent invocations",
                            classes="usage-section-heading",
                        )
                        usage_table = DataTable(
                            id="usage-table", cursor_type="row"
                        )
                        usage_table.add_columns(
                            "Time", "Type", "Name",
                            "Duration", "Tokens", "Cost",
                            "% session", "% 5h",
                        )
                        yield usage_table
                        yield Static(
                            "",
                            id="usage-empty",
                            classes="usage-hint",
                        )
                        yield Static(
                            "Files read",
                            classes="usage-section-heading",
                        )
                        files_table = DataTable(
                            id="files-table", cursor_type="row"
                        )
                        files_table.add_columns(
                            "File", "Reads", "Tokens (~est)",
                        )
                        yield files_table
                        yield Static(
                            "",
                            id="files-empty",
                            classes="usage-hint",
                        )
                    with TabPane("Time [2]", id="tab-time"):
                        yield self._make_plot("chart-context-time")
                        yield self._make_plot("chart-cost-time")
                    with TabPane("Turn [3]", id="tab-turn"):
                        yield self._make_plot("chart-context")
                        yield self._make_plot("chart-cost")
                    with TabPane("Distribution [4]", id="tab-dist"):
                        yield self._make_plot("chart-hist")
                        yield self._make_plot("chart-gap")
            else:
                yield Static(
                    Text(
                        "Charts unavailable — session JSONL not found on disk.",
                        style="dim italic",
                    )
                )

            # Skills/agents now live in the Usage tab above — no
            # separate sections at the bottom.

        with Horizontal(id="detail-footer"):
            yield Static(
                "[b]1[/b] Usage   [b]2[/b] Time   [b]3[/b] Turn"
                "   [b]4[/b] Distribution",
                id="footer-left",
            )
            yield Static(
                "[b]Esc[/b] back   "
                "[b]F1[/b] copy session ID   [b]F2[/b] copy project path",
                id="footer-right",
            )

    def on_mount(self) -> None:
        sess = self.aggregator.sessions.get(self.session_id)
        if sess is None:
            return
        self._populate_usage_table(sess)
        self._populate_files_table()
        turns = self.aggregator.load_full_session_turns(self.session_id)
        if not turns:
            return
        self._populate_charts(turns, sess)

    def _populate_charts(
        self,
        turns: list[tuple[datetime, "object", float]],
        sess: SessionState,
    ) -> None:
        # The y-axis on context charts is a percentage of the model's
        # context window. _context_limit_for handles the 1M-variant
        # case where the API reports a 200K model id but the session
        # actually exceeded 200K tokens.
        from .tui import _context_limit_for
        ctx_limit = _context_limit_for(
            sess.last_context_model, sess.max_context_tokens
        )

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
            ctx_series.append(ctx / ctx_limit * 100.0)  # % of context window
            cost_series.append(cost)
            token_series.append((ctx + rec.output_tokens) / 1000.0)

        n = len(turns)
        x_turns = list(range(1, n + 1))
        # Pre-format turn-axis labels with 'k' suffix above 999. plotext
        # otherwise picks float ticks for small ranges and silently applies
        # a '×10³' multiplier for large ones — both confusing on a turn
        # counter. NOTE: canvas/axes colors are intentionally left to
        # textual-plotext's auto theme — it resets them on every render
        # anyway, so calling canvas_color() here would be no-op.
        tick_positions = sorted(
            {1, max(1, n // 4), max(1, n // 2), max(1, 3 * n // 4), n}
        )
        tick_labels = [_fmt_turn_tick(p) for p in tick_positions]

        # Context %-of-window line chart.
        ctx_plot = self.query_one("#chart-context", PlotextPlot)
        p = ctx_plot.plt
        p.clear_data()
        p.plot(x_turns, ctx_series, marker="braille", color="cyan")
        p.title("Context % per turn")
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
        p.plot(x_turns, cumulative, marker="braille", color="green")
        p.title("Cumulative cost ($) over turns")
        p.xlabel("turn")
        p.xticks(tick_positions, tick_labels)

        # ---- Time-axis charts (Tab "Time") ----
        # Use seconds-since-first-turn as the numeric x value. Absolute
        # epoch would also work but produces ugly tick numbers; relative
        # seconds with formatted labels reads better.
        first_ts = turns[0][0]
        if first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)

        def _to_secs(ts: datetime) -> float:
            t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            return (t - first_ts).total_seconds()

        times_secs = [_to_secs(ts) for ts, _, _ in turns]
        total_secs = times_secs[-1] if times_secs else 0.0
        time_tick_secs = sorted(
            {0.0, total_secs / 4, total_secs / 2, 3 * total_secs / 4, total_secs}
        )

        def _fmt_time_tick(secs: float) -> str:
            ts_local = (first_ts + timedelta(seconds=secs)).astimezone()
            if total_secs > 86400 * 2:
                return ts_local.strftime("%d-%m %H:%M")
            if total_secs > 3600 * 2:
                return ts_local.strftime("%H:%M")
            return ts_local.strftime("%H:%M:%S")

        time_tick_labels = [_fmt_time_tick(s) for s in time_tick_secs]

        # Context %-of-window over time.
        p = self.query_one("#chart-context-time", PlotextPlot).plt
        p.clear_data()
        p.plot(times_secs, ctx_series, marker="braille", color="cyan")
        p.title("Context % over time")
        p.xlabel("time")
        p.xticks(time_tick_secs, time_tick_labels)

        # Cumulative cost over time.
        p = self.query_one("#chart-cost-time", PlotextPlot).plt
        p.clear_data()
        p.plot(times_secs, cumulative, marker="braille", color="green")
        p.title("Cumulative cost over time ($)")
        p.xlabel("time")
        p.xticks(time_tick_secs, time_tick_labels)

        # ---- Distribution tab ----
        # Tokens-per-turn distribution (histogram). The x axis here is
        # the SIZE of a single turn in K tokens — NOT a turn counter; the
        # rightmost bucket holds the largest turn observed in the session.
        # The y axis is the count of turns landing in that bucket.
        hist_plot = self.query_one("#chart-hist", PlotextPlot)
        p = hist_plot.plt
        p.clear_data()
        p.hist(token_series, bins=20, color="orange")
        p.title("Turn-size distribution (how many turns by token count)")
        p.xlabel("Turn size (K tokens)")
        # Force integer x-ticks. plotext's auto-tick prefers float values
        # like '362.1' which look like turn numbers and confuse readers.
        if token_series:
            top = max(token_series)
            step = max(1, int(round(top / 5 / 50) * 50))  # round to nice 50K
            xt = list(range(0, int(top) + step, step))
            p.xticks(xt, [str(v) for v in xt])

        # Time-between-turns histogram. Gaps capped at 10 minutes so the
        # active-conversation tail (most gaps are seconds-to-minutes)
        # stays readable instead of being squashed by the rare hours-long
        # break between sessions on different days.
        _GAP_CAP_MIN = 10.0
        gaps_min: list[float] = []
        prev_ts = None
        for ts, _rec, _cost in turns:
            t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if prev_ts is not None:
                g = (t - prev_ts).total_seconds() / 60.0
                gaps_min.append(min(g, _GAP_CAP_MIN))
            prev_ts = t
        gap_plot = self.query_one("#chart-gap", PlotextPlot)
        p = gap_plot.plt
        p.clear_data()
        if gaps_min:
            p.hist(gaps_min, bins=20, color="magenta")
        p.title("Gap between turns (minutes, ≥10 collapsed to 10)")
        p.xlabel("minutes")

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            # No tabs yet (e.g. session has no turns -> charts skipped).
            pass

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
        sub.append(f"{_fmt_duration(sess.first_seen, sess.last_seen)}\n")
        sub.append("Tools:    ", style="dim")
        sub.append(f"{self._tools_summary(sess)}\n")
        sub.append("Top reads:", style="dim")
        sub.append(f" {self._top_reads_summary(sess)}")

        return Group(title, Text(""), sub)

    def _top_reads_summary(self, sess: SessionState) -> str:
        """Top 3 files by read count, basename only ('foo.py (12)')."""
        from pathlib import Path as _Path
        files = self.aggregator.count_file_reads_in_session(self.session_id)
        if not files:
            return "(no Read tool calls)"
        top = sorted(files.items(), key=lambda kv: -kv[1]["reads"])[:3]
        return " · ".join(
            f"{_Path(fp).name} ({stats['reads']})" for fp, stats in top
        )

    def _tools_summary(self, sess: SessionState) -> str:
        """Top 3-5 tools used in the session as 'Bash 47% · Edit 17% · …'."""
        counts = self.aggregator.count_tools_in_session(self.session_id)
        if not counts:
            return "(no tool calls recorded)"
        total = sum(counts.values())
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        parts = [f"{name} {n / total * 100:.0f}%" for name, n in top]
        # If more than 5 tools were used, hint how much was lumped together.
        leftover = total - sum(n for _, n in top)
        if leftover > 0:
            parts.append(f"… +{leftover}")
        return " · ".join(parts) + f"  ({total} calls)"

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

    def _populate_usage_table(self, sess: SessionState) -> None:
        """Fill the Usage tab DataTable with one row per Skill/Agent span.

        Each row carries timestamp, duration, token count, USD cost, % of
        session cost, and % of derived 5h budget (from API utilization).
        """
        try:
            table = self.query_one("#usage-table", DataTable)
            empty = self.query_one("#usage-empty", Static)
        except Exception:
            return

        spans = list(self.aggregator.spans_by_session.get(self.session_id, ()))
        if not spans:
            empty.update(
                "No skill/agent usage recorded for this session.\n"
                "This data comes from the Claude Code hook script — sessions "
                "that predate the hook setup, or that never invoked Skill or "
                "Agent tools, stay empty here."
            )
            return

        empty.update("")
        budget_5h = self._derive_5h_budget()
        session_cost = sess.sums.cost_usd or 0.0

        # Sort by start time desc — most recent invocation first.
        spans.sort(key=lambda s: s.started_at, reverse=True)
        for span in spans:
            # Match the DD-MM-YYYY HH:MM:SS format used everywhere else
            # in the app (info block, main session table) so the column
            # reads consistently.
            ts_str = _fmt_dt(span.started_at)
            duration = self._fmt_span_duration(span)
            tokens = _fmt_int(span.sums.total_tokens)
            cost = f"${span.sums.cost_usd:.4f}"
            pct_session = (
                f"{span.sums.cost_usd / session_cost * 100:.2f}%"
                if session_cost > 0
                else "—"
            )
            pct_5h = (
                f"{span.sums.cost_usd / budget_5h * 100:.2f}%"
                if budget_5h is not None and budget_5h > 0
                else "—"
            )
            table.add_row(
                ts_str,
                span.tool,
                span.name or "(?)",
                duration,
                tokens,
                cost,
                pct_session,
                pct_5h,
            )

    def _populate_files_table(self) -> None:
        """Fill the Files-read DataTable with one row per unique file path
        Read'd in the session, sorted by estimated tokens desc."""
        try:
            table = self.query_one("#files-table", DataTable)
            empty = self.query_one("#files-empty", Static)
        except Exception:
            return

        files = self.aggregator.count_file_reads_in_session(self.session_id)
        if not files:
            empty.update(
                "No file reads recorded for this session — either the "
                "session never used the Read tool or the JSONL file is gone."
            )
            return

        empty.update("")
        # Order: biggest tokens first — that's the actionable signal
        # ('which file is bloating my context the most').
        ordered = sorted(
            files.items(), key=lambda kv: -kv[1]["tokens_est"]
        )
        for fp, stats in ordered:
            tokens = stats["tokens_est"]
            tokens_str = (
                f"{tokens / 1000:.1f}K" if tokens >= 1000 else str(tokens)
            )
            table.add_row(fp, str(stats["reads"]), tokens_str)

    def _fmt_span_duration(self, span) -> str:
        if span.duration_ms is not None:
            ms = span.duration_ms
        elif span.ended_at is not None:
            ms = int((span.ended_at - span.started_at).total_seconds() * 1000)
        else:
            return "—"
        if ms < 1000:
            return f"{ms}ms"
        if ms < 60_000:
            return f"{ms / 1000:.1f}s"
        return f"{ms // 60_000}m {(ms % 60_000) // 1000}s"

    def _derive_5h_budget(self) -> float | None:
        """Reverse-engineer the user's 5h dollar budget from API utilization.

        Anthropic doesn't publish exact plan caps, but it returns
        utilization % of the active 5h block. Pairing that with our
        block-local cost gives a derived ceiling: cap = local / (util/100).
        Fall back to the static plan limit on the aggregator if either
        side is missing or zero.
        """
        api = self.aggregator.api_usage
        block = self.aggregator.block_info()
        if (
            api is not None
            and api.five_hour is not None
            and block is not None
            and api.five_hour.utilization > 0
            and block.sums.cost_usd > 0
        ):
            return block.sums.cost_usd / (api.five_hour.utilization / 100.0)
        return self.aggregator.cost_limit  # may itself be None

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
