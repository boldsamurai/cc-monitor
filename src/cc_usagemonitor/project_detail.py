"""Drill-down view for a single project, pushed from the Projects tab.

Aggregates every session whose project_slug matches across all stats:
total cost / tokens, top tools and files, sessions list, charts of
activity over time.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from .aggregator import Aggregator, SessionState, TokenSums
from .parser import humanize_model_name
from .launchers import open_terminal_with
from .project_slug import decode_project_path
from .session_detail import (
    SessionDetailScreen,
    _PLOTEXT_THEME_NAME,
    _fmt_duration,
    _fmt_dt,
    _fmt_int,
    _fmt_turn_tick,
)


class ProjectDetailScreen(Screen):
    """Drill-down for a single project. Esc/q to close."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
        Binding("1", "show_tab('tab-sessions')", "Sessions"),
        Binding("2", "show_tab('tab-usage')", "Usage"),
        Binding("3", "show_tab('tab-activity')", "Activity"),
        # Letters rather than F-keys (F1-Fn aren't reliable on laptops
        # or remote terminals). Open actions first, copies after.
        Binding("o", "open_explorer", "Open in file manager"),
        Binding("n", "open_new_claude", "New Claude Code session"),
        Binding("s", "open_resume_last", "Resume last session"),
        Binding("p", "copy_path", "Copy project path"),
        Binding("i", "copy_session_id", "Copy session ID"),
        Binding("f1", "open_explorer", "Open in file manager", show=False),
        Binding("f2", "open_new_claude", "New Claude Code session", show=False),
        Binding("f3", "open_resume_last", "Resume last session", show=False),
        Binding("f4", "copy_path", "Copy project path", show=False),
        Binding("f5", "copy_session_id", "Copy session ID", show=False),
    ]

    CSS = """
    ProjectDetailScreen { background: $panel; }
    #pd-top {
        height: auto;
        padding: 1 2 0 2;
        background: $panel;
    }
    #pd-top > Static {
        width: 1fr;
        padding: 0 2;
    }
    #pd-info { width: 2fr; }
    .pd-chart {
        height: 14;
        margin: 1 2;
        background: $panel;
    }
    #pd-tabs {
        height: 1fr;
        background: $panel;
    }
    #pd-tabs Tabs { background: $panel; }
    #pd-tabs TabPane { padding: 0; background: $panel; }
    #pd-sessions-table { height: auto; max-height: 25; background: $panel; }
    #pd-usage-row { height: 1fr; }
    .pd-col-spans { width: 1fr; height: 1fr; margin-right: 2; }
    .pd-col-files { width: 1fr; height: 1fr; }
    #pd-spans-table {
        height: auto;
        max-height: 25;
        background: $panel;
    }
    /* Files tables fill their section (1fr) so DataTable handles row
       overflow with its own internal scroll — auto+max-height would
       let the second table push below the visible column. */
    #pd-files-table, #pd-files-write-table {
        height: 1fr;
        background: $panel;
    }
    /* Two stacked sections inside .pd-col-files (Files read / Files
       written) — fixed 50/50 split. Trades a bit of dead space when
       one table has few rows for predictable overflow handling. */
    .pd-files-section { height: 1fr; }
    .pd-section-heading {
        padding: 1 2 0 2;
        text-style: bold underline;
    }
    /* Empty hint collapses to 0 rows when its content is "" so it
       doesn't insert extra padding between Files read and Files
       written when both have data. */
    .pd-hint {
        height: auto;
        padding: 0 2;
        color: $text-muted;
    }
    #pd-footer {
        height: 1;
        dock: bottom;
        background: $panel;
        color: $text;
    }
    #pd-footer-left { width: auto; padding: 0 1; }
    #pd-footer-right { width: 1fr; padding: 0 1; text-align: right; }
    """

    def __init__(self, project_slug: str, aggregator: Aggregator):
        super().__init__()
        self.project_slug = project_slug
        self.aggregator = aggregator
        # Capture sessions belonging to this project upfront — keeps later
        # iterations cheap and the result stable even if new sessions
        # arrive while the screen is open.
        self._sessions: list[SessionState] = sorted(
            (
                s for s in aggregator.sessions.values()
                if s.project_slug == project_slug
            ),
            key=lambda s: s.last_seen or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    # ----- compose -----

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="pd-top"):
                yield Static(self._build_info_block(), id="pd-info")
                yield Static(self._build_totals_block(), id="pd-totals")
                yield Static(self._build_models_block(), id="pd-models")

            with TabbedContent(id="pd-tabs"):
                with TabPane("Sessions [1]", id="tab-sessions"):
                    yield self._make_sessions_table()
                with TabPane("Usage [2]", id="tab-usage"):
                    with Horizontal(id="pd-usage-row"):
                        with Vertical(classes="pd-col-spans"):
                            yield Static(
                                "Skill / Agent invocations (across sessions)",
                                classes="pd-section-heading",
                            )
                            spans_table = DataTable(
                                id="pd-spans-table", cursor_type="row"
                            )
                            spans_table.add_columns(
                                "Type", "Name", "Calls", "Tokens", "Cost",
                            )
                            yield spans_table
                            yield Static("", id="pd-spans-empty", classes="pd-hint")
                        with Vertical(classes="pd-col-files"):
                            with Vertical(classes="pd-files-section"):
                                yield Static(
                                    "Files read (across sessions)",
                                    classes="pd-section-heading",
                                )
                                files_table = DataTable(
                                    id="pd-files-table", cursor_type="row"
                                )
                                files_table.add_columns(
                                    "File", "Reads", "Tokens (~est)",
                                )
                                yield files_table
                                yield Static("", id="pd-files-empty", classes="pd-hint")
                            with Vertical(classes="pd-files-section"):
                                yield Static(
                                    "Files written (across sessions)",
                                    classes="pd-section-heading",
                                )
                                files_write_table = DataTable(
                                    id="pd-files-write-table", cursor_type="row"
                                )
                                files_write_table.add_columns(
                                    "File", "Writes", "Edits", "Tokens (~est)",
                                )
                                yield files_write_table
                                yield Static(
                                    "",
                                    id="pd-files-write-empty",
                                    classes="pd-hint",
                                )
                with TabPane("Activity [3]", id="tab-activity"):
                    with VerticalScroll():
                        yield self._make_plot("pd-chart-cost")
                        yield self._make_plot("pd-chart-tokens")

        with Horizontal(id="pd-footer"):
            yield Static("", id="pd-footer-left")
            # Footer text is rebuilt in _update_footer based on the
            # active tab (5 only meaningful on Sessions). Initial value
            # matches the default Sessions tab.
            yield Static("", id="pd-footer-right")

    # ----- helpers used in compose -----

    def _make_plot(self, plot_id: str) -> PlotextPlot:
        p = PlotextPlot(classes="pd-chart")
        p.id = plot_id
        p.theme = _PLOTEXT_THEME_NAME
        return p

    def _make_sessions_table(self) -> DataTable:
        t = DataTable(id="pd-sessions-table", cursor_type="row", zebra_stripes=True)
        t.add_columns(
            "Session", "Last", "Duration", "Cost", "Turns", "$/turn", "Tokens",
        )
        return t

    # ----- on_mount: populate dynamic data -----

    def on_mount(self) -> None:
        self._populate_sessions_table()
        self._populate_charts()
        self._populate_usage_tables()
        # Sessions table is the natural starting point — auto-focus
        # so Enter drills further without needing Tab first.
        try:
            self.query_one("#pd-sessions-table", DataTable).focus()
        except Exception:
            pass

    def _populate_sessions_table(self) -> None:
        try:
            t = self.query_one("#pd-sessions-table", DataTable)
        except Exception:
            return
        for sess in self._sessions:
            duration = _fmt_duration(sess.first_seen, sess.last_seen)
            per_turn = (
                sess.sums.cost_usd / sess.sums.turns if sess.sums.turns else 0
            )
            per_turn_str = (
                f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}"
            )
            t.add_row(
                sess.session_id[:8],
                _fmt_dt(sess.last_seen),
                duration,
                f"${sess.sums.cost_usd:.4f}",
                _fmt_int(sess.sums.turns),
                per_turn_str,
                _fmt_int(sess.sums.total_tokens),
                key=sess.session_id,  # full id as key for drill-in
            )

    def _populate_charts(self) -> None:
        # Cumulative cost + cumulative tokens, both indexed by chronological
        # turn order across ALL sessions in the project. Quickest signal:
        # 'where in time did this project burn money'.
        records: list[tuple[datetime, float, int]] = []  # (ts, cost, tokens)
        for sess in self._sessions:
            for ts, rec, cost in self.aggregator.load_full_session_turns(
                sess.session_id
            ):
                records.append((ts, cost, rec.input_tokens + rec.output_tokens
                                + rec.cache_read_tokens
                                + rec.cache_write_5m_tokens
                                + rec.cache_write_1h_tokens))
        if not records:
            return
        records.sort(key=lambda r: r[0])
        first_ts = records[0][0]

        def _to_secs(ts: datetime) -> float:
            t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            ft = (
                first_ts if first_ts.tzinfo else first_ts.replace(tzinfo=timezone.utc)
            )
            return (t - ft).total_seconds()

        times = [_to_secs(r[0]) for r in records]
        cum_cost: list[float] = []
        cum_tokens: list[float] = []
        running_cost = 0.0
        running_tok = 0
        for _, c, t_count in records:
            running_cost += c
            running_tok += t_count
            cum_cost.append(running_cost)
            cum_tokens.append(running_tok / 1000)  # K tokens

        total_secs = times[-1] if times else 0.0
        tick_secs = sorted(
            {0.0, total_secs / 4, total_secs / 2, 3 * total_secs / 4, total_secs}
        )

        def _fmt_time_tick(secs: float) -> str:
            from datetime import timedelta as _td
            ft = first_ts if first_ts.tzinfo else first_ts.replace(tzinfo=timezone.utc)
            ts_local = (ft + _td(seconds=secs)).astimezone()
            if total_secs > 86400 * 7:
                return ts_local.strftime("%d-%m-%Y")
            if total_secs > 86400 * 2:
                return ts_local.strftime("%d-%m %H:%M")
            return ts_local.strftime("%H:%M")

        labels = [_fmt_time_tick(s) for s in tick_secs]

        try:
            p = self.query_one("#pd-chart-cost", PlotextPlot).plt
            p.clear_data()
            p.plot(times, cum_cost, marker="braille", color="green")
            p.title("Cumulative cost over time ($)")
            p.xlabel("time")
            p.xticks(tick_secs, labels)

            p = self.query_one("#pd-chart-tokens", PlotextPlot).plt
            p.clear_data()
            p.plot(times, cum_tokens, marker="braille", color="cyan")
            p.title("Cumulative tokens over time (K)")
            p.xlabel("time")
            p.xticks(tick_secs, labels)
        except Exception:
            pass

    def _populate_usage_tables(self) -> None:
        # Skills + Agents — sum TokenSums across all sessions in this
        # project. Each row is one (Type, Name) pair.
        try:
            spans_table = self.query_one("#pd-spans-table", DataTable)
            spans_empty = self.query_one("#pd-spans-empty", Static)
            files_table = self.query_one("#pd-files-table", DataTable)
            files_empty = self.query_one("#pd-files-empty", Static)
            files_write_table = self.query_one(
                "#pd-files-write-table", DataTable
            )
            files_write_empty = self.query_one(
                "#pd-files-write-empty", Static
            )
        except Exception:
            return

        skills_agg: dict[str, TokenSums] = defaultdict(TokenSums)
        agents_agg: dict[str, TokenSums] = defaultdict(TokenSums)
        for sess in self._sessions:
            for name, sums in sess.skills.items():
                acc = skills_agg[name]
                acc.input += sums.input
                acc.output += sums.output
                acc.cache_read += sums.cache_read
                acc.cache_write_5m += sums.cache_write_5m
                acc.cache_write_1h += sums.cache_write_1h
                acc.cost_usd += sums.cost_usd
                acc.turns += sums.turns
            for name, sums in sess.agents.items():
                acc = agents_agg[name]
                acc.input += sums.input
                acc.output += sums.output
                acc.cache_read += sums.cache_read
                acc.cache_write_5m += sums.cache_write_5m
                acc.cache_write_1h += sums.cache_write_1h
                acc.cost_usd += sums.cost_usd
                acc.turns += sums.turns

        rows = []
        for name, sums in skills_agg.items():
            rows.append(("Skill", name, sums))
        for name, sums in agents_agg.items():
            rows.append(("Agent", name, sums))
        rows.sort(key=lambda r: -r[2].cost_usd)

        if not rows:
            spans_empty.update(
                "No Skill/Agent invocations recorded across these sessions.\n"
                "Hook events are required — older sessions stay empty."
            )
        else:
            for type_, name, sums in rows:
                spans_table.add_row(
                    type_,
                    name,
                    _fmt_int(sums.turns),
                    _fmt_int(sums.total_tokens),
                    f"${sums.cost_usd:.4f}",
                )

        # Files: sum reads+chars+tokens across all sessions, then sort
        # by tokens_est desc.
        files_agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"reads": 0, "chars": 0, "tokens_est": 0}
        )
        for sess in self._sessions:
            for fp, stats in self.aggregator.count_file_reads_in_session(
                sess.session_id
            ).items():
                bucket = files_agg[fp]
                bucket["reads"] += stats["reads"]
                bucket["chars"] += stats["chars"]
                bucket["tokens_est"] += stats["tokens_est"]

        if not files_agg:
            files_empty.update(
                "No Read tool calls recorded across these sessions."
            )
        else:
            project_root = self._project_path()
            ordered = sorted(
                files_agg.items(), key=lambda kv: -kv[1]["tokens_est"]
            )
            for fp, stats in ordered:
                display = fp
                if project_root and fp.startswith(project_root + "/"):
                    display = fp[len(project_root) + 1:]
                if len(display) > 60:
                    display = "…" + display[-59:]
                tokens = stats["tokens_est"]
                tokens_str = (
                    f"{tokens / 1000:.1f}K" if tokens >= 1000 else str(tokens)
                )
                files_table.add_row(display, str(stats["reads"]), tokens_str)

        # Files written: sum writes/edits/chars across all sessions,
        # sort by total mutations desc.
        writes_agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"writes": 0, "edits": 0, "chars": 0, "tokens_est": 0}
        )
        for sess in self._sessions:
            for fp, stats in self.aggregator.count_file_writes_in_session(
                sess.session_id
            ).items():
                bucket = writes_agg[fp]
                bucket["writes"] += stats["writes"]
                bucket["edits"] += stats["edits"]
                bucket["chars"] += stats["chars"]
                bucket["tokens_est"] += stats["tokens_est"]

        if not writes_agg:
            files_write_empty.update(
                "No Write/Edit tool calls recorded across these sessions."
            )
        else:
            project_root = self._project_path()
            ordered = sorted(
                writes_agg.items(),
                key=lambda kv: -(kv[1]["writes"] + kv[1]["edits"]),
            )
            for fp, stats in ordered:
                display = fp
                if project_root and fp.startswith(project_root + "/"):
                    display = fp[len(project_root) + 1:]
                if len(display) > 60:
                    display = "…" + display[-59:]
                tokens = stats["tokens_est"]
                tokens_str = (
                    f"{tokens / 1000:.1f}K" if tokens >= 1000 else str(tokens)
                )
                files_write_table.add_row(
                    display,
                    str(stats["writes"]),
                    str(stats["edits"]),
                    tokens_str,
                )

    # ----- info-block builders -----

    def _project_path(self) -> str | None:
        for sess in self._sessions:
            if sess.cwd:
                return sess.cwd
        return decode_project_path(self.project_slug)

    def _build_info_block(self) -> RenderableType:
        path = self._project_path() or "(not found on disk)"
        exists = bool(path and Path(path).is_dir())
        name = path.rsplit("/", 1)[-1] if path != "(not found on disk)" else "?"
        first = min(
            (s.first_seen for s in self._sessions if s.first_seen),
            default=None,
        )
        last = max(
            (s.last_seen for s in self._sessions if s.last_seen),
            default=None,
        )

        title = Text()
        title.append("Project: ", style="bold")
        title.append(name, style="bold cyan")
        title.append("  " + ("✓" if exists else "✗ deleted"),
                     style="green" if exists else "red dim")

        sub = Text()
        sub.append("Path:     ", style="dim")
        sub.append(f"{path}\n")
        sub.append("Sessions: ", style="dim")
        sub.append(f"{_fmt_int(len(self._sessions))}\n")
        sub.append("First:    ", style="dim")
        sub.append(f"{_fmt_dt(first)}\n")
        sub.append("Last:     ", style="dim")
        sub.append(f"{_fmt_dt(last)}\n")
        sub.append("Span:     ", style="dim")
        sub.append(f"{_fmt_duration(first, last)}\n")
        sub.append("Tools:    ", style="dim")
        sub.append(self._tools_summary())
        sub.append("\nTop reads:", style="dim")
        sub.append(f" {self._top_reads_summary()}")

        return Group(title, Text(""), sub)

    def _tools_summary(self) -> str:
        counts: dict[str, int] = defaultdict(int)
        for sess in self._sessions:
            for name, n in self.aggregator.count_tools_in_session(
                sess.session_id
            ).items():
                counts[name] += n
        if not counts:
            return "(no tool calls recorded)"
        total = sum(counts.values())
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        parts = [f"{n} {v / total * 100:.0f}%" for n, v in top]
        return " · ".join(parts) + f"  ({total} calls)"

    def _top_reads_summary(self) -> str:
        agg: dict[str, int] = defaultdict(int)
        for sess in self._sessions:
            for fp, stats in self.aggregator.count_file_reads_in_session(
                sess.session_id
            ).items():
                agg[fp] += stats["reads"]
        if not agg:
            return "(no Read tool calls)"
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:3]
        return " · ".join(
            f"{Path(fp).name} ({n})" for fp, n in top
        )

    def _build_totals_block(self) -> RenderableType:
        total_cost = sum(s.sums.cost_usd for s in self._sessions)
        total_turns = sum(s.sums.turns for s in self._sessions)
        total_tokens = sum(s.sums.total_tokens for s in self._sessions)
        per_session = (
            total_cost / len(self._sessions) if self._sessions else 0.0
        )
        per_turn = total_cost / total_turns if total_turns else 0.0
        cache_read = sum(s.sums.cache_read for s in self._sessions)
        cache_write = sum(
            s.sums.cache_write_5m + s.sums.cache_write_1h
            for s in self._sessions
        )
        input_total = sum(s.sums.input for s in self._sessions) + cache_read + cache_write
        cache_pct = cache_read / input_total * 100 if input_total else 0.0

        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_row("Cost", f"${total_cost:,.4f}")
        stats.add_row(
            "$/session",
            f"${per_session:.2f}" if per_session >= 1 else f"${per_session:.4f}",
        )
        stats.add_row(
            "$/turn",
            f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}",
        )
        stats.add_row("", "")
        stats.add_row("Turns", _fmt_int(total_turns))
        stats.add_row("Total tokens", _fmt_int(total_tokens))
        stats.add_row("Cache hit %", f"{cache_pct:.1f}%")
        return Group(Text("Totals", style="bold underline"), Text(""), stats)

    def _build_models_block(self) -> RenderableType:
        model_sums: dict[str, TokenSums] = defaultdict(TokenSums)
        for sess in self._sessions:
            for model, sums in sess.by_model.items():
                acc = model_sums[model]
                acc.input += sums.input
                acc.output += sums.output
                acc.cache_read += sums.cache_read
                acc.cache_write_5m += sums.cache_write_5m
                acc.cache_write_1h += sums.cache_write_1h
                acc.cost_usd += sums.cost_usd
                acc.turns += sums.turns
        t = Table(show_header=True, header_style="bold dim")
        t.add_column("Model")
        t.add_column("Turns", justify="right")
        t.add_column("Cost", justify="right")
        for model, sums in sorted(
            model_sums.items(), key=lambda kv: -kv[1].cost_usd
        ):
            t.add_row(
                humanize_model_name(model) or "(unknown)",
                _fmt_int(sums.turns),
                f"${sums.cost_usd:.4f}",
            )
        return Group(Text("By model", style="bold underline"), Text(""), t)

    # ----- actions -----

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    def action_copy_path(self) -> None:
        path = self._project_path()
        if not path:
            self.app.notify("Project path unknown", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(path)
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error")
            return
        self.app.notify(f"Copied {path}", timeout=2)

    def action_copy_session_id(self) -> None:
        """Copy the session_id of the cursor row in the Sessions table.
        Only fires when the Sessions tab is active — bails with a
        no-op notify in any other tab so the keybinding doesn't quietly
        copy something stale."""
        try:
            tabs = self.query_one(TabbedContent)
        except Exception:
            return
        if tabs.active != "tab-sessions":
            self.app.notify(
                "Copy session ID only works on the Sessions tab",
                severity="warning",
            )
            return
        try:
            table = self.query_one("#pd-sessions-table", DataTable)
        except Exception:
            return
        keys = list(table.rows.keys())
        idx = table.cursor_row
        if not (0 <= idx < len(keys)):
            self.app.notify(
                "Move the cursor onto a session row first",
                severity="warning",
            )
            return
        session_id = str(keys[idx].value)
        try:
            self.app.copy_to_clipboard(session_id)
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error")
            return
        self.app.notify(f"Copied {session_id}", timeout=2)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # Show F3 hint only while the Sessions tab is in front — keeps
        # the footer honest about what the keybinding actually does.
        self._update_footer()
        self._focus_table_for_tab()

    def _focus_table_for_tab(self) -> None:
        """Auto-focus the primary DataTable in tabs that contain one,
        so Enter/arrows operate on table rows immediately instead of
        sitting on the tab bar after switch."""
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return
        target = {
            "tab-sessions": "#pd-sessions-table",
            "tab-usage": "#pd-spans-table",
        }.get(active)
        if not target:
            return
        try:
            self.query_one(target, DataTable).focus()
        except Exception:
            pass

    def _update_footer(self) -> None:
        try:
            tabs = self.query_one(TabbedContent)
            left = self.query_one("#pd-footer-left", Static)
            right = self.query_one("#pd-footer-right", Static)
        except Exception:
            return
        actions = (
            "[b]o[/b] open dir   "
            "[b]n[/b] new claude   "
            "[b]s[/b] resume last   "
            "[b]p[/b] copy path"
        )
        if tabs.active == "tab-sessions":
            # Enter drills into the highlighted session's detail screen.
            # 'i' (copy session ID) is also session-specific, so both are
            # gated on the Sessions tab.
            actions = (
                "[b]↵[/b] details   "
                + actions
                + "   [b]i[/b] copy session ID"
            )
        left.update(actions)
        right.update(
            "[b]Tab[/b] / [b]shift+Tab[/b] focus   [b]esc[/b] back"
        )

    def _last_session_id(self) -> str | None:
        latest = None
        latest_ts = None
        for sess in self._sessions:
            if sess.last_seen is None:
                continue
            ts = sess.last_seen
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest = sess.session_id
        return latest

    def action_open_new_claude(self) -> None:
        path = self._project_path()
        if not path:
            self.app.notify("Project path unknown", severity="warning")
            return
        ok, msg = open_terminal_with(path, ["claude"])
        self.app.notify(msg, severity="information" if ok else "error")

    def action_open_resume_last(self) -> None:
        path = self._project_path()
        if not path:
            self.app.notify("Project path unknown", severity="warning")
            return
        last_sid = self._last_session_id()
        if not last_sid:
            self.app.notify(
                "No previous session recorded for this project",
                severity="warning",
            )
            return
        ok, msg = open_terminal_with(path, ["claude", "--resume", last_sid])
        self.app.notify(msg, severity="information" if ok else "error")

    def action_open_explorer(self) -> None:
        """Open the project directory in the OS file manager. Uses
        xdg-open on Linux, 'open' on macOS, 'explorer' on Windows.
        Path-only — never executes anything from the project."""
        path = self._project_path()
        if not path:
            self.app.notify("Project path unknown", severity="warning")
            return
        if not Path(path).is_dir():
            self.app.notify(
                f"Path no longer exists: {path}", severity="warning"
            )
            return
        import sys
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.app.notify(f"Open failed: {e}", severity="error")
            return
        self.app.notify(f"Opened {path}", timeout=2)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a session row drills further into per-session detail.
        if event.data_table.id != "pd-sessions-table":
            return
        if event.row_key is None or event.row_key.value is None:
            return
        self.app.push_screen(
            SessionDetailScreen(str(event.row_key.value), self.aggregator)
        )
