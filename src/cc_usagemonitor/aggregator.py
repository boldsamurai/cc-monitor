from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .anthropic_usage import UsageData
from .parser import HookEvent, UsageRecord
from .pricing import PricingTable

BLOCK_DURATION = timedelta(hours=5)
LONG_WINDOW = timedelta(days=8)  # 192h, matches Maciek-roboblog's P90 window


def _percentile(values: list[float], p: float) -> float:
    """Linear interpolation percentile (NumPy's default behavior)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


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
    # Approximation of how full the model's context window was on the most
    # recent assistant turn: input + cache_read + cache_write_5m + cache_write_1h.
    # Updated on every ingest; final value = last seen turn for this session.
    last_context_tokens: int = 0
    last_context_model: str = ""
    # Per-session high-water mark of prompt size — used to infer the true
    # context window size when the model id alone doesn't tell us
    # (Anthropic returns 'claude-opus-4-7' even when the user is running
    # the 1M variant locally configured as 'opus[1m]').
    max_context_tokens: int = 0
    # Per-session breakdown for skill/agent calls correlated via hooks.
    skills: dict[str, TokenSums] = field(default_factory=dict)
    agents: dict[str, TokenSums] = field(default_factory=dict)


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


@dataclass
class BlockInfo:
    """Snapshot of the current 5-hour Anthropic session block."""
    start: datetime
    end: datetime  # start + 5h
    sums: TokenSums
    minutes_elapsed: float
    minutes_remaining: float
    burn_tokens_per_min: float
    burn_cost_per_min: float
    eta_to_token_limit_min: float | None = None
    eta_to_cost_limit_min: float | None = None
    token_limit: int | None = None
    cost_limit: float | None = None
    pct_tokens: float | None = None
    pct_cost: float | None = None


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
        # Long-window record archive (7 days) for rolling sums and 5h-block
        # detection. Each entry is (rec_ts, rec, cost). Kept sorted by rec_ts.
        self._long_window: deque[tuple[datetime, UsageRecord, float]] = deque()
        # Optional plan limits for the active 5h block.
        self.token_limit: int | None = None
        self.cost_limit: float | None = None
        # Authoritative usage data from Anthropic /api/oauth/usage. When
        # populated, the TUI prefers it over local-only token/cost limits.
        self.api_usage: UsageData | None = None

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
        ctx = (
            rec.input_tokens
            + rec.cache_read_tokens
            + rec.cache_write_5m_tokens
            + rec.cache_write_1h_tokens
        )
        sess.last_context_tokens = ctx
        sess.last_context_model = rec.model
        if ctx > sess.max_context_tokens:
            sess.max_context_tokens = ctx
        sess.sums.add(rec, cost)
        if rec.is_sidechain:
            sess.sums_sidechain.add(rec, cost)
        else:
            sess.sums_main.add(rec, cost)
        sess.by_model[rec.model or "unknown"].add(rec, cost)

        self._update_long_window(rec, cost)
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

    def _update_long_window(self, rec: UsageRecord, cost: float) -> None:
        """Append rec to the 7d archive (kept sorted by rec.ts) and prune
        anything older than now - 7d."""
        rec_ts = rec.ts if rec.ts.tzinfo else rec.ts.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        if now - rec_ts > LONG_WINDOW:
            return  # too old to matter
        # Insert keeping order. Records typically arrive in order during tail
        # mode, but --from-start replay walks files in glob order so we may
        # need to insert in the middle. Linear-scan from the right is fine
        # for ~10k entries.
        if not self._long_window or self._long_window[-1][0] <= rec_ts:
            self._long_window.append((rec_ts, rec, cost))
        else:
            # Find insertion point from the right.
            arr = list(self._long_window)
            i = len(arr)
            while i > 0 and arr[i - 1][0] > rec_ts:
                i -= 1
            arr.insert(i, (rec_ts, rec, cost))
            self._long_window = deque(arr)
        # Prune stale entries.
        cutoff = now - LONG_WINDOW
        while self._long_window and self._long_window[0][0] < cutoff:
            self._long_window.popleft()

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
        sess = self.sessions.get(rec.session_id)
        if span.tool == "Skill" and span.name:
            self.by_skill[span.name].add(rec, cost)
            if sess is not None:
                sess.skills.setdefault(span.name, TokenSums()).add(rec, cost)
        elif span.tool == "Agent" and span.name:
            self.by_agent[span.name].add(rec, cost)
            if sess is not None:
                sess.sums_from_agents.add(rec, cost)
                sess.agents.setdefault(span.name, TokenSums()).add(rec, cost)

    # ----- read API for TUI -----

    def sums_in_window(self, window: timedelta) -> TokenSums:
        """Aggregate tokens/cost for records whose ts is within `window`
        of now. Cheap because it walks the 7d deque, which is bounded."""
        cutoff = datetime.now(tz=timezone.utc) - window
        sums = TokenSums()
        for ts, rec, cost in self._long_window:
            if ts >= cutoff:
                sums.add(rec, cost)
        return sums

    def auto_detect_limits_p90(self) -> tuple[int, float] | None:
        """Analyze the 8-day window and return (P90 token limit, P90 cost
        limit) computed across historical 5h blocks. Used by --plan auto.

        Returns None if there are fewer than 3 historical blocks (P90 of
        a 1-2 element list is meaningless).
        """
        if not self._long_window:
            return None
        records = list(self._long_window)
        # Identify block boundaries: a new block starts after a >=5h gap.
        block_token_totals: list[int] = []
        block_cost_totals: list[float] = []
        cur_tokens = 0
        cur_cost = 0.0
        prev_ts: datetime | None = None
        for ts, rec, cost in records:
            if prev_ts is not None and ts - prev_ts >= BLOCK_DURATION:
                block_token_totals.append(cur_tokens)
                block_cost_totals.append(cur_cost)
                cur_tokens = 0
                cur_cost = 0.0
            cur_tokens += (
                rec.input_tokens
                + rec.output_tokens
                + rec.cache_read_tokens
                + rec.cache_write_5m_tokens
                + rec.cache_write_1h_tokens
            )
            cur_cost += cost
            prev_ts = ts
        if cur_tokens:
            block_token_totals.append(cur_tokens)
            block_cost_totals.append(cur_cost)

        if len(block_token_totals) < 3:
            return None

        token_p90 = _percentile(block_token_totals, 90)
        cost_p90 = _percentile(block_cost_totals, 90)
        # Round token limit up to a clean number for nicer display.
        token_p90 = int(round(token_p90))
        return (token_p90, cost_p90)

    def block_info(self) -> BlockInfo | None:
        """Compute the current 5-hour block from the long-window archive.

        A block starts at the first record after a >=5h gap (or at the very
        first record). The "current" block is the latest such span that
        contains the most recent record. Returns None if no record is
        within the last 5 hours.
        """
        if not self._long_window:
            return None

        # Walk forward and identify block_start of the most recent block.
        sorted_records = list(self._long_window)  # already sorted
        block_start = sorted_records[0][0]
        prev_ts = sorted_records[0][0]
        for ts, _rec, _cost in sorted_records[1:]:
            if ts - prev_ts >= BLOCK_DURATION:
                block_start = ts
            prev_ts = ts

        now = datetime.now(tz=timezone.utc)
        block_end = block_start + BLOCK_DURATION
        # If the latest record is older than block_end, the block has elapsed
        # already — no active block.
        latest_ts = sorted_records[-1][0]
        if now - latest_ts > BLOCK_DURATION and now > block_end:
            return None

        sums = TokenSums()
        for ts, rec, cost in sorted_records:
            if ts >= block_start:
                sums.add(rec, cost)

        elapsed_min = max(1.0, (now - block_start).total_seconds() / 60.0)
        remaining_min = max(0.0, (block_end - now).total_seconds() / 60.0)
        burn_tokens = sums.total_tokens / elapsed_min
        burn_cost = sums.cost_usd / elapsed_min

        info = BlockInfo(
            start=block_start,
            end=block_end,
            sums=sums,
            minutes_elapsed=elapsed_min,
            minutes_remaining=remaining_min,
            burn_tokens_per_min=burn_tokens,
            burn_cost_per_min=burn_cost,
            token_limit=self.token_limit,
            cost_limit=self.cost_limit,
        )

        if self.token_limit:
            info.pct_tokens = sums.total_tokens / self.token_limit * 100
            remaining_tokens = max(0, self.token_limit - sums.total_tokens)
            info.eta_to_token_limit_min = (
                remaining_tokens / burn_tokens if burn_tokens > 0 else None
            )
        if self.cost_limit:
            info.pct_cost = sums.cost_usd / self.cost_limit * 100
            remaining_cost = max(0.0, self.cost_limit - sums.cost_usd)
            info.eta_to_cost_limit_min = (
                remaining_cost / burn_cost if burn_cost > 0 else None
            )

        return info

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
