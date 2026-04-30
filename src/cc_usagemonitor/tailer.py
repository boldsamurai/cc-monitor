from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger
from .parser import (
    HookEvent,
    UsageRecord,
    parse_hook_event_line,
    parse_session_line,
    project_slug_from_path,
)
from .paths import EVENT_LOG, PROJECTS_DIR

log = get_logger(__name__)


@dataclass
class _FileTail:
    path: Path
    pos: int
    inode: int


class Tailer:
    """Polling-based tail-follow for session JSONLs and the hook event log.

    Polling (vs inotify) keeps the implementation simple and works fine here:
    Claude Code writes lines on each turn, not at sub-second cadence.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        projects_dir: Path = PROJECTS_DIR,
        event_log: Path = EVENT_LOG,
        poll_interval: float = 0.5,
    ):
        self.queue = queue
        self.projects_dir = projects_dir
        self.event_log = event_log
        self.poll_interval = poll_interval
        self._session_tails: dict[Path, _FileTail] = {}
        self._event_tail: _FileTail | None = None
        # Flips True after the first poll iteration completes — the UI
        # uses this to hide the 'replaying historical sessions' banner
        # once the initial archive replay is done. Doesn't reset on
        # Force re-scan (the in-memory state was just cleared, but the
        # banner already came down on first launch and hiding it again
        # would just confuse).
        self.initial_scan_done: bool = False

    def snapshot(self) -> dict:
        """Per-file tail offsets for cross-run persistence. Stored as a
        list of (str(path), pos, inode) tuples so the snapshot doesn't
        carry Path objects (the dataclass works fine but the simpler
        shape decouples from internal types).
        """
        return {
            "session_tails": [
                (str(t.path), t.pos, t.inode)
                for t in self._session_tails.values()
            ],
            "event_tail": (
                None if self._event_tail is None
                else (
                    str(self._event_tail.path),
                    self._event_tail.pos,
                    self._event_tail.inode,
                )
            ),
        }

    def restore(self, data: dict) -> None:
        """Restore offsets saved by ``snapshot()``. Files whose inode
        no longer matches (rotated / replaced) are silently re-tailed
        from byte 0 by the regular polling code — we don't validate
        here, the next ``_read_session_file`` call notices and resets.
        Sets from_start=False so the next sweep tails forward instead
        of replaying from offset 0."""
        for path_str, pos, inode in data.get("session_tails", []):
            p = Path(path_str)
            self._session_tails[p] = _FileTail(
                path=p, pos=pos, inode=inode,
            )
        ev = data.get("event_tail")
        if ev is not None:
            path_str, pos, inode = ev
            self._event_tail = _FileTail(
                path=Path(path_str), pos=pos, inode=inode,
            )
        # Treat the restored state as a 'completed initial sweep' so
        # the LoadingScreen modal in the TUI can come down immediately
        # (the heavy work was done on the previous run; the next poll
        # tick is just a cheap incremental tail forward).
        self.initial_scan_done = True
        log.info(
            "tailer restored: %d session offsets, event_tail=%s",
            len(self._session_tails),
            "yes" if self._event_tail is not None else "no",
        )

    def reset_tails(self) -> None:
        """Forget all per-file tail positions and switch to from-start
        mode so the next polling tick re-reads every JSONL from byte 0.
        Pairs with Aggregator.reset_state() for the Settings
        'Force re-scan' action — call both together to avoid double-
        counting records that are already in the in-memory archive.
        """
        self._session_tails.clear()
        self._event_tail = None
        log.info("tailer reset: re-scanning from start")

    async def run(self) -> None:
        # Seed event log if it doesn't exist yet — tail still works once it appears.
        log.info(
            "tailer starting: projects=%s event_log=%s tracked_files=%d",
            self.projects_dir, self.event_log, len(self._session_tails),
        )
        # Yield once so any pending UI work scheduled at on_mount time
        # (notably the LoadingScreen modal) gets render time before we
        # start blocking the event loop with the first replay sweep.
        await asyncio.sleep(0)
        while True:
            await self._scan_sessions()
            await self._scan_event_log()
            # Mark replay as complete after the very first sweep so the
            # UI loading banner can come down. Subsequent iterations
            # are incremental tails, no longer 'loading'.
            if not self.initial_scan_done:
                self.initial_scan_done = True
                log.info("tailer initial replay complete")
            await asyncio.sleep(self.poll_interval)

    async def _scan_sessions(self) -> None:
        if not self.projects_dir.exists():
            return
        # Top-level session JSONLs. _read_session_file does synchronous
        # file I/O wrapped in async — we explicitly yield to the event
        # loop after each file so the LoadingScreen modal (and any
        # other concurrent UI work) gets render time during the
        # initial replay sweep across many JSONLs.
        for jsonl in self.projects_dir.glob("*/*.jsonl"):
            await self._read_session_file(jsonl)
            await asyncio.sleep(0)
        # Subagent JSONLs live at projects/<slug>/<session_id>/subagents/
        # — Claude Code spawns each subagent in its own file with
        # isSidechain=True and the parent session_id. Reading these is
        # how we recover real Agent attribution; without it the parent
        # JSONL only shows the tool_use stub and the resulting tool_result.
        for jsonl in self.projects_dir.glob("*/*/subagents/*.jsonl"):
            await self._read_session_file(jsonl)
            await asyncio.sleep(0)

    async def _read_session_file(self, path: Path) -> None:
        try:
            st = path.stat()
        except FileNotFoundError:
            return

        tail = self._session_tails.get(path)
        if tail is None or tail.inode != st.st_ino:
            # Always read fresh (untracked or rotated) files from
            # byte 0 — historical replay is the desired default and
            # the snapshot mechanism handles 'don't re-read what you
            # already saw' via persisted offsets.
            start = 0
            tail = _FileTail(path=path, pos=start, inode=st.st_ino)
            self._session_tails[path] = tail

        if st.st_size < tail.pos:
            # File truncated/rotated.
            tail.pos = 0

        if st.st_size == tail.pos:
            return

        slug = project_slug_from_path(path)
        try:
            with path.open("rb") as f:
                f.seek(tail.pos)
                chunk = f.read(st.st_size - tail.pos)
                tail.pos = f.tell()
        except OSError as e:
            log.warning("failed to read %s: %s", path, e)
            return

        n_records = 0
        for raw in chunk.splitlines():
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception as e:
                log.debug("decode error in %s: %s", path, e)
                continue
            rec = parse_session_line(line, slug)
            if rec is not None:
                await self.queue.put(rec)
                n_records += 1
        if n_records:
            log.debug("ingested %d records from %s", n_records, path.name)

    async def _scan_event_log(self) -> None:
        if not self.event_log.exists():
            return
        try:
            st = self.event_log.stat()
        except FileNotFoundError:
            return

        if self._event_tail is None or self._event_tail.inode != st.st_ino:
            # Always read fresh (untracked or rotated) files from
            # byte 0 — historical replay is the desired default and
            # the snapshot mechanism handles 'don't re-read what you
            # already saw' via persisted offsets.
            start = 0
            self._event_tail = _FileTail(path=self.event_log, pos=start, inode=st.st_ino)

        tail = self._event_tail
        if st.st_size < tail.pos:
            tail.pos = 0
        if st.st_size == tail.pos:
            return

        try:
            with self.event_log.open("rb") as f:
                f.seek(tail.pos)
                chunk = f.read(st.st_size - tail.pos)
                tail.pos = f.tell()
        except OSError as e:
            log.warning("failed to read event log: %s", e)
            return

        n_events = 0
        for raw in chunk.splitlines():
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            ev = parse_hook_event_line(line)
            if ev is not None:
                await self.queue.put(ev)
                n_events += 1
        if n_events:
            log.debug("ingested %d hook events", n_events)


# Re-export for type hints in callers.
QueueItem = UsageRecord | HookEvent
