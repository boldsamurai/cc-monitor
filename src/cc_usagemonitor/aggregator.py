from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .parser import HookEvent, UsageRecord
from .pricing import PricingTable


@dataclass
class TokenSums:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add(self, rec: UsageRecord, cost: float) -> None:
        self.input += rec.input_tokens
        self.output += rec.output_tokens
        self.cache_read += rec.cache_read_tokens
        self.cache_write_5m += rec.cache_write_5m_tokens
        self.cache_write_1h += rec.cache_write_1h_tokens
        self.cost_usd += cost
        self.turns += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.input
            + self.output
            + self.cache_read
            + self.cache_write_5m
            + self.cache_write_1h
        )


@dataclass
class SessionState:
    session_id: str
    project_slug: str
    sums: TokenSums = field(default_factory=TokenSums)
    sums_main: TokenSums = field(default_factory=TokenSums)
    sums_sidechain: TokenSums = field(default_factory=TokenSums)
    sums_from_agents: TokenSums = field(default_factory=TokenSums)
    by_model: dict[str, TokenSums] = field(default_factory=lambda: defaultdict(TokenSums))
    last_seen: datetime | None = None
    first_seen: datetime | None = None


@dataclass
class ToolSpan:
    """A pending or completed tool span built from hook events."""
    span_id: str
    session_id: str
    tool: str
    name: str | None
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    # Usage attributed after correlation.
    sums: TokenSums = field(default_factory=TokenSums)


class Aggregator:
    """In-memory state. Single-threaded — only mutated from the asyncio loop."""

    def __init__(self, pricing: PricingTable, recent_window_seconds: int = 60):
        self.pricing = pricing
        self.sessions: dict[str, SessionState] = {}
        self.spans_by_id: dict[str, ToolSpan] = {}
        self.spans_by_session: dict[str, list[ToolSpan]] = defaultdict(list)
        # Track recently-ended tool spans per session, FIFO. Oldest first.
        self._pending_correlation: dict[str, deque[ToolSpan]] = defaultdict(deque)
        self.recent_usage: deque[tuple[datetime, int, float]] = deque()
        self._recent_window = timedelta(seconds=recent_window_seconds)
        # Aggregations per skill / agent name.
        self.by_skill: dict[str, TokenSums] = defaultdict(TokenSums)
        self.by_agent: dict[str, TokenSums] = defaultdict(TokenSums)
        # 5h block tracking.
        self.block_start: datetime | None = None
        self.block_sums: TokenSums = TokenSums()

    # ----- ingest -----

    def ingest(self, item) -> None:
        if isinstance(item, UsageRecord):
            self._ingest_usage(item)
        elif isinstance(item, HookEvent):
            self._ingest_event(item)

    def _ingest_usage(self, rec: UsageRecord) -> None:
        price = self.pricing.for_model(rec.model)
        cost = price.cost(rec.raw_usage)

        sess = self.sessions.get(rec.session_id)
        if sess is None:
            sess = SessionState(session_id=rec.session_id, project_slug=rec.project_slug)
            self.sessions[rec.session_id] = sess
            sess.first_seen = rec.ts

        sess.last_seen = rec.ts
        sess.sums.add(rec, cost)
        if rec.is_sidechain:
            sess.sums_sidechain.add(rec, cost)
        else:
            sess.sums_main.add(rec, cost)
        sess.by_model[rec.model or "unknown"].add(rec, cost)

        self._update_block(rec, cost)
        self._update_recent(rec, cost)
        self._try_attribute_to_span(rec, cost)

    def _ingest_event(self, ev: HookEvent) -> None:
        if ev.event == "tool_start" and ev.span_id:
            span = ToolSpan(
                span_id=ev.span_id,
                session_id=ev.session_id,
                tool=ev.tool or "",
                name=ev.name,
                started_at=ev.ts,
            )
            self.spans_by_id[ev.span_id] = span
            self.spans_by_session[ev.session_id].append(span)
        elif ev.event == "tool_end" and ev.span_id:
            span = self.spans_by_id.get(ev.span_id)
            if span is None:
                # tool_end without start — synthesize.
                span = ToolSpan(
                    span_id=ev.span_id,
                    session_id=ev.session_id,
                    tool=ev.tool or "",
                    name=ev.name,
                    started_at=ev.ts,
                )
                self.spans_by_id[ev.span_id] = span
                self.spans_by_session[ev.session_id].append(span)
            span.ended_at = ev.ts
            span.duration_ms = ev.duration_ms
            # Skill / Agent spans are eligible for usage attribution: usage
            # records that arrive AFTER tool_end belong to the turn that
            # contained the tool call. We FIFO-attribute the next usage in
            # this session to this span.
            if span.tool in ("Skill", "Agent"):
                self._pending_correlation[ev.session_id].append(span)
        # 'stop' currently has no aggregate state — useful for future per-turn buckets.

    # ----- helpers -----

    def _update_block(self, rec: UsageRecord, cost: float) -> None:
        if self.block_start is None or rec.ts - self.block_start >= timedelta(hours=5):
            self.block_start = rec.ts
            self.block_sums = TokenSums()
        self.block_sums.add(rec, cost)

    def _update_recent(self, rec: UsageRecord, cost: float) -> None:
        # Rate measures "tokens flowing right now". A record qualifies only if
        # both its API-side timestamp AND ingest time fall in the window —
        # this filters out --from-start replay of historical sessions while
        # still tolerating tail-mode ingest of live sessions.
        now = datetime.now(tz=timezone.utc)
        rec_ts = rec.ts if rec.ts.tzinfo else rec.ts.replace(tzinfo=timezone.utc)
        if now - rec_ts > self._recent_window:
            return
        tokens = (
            rec.input_tokens
            + rec.output_tokens
            + rec.cache_read_tokens
            + rec.cache_write_5m_tokens
            + rec.cache_write_1h_tokens
        )
        self.recent_usage.append((now, tokens, cost))
        cutoff = now - self._recent_window
        while self.recent_usage and self.recent_usage[0][0] < cutoff:
            self.recent_usage.popleft()

    def active_session_count(self, max_idle: timedelta = timedelta(minutes=30)) -> int:
        """Sessions whose last_seen is within max_idle of now."""
        cutoff = datetime.now(tz=timezone.utc) - max_idle
        n = 0
        for s in self.sessions.values():
            if s.last_seen is None:
                continue
            ts = s.last_seen
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                n += 1
        return n

    def _try_attribute_to_span(self, rec: UsageRecord, cost: float) -> None:
        queue = self._pending_correlation.get(rec.session_id)
        if not queue:
            return
        span = queue.popleft()
        span.sums.add(rec, cost)
        if span.tool == "Skill" and span.name:
            self.by_skill[span.name].add(rec, cost)
        elif span.tool == "Agent" and span.name:
            self.by_agent[span.name].add(rec, cost)
            sess = self.sessions.get(rec.session_id)
            if sess is not None:
                sess.sums_from_agents.add(rec, cost)

    # ----- read API for TUI -----

    def recent_token_rate_per_min(self) -> float:
        return self._scale_to_per_min(sum(t for _, t, _ in self.recent_usage))

    def recent_cost_per_min(self) -> float:
        return self._scale_to_per_min(sum(c for _, _, c in self.recent_usage))

    def recent_turns_per_min(self) -> float:
        return self._scale_to_per_min(len(self.recent_usage))

    def _scale_to_per_min(self, total: float) -> float:
        if not self.recent_usage:
            return 0.0
        seconds = self._recent_window.total_seconds()
        return total * 60.0 / seconds

    def total_sums(self) -> TokenSums:
        agg = TokenSums()
        for s in self.sessions.values():
            agg.input += s.sums.input
            agg.output += s.sums.output
            agg.cache_read += s.sums.cache_read
            agg.cache_write_5m += s.sums.cache_write_5m
            agg.cache_write_1h += s.sums.cache_write_1h
            agg.cost_usd += s.sums.cost_usd
            agg.turns += s.sums.turns
        return agg
