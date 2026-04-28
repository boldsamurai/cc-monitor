"""Full-screen drill-down view for a single session, pushed when the user
hits Enter on a row in the Sessions tab."""

from __future__ import annotations

from datetime import datetime, timezone

import plotext as plt
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from .aggregator import Aggregator, SessionState, TokenSums
from .project_slug import decode_project_path, decode_project_slug


_SPARKLINE_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 40) -> str:
    """Render a list of numbers as a one-line sparkline.

    Resamples to `width` buckets so output is consistent regardless of
    series length. Empty input returns an empty string.
    """
    if not values:
        return ""
    n = len(values)
    if n > width:
        bucket = n / width
        sampled = [
            max(values[int(i * bucket): max(int(i * bucket) + 1, int((i + 1) * bucket))])
            for i in range(width)
        ]
    else:
        sampled = values
    lo = min(sampled)
    hi = max(sampled)
    if hi == lo:
        return _SPARKLINE_BLOCKS[len(_SPARKLINE_BLOCKS) // 2] * len(sampled)
    out = []
    levels = len(_SPARKLINE_BLOCKS) - 1
    for v in sampled:
        idx = int(round((v - lo) / (hi - lo) * levels))
        out.append(_SPARKLINE_BLOCKS[idx])
    return "".join(out)


def _plotext_to_str(width: int, height: int) -> str:
    """Render the current plotext figure to a plain string and clear state."""
    plt.plotsize(width, height)
    rendered = plt.build()
    plt.clear_figure()
    return rendered


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
        Binding("1", "copy_session_id", "Copy session ID"),
        Binding("2", "copy_project_path", "Copy project path"),
    ]

    CSS = """
    SessionDetailScreen { background: $background; }
    #detail-body {
        padding: 1 2;
        background: $boost;
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
        with VerticalScroll():
            yield Static(self._build_content(), id="detail-body")
        yield Static(
            "[b]Esc[/b] back   ·   "
            "[b]1[/b] copy session ID   ·   [b]2[/b] copy project path",
            id="detail-footer",
        )

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

    def _build_content(self) -> RenderableType:
        sess = self.aggregator.sessions.get(self.session_id)
        if sess is None:
            return Text(f"Session {self.session_id} not found", style="bold red")

        # Header.
        project_name = decode_project_slug(sess.project_slug)
        project_path = decode_project_path(sess.project_slug) or "(not found on disk)"
        title = Text()
        title.append("Session ", style="bold")
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

        # Top stats table.
        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_row("Cost", f"${sess.sums.cost_usd:,.4f}")
        stats.add_row("Turns", _fmt_int(sess.sums.turns))
        per_turn = (sess.sums.cost_usd / sess.sums.turns) if sess.sums.turns else 0
        per_turn_str = f"${per_turn:.4f}" if per_turn < 1 else f"${per_turn:.2f}"
        stats.add_row("$/turn", per_turn_str)
        stats.add_row("", "")
        stats.add_row("Input tokens", _fmt_int(sess.sums.input))
        stats.add_row("Output tokens", _fmt_int(sess.sums.output))
        stats.add_row("Cache reads", _fmt_int(sess.sums.cache_read))
        stats.add_row(
            "Cache writes",
            f"{_fmt_int(sess.sums.cache_write_5m + sess.sums.cache_write_1h)}  "
            f"(5m: {_fmt_int(sess.sums.cache_write_5m)}, "
            f"1h: {_fmt_int(sess.sums.cache_write_1h)})",
        )
        stats.add_row("Total tokens", _fmt_int(sess.sums.total_tokens))

        # Cache hit ratio.
        total_in = (
            sess.sums.input
            + sess.sums.cache_read
            + sess.sums.cache_write_5m
            + sess.sums.cache_write_1h
        )
        cache_pct = (sess.sums.cache_read / total_in * 100) if total_in else 0
        stats.add_row("Cache hit %", f"{cache_pct:.1f}%")

        # Context info.
        from .tui import _context_limit_for
        ctx_limit = _context_limit_for(sess.last_context_model, sess.max_context_tokens)
        stats.add_row("", "")
        stats.add_row(
            "Context (last)",
            f"{_fmt_int(sess.last_context_tokens)} / {_fmt_int(ctx_limit)} "
            f"({sess.last_context_tokens/ctx_limit*100:.1f}%)",
        )
        stats.add_row(
            "Context (peak)",
            f"{_fmt_int(sess.max_context_tokens)} / {_fmt_int(ctx_limit)} "
            f"({sess.max_context_tokens/ctx_limit*100:.1f}%)",
        )

        # Per-model + per-skill / agent.
        model_table = self._model_table(sess)
        skills_table = self._skills_table(sess)
        agents_table = self._agents_table(sess)

        # Per-turn data drives the charts. Older sessions whose records
        # have already aged out of the 8-day archive will have no data
        # here; we surface a hint instead of an empty chart.
        turns = self.aggregator.turns_for_session(self.session_id)
        charts_block: list[RenderableType] = []
        if turns:
            charts_block = self._build_charts(turns)
        else:
            charts_block = [
                Text("Charts unavailable — turn-level data only kept for 8 days.",
                     style="dim italic"),
            ]

        return Group(
            title,
            Text(""),
            sub,
            Text(""),
            Text("Totals", style="bold underline"),
            stats,
            Text(""),
            Text("Charts", style="bold underline"),
            *charts_block,
            Text(""),
            Text("By model", style="bold underline"),
            model_table,
            *([Text(""), Text("Skills used in this session", style="bold underline"), skills_table]
              if skills_table else []),
            *([Text(""), Text("Agents used in this session", style="bold underline"), agents_table]
              if agents_table else []),
        )

    def _build_charts(
        self, turns: list[tuple[datetime, "object", float]]
    ) -> list[RenderableType]:
        """Three charts: context-size sparkline, cumulative cost line chart,
        tokens-per-turn histogram."""
        # Per-turn series.
        ctx_series: list[float] = []
        cost_series: list[float] = []
        token_series: list[int] = []
        for _ts, rec, cost in turns:
            ctx = (
                rec.input_tokens
                + rec.cache_read_tokens
                + rec.cache_write_5m_tokens
                + rec.cache_write_1h_tokens
            )
            ctx_series.append(ctx)
            cost_series.append(cost)
            token_series.append(ctx + rec.output_tokens)

        out: list[RenderableType] = []

        # Sparkline of context size per turn.
        spark = _sparkline(ctx_series, width=60)
        ctx_max = max(ctx_series) if ctx_series else 0
        out.append(Text.from_markup(
            f"[b]Context size per turn[/b]  "
            f"[cyan]{spark}[/cyan]  "
            f"[dim]peak {ctx_max/1000:.0f}K · {len(ctx_series)} turns[/dim]"
        ))

        # Cumulative cost line chart.
        cumulative_cost: list[float] = []
        running = 0.0
        for c in cost_series:
            running += c
            cumulative_cost.append(running)
        plt.plot(list(range(1, len(cumulative_cost) + 1)), cumulative_cost,
                 color="cyan", marker="braille")
        plt.title("Cumulative cost ($) over turns")
        plt.xlabel("turn")
        plt.ylabel("cost ($)")
        plt.theme("clear")
        out.append(Text(_plotext_to_str(width=100, height=14), no_wrap=True))

        # Histogram of tokens-per-turn.
        plt.hist(token_series, bins=20, color="green", marker="hd")
        plt.title("Tokens per turn distribution")
        plt.xlabel("tokens")
        plt.ylabel("turns")
        plt.theme("clear")
        out.append(Text(_plotext_to_str(width=100, height=12), no_wrap=True))

        return out

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
