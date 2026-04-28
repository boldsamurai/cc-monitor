from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from .parser import (
    HookEvent,
    UsageRecord,
    parse_hook_event_line,
    parse_session_line,
    project_slug_from_path,
)
from .paths import EVENT_LOG, PROJECTS_DIR


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
        from_start: bool = False,
    ):
        self.queue = queue
        self.projects_dir = projects_dir
        self.event_log = event_log
        self.poll_interval = poll_interval
        self.from_start = from_start
        self._session_tails: dict[Path, _FileTail] = {}
        self._event_tail: _FileTail | None = None

    async def run(self) -> None:
        # Seed event log if it doesn't exist yet — tail still works once it appears.
        while True:
            await self._scan_sessions()
            await self._scan_event_log()
            await asyncio.sleep(self.poll_interval)

    async def _scan_sessions(self) -> None:
        if not self.projects_dir.exists():
            return
        # Top-level session JSONLs.
        for jsonl in self.projects_dir.glob("*/*.jsonl"):
            await self._read_session_file(jsonl)
        # Subagent JSONLs live at projects/<slug>/<session_id>/subagents/
        # — Claude Code spawns each subagent in its own file with
        # isSidechain=True and the parent session_id. Reading these is
        # how we recover real Agent attribution; without it the parent
        # JSONL only shows the tool_use stub and the resulting tool_result.
        for jsonl in self.projects_dir.glob("*/*/subagents/*.jsonl"):
            await self._read_session_file(jsonl)

    async def _read_session_file(self, path: Path) -> None:
        try:
            st = path.stat()
        except FileNotFoundError:
            return

        tail = self._session_tails.get(path)
        if tail is None or tail.inode != st.st_ino:
            start = 0 if self.from_start else st.st_size
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
        except OSError:
            return

        for raw in chunk.splitlines():
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            rec = parse_session_line(line, slug)
            if rec is not None:
                await self.queue.put(rec)

    async def _scan_event_log(self) -> None:
        if not self.event_log.exists():
            return
        try:
            st = self.event_log.stat()
        except FileNotFoundError:
            return

        if self._event_tail is None or self._event_tail.inode != st.st_ino:
            start = 0 if self.from_start else st.st_size
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
        except OSError:
            return

        for raw in chunk.splitlines():
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            ev = parse_hook_event_line(line)
            if ev is not None:
                await self.queue.put(ev)


# Re-export for type hints in callers.
QueueItem = UsageRecord | HookEvent
