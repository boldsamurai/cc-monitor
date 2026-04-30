from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .anthropic_usage import UsageData
from .logger import get_logger
from .parser import HookEvent, UsageRecord
from .paths import PROJECTS_DIR
from .pricing import PricingTable

log = get_logger(__name__)

BLOCK_DURATION = timedelta(hours=5)
LONG_WINDOW = timedelta(days=8)  # 192h, matches Maciek-roboblog's P90 window


def _top_of_hour(ts: datetime) -> datetime:
    """Truncate a timestamp to the start of its hour.

    Anthropic anchors 5h session blocks at the top of the first
    message's hour — see Maciek-roboblog README: 'If you start at
    14:35, your session ends at 19:00.' Without this anchoring, our
    locally-inferred block_end drifts by up to 59 minutes from the
    server's reset_at, and the BlockPanel's projection 'by HH:MM'
    line disagrees with the API's '5h resets HH:MM' line.
    """
    return ts.replace(minute=0, second=0, microsecond=0)


def _iter_blocks(
    records: list[tuple[datetime, "UsageRecord", float]],
):
    """Yield (block_start, block_end, [record_indices]) for the input.

    Block detection rule (matches Anthropic's behavior):
      - First record opens a block anchored at its top-of-hour.
      - Each subsequent record either falls into the current block
        (ts < block_end) or opens a new one (ts >= block_end), again
        anchored at top-of-hour.

    Records must be in chronological order — the long-window deque
    already maintains that invariant.
    """
    if not records:
        return
    block_start = _top_of_hour(records[0][0])
    block_end = block_start + BLOCK_DURATION
    indices: list[int] = []
    for i, (ts, _rec, _cost) in enumerate(records):
        if ts >= block_end:
            yield (block_start, block_end, indices)
            block_start = _top_of_hour(ts)
            block_end = block_start + BLOCK_DURATION
            indices = []
        indices.append(i)
    if indices:
        yield (block_start, block_end, indices)


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
    # Bumped by every ingest of this session. Aggregator's per-session
    # cache keys derived helpers ('count_file_reads', etc.) on this so
    # opening a detail screen twice in a row hits the cache instead of
    # re-parsing the JSONL from disk. Lives on the session instead of
    # globally so an ingest of session A doesn't invalidate session B's
    # cached file-read stats.
    revision: int = 0
    # Ground-truth project path captured from the JSONL ('cwd' field).
    # Kept here once seen — the slug → path decode is lossy (slug
    # encoder collapses '/', '_', '.' all to '-'), so the real cwd is
    # the only fully reliable source.
    cwd: str | None = None


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
    eta_to_cost_limit_min: float | None = None
    cost_limit: float | None = None
    pct_cost: float | None = None


class Aggregator:
    """In-memory state. Single-threaded — only mutated from the asyncio loop."""

    def __init__(
        self,
        pricing: PricingTable,
        recent_window_seconds: int = 60,
        projects_dir: Path | None = None,
    ):
        self.pricing = pricing
        # Detail-screen helpers re-read JSONL from disk (load_full_
        # session_turns, count_file_reads, etc.). Make the lookup root
        # configurable so --projects-dir on the CLI keeps both the
        # Tailer and the aggregator pointing at the same tree.
        self.projects_dir = projects_dir if projects_dir is not None else PROJECTS_DIR
        self.sessions: dict[str, SessionState] = {}
        self.spans_by_id: dict[str, ToolSpan] = {}
        self.spans_by_session: dict[str, list[ToolSpan]] = defaultdict(list)
        # Skill spans waiting for the next non-sidechain assistant turn
        # to attribute (FIFO). Skills don't spawn a subagent — the cost
        # shows up on the parent's continuation, so 'next message wins'
        # is a reasonable approximation.
        self._pending_correlation: dict[str, deque[ToolSpan]] = defaultdict(deque)
        self.recent_usage: deque[tuple[datetime, int, float]] = deque()
        self._recent_window = timedelta(seconds=recent_window_seconds)
        # Aggregations per skill / agent name.
        self.by_skill: dict[str, TokenSums] = defaultdict(TokenSums)
        self.by_agent: dict[str, TokenSums] = defaultdict(TokenSums)
        # Long-window record archive (7 days) for rolling sums and 5h-block
        # detection. Each entry is (rec_ts, rec, cost). Kept sorted by rec_ts.
        self._long_window: deque[tuple[datetime, UsageRecord, float]] = deque()
        # Optional plan cost ceiling for the active 5h block. The token
        # half of the old plan presets was misleading in cache-heavy
        # usage and got dropped; only cost is published authoritatively
        # by Anthropic so it's the only one we model.
        self.cost_limit: float | None = None
        # Authoritative usage data from Anthropic /api/oauth/usage. When
        # populated, the TUI prefers it over local-only token/cost limits.
        self.api_usage: UsageData | None = None
        # Monotonic counter bumped by every state-mutating ingest. The
        # TUI's _refresh_view reads this and skips full table rebuilds
        # when it hasn't changed since the previous tick. Without this,
        # the 0.5s tick walks all sessions on every fire even if no
        # JSONL line was added — cheap per session but at hundreds of
        # sessions it eats enough main-thread time to make mouse
        # clicks feel laggy. Per-session counters live on SessionState
        # so detail-screen caches can scope-invalidate.
        self.revision: int = 0
        # Cached results of expensive per-session helpers. Keyed by
        # (session_id, method_name); value is (session_revision, result)
        # so a stale entry (lower revision than the live SessionState)
        # is silently recomputed. Most detail-screen helpers re-read
        # the JSONL from disk and are the dominant cost of opening a
        # session/project detail twice.
        self._session_cache: dict[
            tuple[str, str], tuple[int, object]
        ] = {}

    # ----- snapshot / restore for cross-run persistence -----

    # Fields persisted across runs. Pricing is loaded fresh, api_usage
    # is re-fetched, _session_cache is cheap to rebuild — none belong
    # in the snapshot. revision is preserved so the TUI's _last_data_
    # revision gate works correctly on restore.
    _SNAPSHOT_FIELDS = (
        "sessions",
        "spans_by_id",
        "spans_by_session",
        "_pending_correlation",
        "recent_usage",
        "by_skill",
        "by_agent",
        "_long_window",
        "cost_limit",
        "revision",
    )

    def snapshot(self) -> dict:
        """Return a dict of state for cross-run persistence. The dict is
        pickled by state.save() — keep values to plain Python / dataclass
        instances so unpickling doesn't depend on third-party state."""
        return {
            field: getattr(self, field)
            for field in self._SNAPSHOT_FIELDS
        }

    def restore(self, data: dict) -> None:
        """Replace runtime state from a snapshot dict produced by
        ``snapshot()``. Tolerates missing keys (older snapshots that
        predate a new field) — those just stay at their __init__
        defaults so a forward-compat snapshot still works without a
        SCHEMA_VERSION bump."""
        for field in self._SNAPSHOT_FIELDS:
            if field in data:
                setattr(self, field, data[field])
        log.info(
            "aggregator restored: %d sessions, %d archive records",
            len(self.sessions), len(self._long_window),
        )

    def reset_state(self) -> None:
        """Drop all accumulated runtime state but keep configuration
        (token/cost limits, api_usage). After this, a follow-up
        ``Tailer.reset_tails()`` + next polling tick will rebuild from
        scratch — used by the Settings 'Force re-scan' action.
        """
        self.sessions.clear()
        self.spans_by_id.clear()
        self.spans_by_session.clear()
        self._pending_correlation.clear()
        self.recent_usage.clear()
        self.by_skill.clear()
        self.by_agent.clear()
        self._long_window.clear()
        self._session_cache.clear()
        # Bump so _refresh_view paints empty tables instead of stale
        # rows on the very next tick.
        self.revision += 1
        log.info("aggregator state reset (force re-scan)")

    # ----- ingest -----

    def ingest(self, item) -> None:
        if isinstance(item, UsageRecord):
            self._ingest_usage(item)
            self.revision += 1
        elif isinstance(item, HookEvent):
            self._ingest_event(item)
            # Hook events bump last_seen on existing sessions, which
            # affects sort order on the Sessions tab — also a state
            # change worth marking.
            self.revision += 1

    def _ingest_usage(self, rec: UsageRecord) -> None:
        price = self.pricing.for_model(rec.model)
        cost = price.cost(rec.raw_usage)

        sess = self.sessions.get(rec.session_id)
        if sess is None:
            sess = SessionState(session_id=rec.session_id, project_slug=rec.project_slug)
            self.sessions[rec.session_id] = sess
            sess.first_seen = rec.ts
            log.info(
                "new session %s in project %s (model=%s)",
                rec.session_id[:8], rec.project_slug, rec.model,
            )

        # Latch the cwd the first time we see it. Lines without cwd
        # (synthetic or early-session events) are ignored.
        if sess.cwd is None and rec.cwd:
            sess.cwd = rec.cwd

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
        # Per-session cache invalidator — see SessionState.revision.
        sess.revision += 1

        self._update_long_window(rec, cost)
        self._update_recent(rec, cost)
        self._try_attribute_to_span(rec, cost)

    def _ingest_event(self, ev: HookEvent) -> None:
        log.debug(
            "hook event %s session=%s tool=%s name=%s",
            ev.event, ev.session_id[:8], ev.tool, ev.name,
        )
        # Bump last_seen on every hook tick so the Sessions tab sorts by
        # real activity, not just the last completed assistant turn.
        # Tool hooks fire during a turn (every Read/Bash/Edit/Skill/Agent
        # call), so a long-running turn keeps the session at the top.
        sess = self.sessions.get(ev.session_id)
        if sess is not None and (sess.last_seen is None or ev.ts > sess.last_seen):
            sess.last_seen = ev.ts
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
            # Agent: sweep the long-window archive for sidechain records
            # in this span's [start, end] timestamp window and attribute
            # them. This is robust regardless of ingest order — works for
            # both live tail (records arrive between start/end) and
            # --from-start replay (records arrive long before tool_end).
            if span.tool == "Agent":
                self._attribute_sidechains_to_agent(span)
            # Skills don't spawn a subagent — queue for FIFO attribution
            # to the next non-sidechain turn (the parent's continuation
            # message includes the skill's injected instructions).
            if span.tool == "Skill":
                self._pending_correlation[ev.session_id].append(span)
        # 'stop' currently has no aggregate state — useful for future per-turn buckets.

    def _attribute_sidechains_to_agent(self, span: ToolSpan) -> None:
        """Sum every sidechain UsageRecord that falls in this Agent
        span's window. Idempotent: only attributes records once because
        tool_end events arrive once per span_id."""
        if span.ended_at is None:
            return
        sess = self.sessions.get(span.session_id)
        attributed = 0
        for ts, rec, cost in self._long_window:
            if rec.session_id != span.session_id:
                continue
            if not rec.is_sidechain:
                continue
            if ts < span.started_at or ts > span.ended_at:
                continue
            span.sums.add(rec, cost)
            attributed += 1
            if span.name:
                self.by_agent[span.name].add(rec, cost)
                if sess is not None:
                    sess.sums_from_agents.add(rec, cost)
                    sess.agents.setdefault(span.name, TokenSums()).add(
                        rec, cost
                    )
        log.info(
            "agent span attributed: %s/%s in session %s -> %d sidechain records, $%.4f",
            span.tool, span.name, span.session_id[:8], attributed, span.sums.cost_usd,
        )

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

    def cost_per_day_per_model(
        self, days: int = 7
    ) -> tuple[list[date], dict[str, list[float]]]:
        """Aggregate cost from the long-window archive into per-(date, model)
        buckets. See ``_per_day_per_model`` for return shape."""
        return self._per_day_per_model(days, value="cost")

    def tokens_per_day_per_model(
        self, days: int = 7
    ) -> tuple[list[date], dict[str, list[float]]]:
        """Aggregate total tokens (input + output + cache_read +
        cache_write_5m + cache_write_1h) per (date, model). Same shape
        as cost_per_day_per_model — see _per_day_per_model."""
        return self._per_day_per_model(days, value="tokens")

    def _per_day_per_model(
        self, days: int, value: str
    ) -> tuple[list[date], dict[str, list[float]]]:
        """Returns ``(dates, per_model)`` where ``dates`` is a
        chronological list of length ``days`` ending today (UTC) and
        ``per_model`` is a {model: [value_for_each_date]} dict aligned
        with that list. Models present in the archive but with zero
        value on a given day still get a 0.0 entry so the lists stay
        equal length — that's what plotext's stacked_bar expects.

        ``value`` selects the per-record metric: 'cost' or 'tokens'."""
        today = datetime.now(tz=timezone.utc).date()
        dates: list[date] = [
            today - timedelta(days=i) for i in range(days - 1, -1, -1)
        ]
        date_index = {d: i for i, d in enumerate(dates)}

        per_model: dict[str, list[float]] = {}
        for ts, rec, cost in self._long_window:
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            d = ts_utc.date()
            idx = date_index.get(d)
            if idx is None:
                continue
            if value == "cost":
                v = cost
            else:
                v = (
                    rec.input_tokens
                    + rec.output_tokens
                    + rec.cache_read_tokens
                    + rec.cache_write_5m_tokens
                    + rec.cache_write_1h_tokens
                )
            model = rec.model or "(unknown)"
            series = per_model.setdefault(model, [0.0] * days)
            series[idx] += v
        return dates, per_model

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
        # Agents are attributed retroactively at tool_end via
        # _attribute_sidechains_to_agent, sweeping _long_window. Sidechain
        # records arriving here have nothing to do — skip.
        if rec.is_sidechain:
            return
        # Non-sidechain: FIFO match against queued Skill spans.
        queue = self._pending_correlation.get(rec.session_id)
        if not queue:
            return
        span = queue.popleft()
        span.sums.add(rec, cost)
        if span.tool == "Skill" and span.name:
            sess = self.sessions.get(rec.session_id)
            self.by_skill[span.name].add(rec, cost)
            if sess is not None:
                sess.skills.setdefault(span.name, TokenSums()).add(rec, cost)

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

    def sums_in_range(
        self, start: datetime, end: datetime | None = None
    ) -> TokenSums:
        """Aggregate tokens/cost for records whose ts falls in
        ``[start, end)``. ``end=None`` means 'up to now'.

        Used by the SummaryPanel's daily/weekly rows where the bucket
        boundaries are calendar-aligned (midnight today, last Monday)
        rather than rolling-by-now windows.
        """
        sums = TokenSums()
        if end is None:
            end = datetime.now(tz=timezone.utc)
        for ts, rec, cost in self._long_window:
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if start <= ts_utc < end:
                sums.add(rec, cost)
        return sums

    def _cached_for_session(self, session_id: str, name: str, fn):
        """Return fn() but cache the result keyed on the session's
        revision. Stale entries (older revision) are silently replaced
        on the next call. Sessions not in self.sessions short-circuit
        to fn() with no caching — there's nothing to invalidate against.
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            return fn()
        key = (session_id, name)
        cached = self._session_cache.get(key)
        if cached is not None and cached[0] == sess.revision:
            return cached[1]
        value = fn()
        self._session_cache[key] = (sess.revision, value)
        return value

    def turns_for_session(
        self, session_id: str
    ) -> list[tuple[datetime, UsageRecord, float]]:
        """Return chronological per-turn records for a session, limited to
        what's still in the 8-day archive. Empty if the session is older."""
        return self._cached_for_session(
            session_id, "turns_for_session",
            lambda: [
                (ts, rec, cost)
                for ts, rec, cost in self._long_window
                if rec.session_id == session_id
            ],
        )

    def count_file_reads_in_session(
        self, session_id: str
    ) -> dict[str, dict[str, int]]:
        """Per-file Read tool stats for a session.

        Returns {file_path: {'reads': N, 'chars': total, 'tokens_est': T}}.
        Backed by the combined session_jsonl_stats helper so calling all
        three count_*_in_session methods opens the JSONL once, not three
        times.
        """
        return self._session_jsonl_stats(session_id)["reads"]

    def count_file_writes_in_session(
        self, session_id: str
    ) -> dict[str, dict[str, int]]:
        """Per-file Write/Edit tool stats for a session.

        Returns {file_path: {'writes': N, 'edits': M, 'chars': C,
        'tokens_est': T}}. 'writes' is full-content Write calls; 'edits'
        lumps Edit and NotebookEdit. 'chars' sums Write.content,
        Edit.new_string, NotebookEdit.new_source.
        """
        return self._session_jsonl_stats(session_id)["writes"]

    def count_tools_in_session(self, session_id: str) -> dict[str, int]:
        """Count tool_use blocks per tool name in a session's JSONL file.

        Independent of the hook pipeline — reads tool_use entries directly
        from the conversation log so it works retroactively for sessions
        that predate the hook setup. Returns name -> count.
        """
        return self._session_jsonl_stats(session_id)["tools"]

    def _session_jsonl_stats(self, session_id: str) -> dict:
        """Single-pass extractor for every per-session derived stat that
        needs the conversation JSONL.

        Each call used to re-read and re-parse the same file three
        separate times (count_file_reads / count_file_writes /
        count_tools). For ProjectDetail that ran 3N file reads where
        N = sessions in the project; this collapses it to N. The result
        is cached on the session's revision so a second open is free.
        """
        return self._cached_for_session(
            session_id, "session_jsonl_stats",
            lambda: self._compute_session_jsonl_stats(session_id),
        )

    def _compute_session_jsonl_stats(self, session_id: str) -> dict:
        import json

        empty = {"reads": {}, "writes": {}, "tools": {}}
        sess = self.sessions.get(session_id)
        if sess is None:
            return empty
        path = self.projects_dir / sess.project_slug / f"{session_id}.jsonl"
        if not path.is_file():
            return empty
        try:
            text = path.read_text()
        except OSError:
            return empty

        # All three derived dicts come from one walk over the JSONL.
        # 'pending' correlates a Read's tool_use_id to its file_path so
        # the later tool_result block can attribute its content size
        # back to the right file.
        reads: dict[str, dict[str, int]] = {}
        writes: dict[str, dict[str, int]] = {}
        tools: dict[str, int] = {}
        pending: dict[str, str] = {}

        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = obj.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "tool_use":
                    name = item.get("name") or "?"
                    tools[name] = tools.get(name, 0) + 1
                    inp = item.get("input") or {}
                    if name == "Read":
                        fp = inp.get("file_path") or "?"
                        pending[item.get("id") or ""] = fp
                        bucket = reads.setdefault(
                            fp, {"reads": 0, "chars": 0, "tokens_est": 0}
                        )
                        bucket["reads"] += 1
                    elif name in ("Write", "Edit", "NotebookEdit"):
                        fp = (
                            inp.get("file_path")
                            or inp.get("notebook_path")
                            or "?"
                        )
                        bucket = writes.setdefault(
                            fp,
                            {"writes": 0, "edits": 0, "chars": 0, "tokens_est": 0},
                        )
                        if name == "Write":
                            bucket["writes"] += 1
                            bucket["chars"] += len(inp.get("content") or "")
                        elif name == "Edit":
                            bucket["edits"] += 1
                            bucket["chars"] += len(inp.get("new_string") or "")
                        else:  # NotebookEdit
                            bucket["edits"] += 1
                            bucket["chars"] += len(inp.get("new_source") or "")
                elif itype == "tool_result":
                    fp = pending.get(item.get("tool_use_id") or "")
                    if fp is None:
                        continue
                    result_content = item.get("content")
                    if isinstance(result_content, list):
                        chars = sum(
                            len(c.get("text") or "")
                            for c in result_content
                            if isinstance(c, dict)
                        )
                    elif isinstance(result_content, str):
                        chars = len(result_content)
                    else:
                        chars = 0
                    reads[fp]["chars"] += chars

        # Rough estimate: 1 token ~ 4 chars (Anthropic tokenizer is
        # ~3.5 for prose, ~4–5 for code; within 20% either way).
        for stats in reads.values():
            stats["tokens_est"] = stats["chars"] // 4
        for stats in writes.values():
            stats["tokens_est"] = stats["chars"] // 4
        return {"reads": reads, "writes": writes, "tools": tools}

    def load_full_session_turns(
        self, session_id: str
    ) -> list[tuple[datetime, UsageRecord, float]]:
        """Re-parse the JSONL file for a session and return every turn.

        Slower than `turns_for_session` but works regardless of the 8-day
        archive — useful for the detail screen of older sessions whose
        per-turn data has aged out.
        """
        return self._cached_for_session(
            session_id, "load_full_session_turns",
            lambda: self._compute_full_session_turns(session_id),
        )

    def _compute_full_session_turns(
        self, session_id: str
    ) -> list[tuple[datetime, UsageRecord, float]]:
        from .parser import parse_session_line

        sess = self.sessions.get(session_id)
        if sess is None:
            return []
        path = self.projects_dir / sess.project_slug / f"{session_id}.jsonl"
        if not path.is_file():
            return []

        # Pull in subagent JSONLs too — they record the actual API calls
        # the agents made (with sessionId pointing at this parent and
        # isSidechain=True). Without these the chart undercounts cost
        # for sessions that delegate heavy work to agents.
        subagent_dir = (
            self.projects_dir / sess.project_slug / session_id / "subagents"
        )
        files_to_read = [path]
        if subagent_dir.is_dir():
            files_to_read.extend(sorted(subagent_dir.glob("*.jsonl")))

        turns: list[tuple[datetime, UsageRecord, float]] = []
        for f in files_to_read:
            try:
                text = f.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                rec = parse_session_line(line, sess.project_slug)
                if rec is None:
                    continue
                cost = self.pricing.for_model(rec.model).cost(rec.raw_usage)
                ts = (
                    rec.ts
                    if rec.ts.tzinfo
                    else rec.ts.replace(tzinfo=timezone.utc)
                )
                turns.append((ts, rec, cost))
        turns.sort(key=lambda x: x[0])
        return turns

    def auto_detect_limits_p90(self) -> float | None:
        """Return P90 cost across historical 5h blocks in the 8-day
        archive. Used by --plan auto. Returns None if there are fewer
        than 3 historical blocks (P90 of a 1-2 element list is
        meaningless).
        """
        if not self._long_window:
            return None
        records = list(self._long_window)
        block_costs: list[float] = []
        for _start, _end, indices in _iter_blocks(records):
            cost = 0.0
            for i in indices:
                _ts, _rec, c = records[i]
                cost += c
            block_costs.append(cost)
        if len(block_costs) < 3:
            return None
        return _percentile(block_costs, 90)

    def block_info(self) -> BlockInfo | None:
        """Compute the current 5-hour block from the long-window archive.

        Block boundaries follow Anthropic's convention (matches Maciek-
        roboblog): a block starts at the top-of-hour of the first message
        and lasts exactly 5 hours. Each new message that lands after the
        previous block's end starts a new block (also anchored to its own
        top-of-hour). The "current" block is the latest one whose end is
        in the future.
        """
        if not self._long_window:
            return None

        sorted_records = list(self._long_window)  # already sorted
        blocks = list(_iter_blocks(sorted_records))
        if not blocks:
            return None
        block_start, block_end, indices = blocks[-1]

        now = datetime.now(tz=timezone.utc)
        # If the most recent block already ended, there's no active
        # block to report on. The next message will open a new one.
        if now >= block_end:
            return None

        sums = TokenSums()
        for i in indices:
            _ts, rec, cost = sorted_records[i]
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
            cost_limit=self.cost_limit,
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
