from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4096)
def decode_project_path(slug: str) -> str | None:
    """Recover the full filesystem path from a Claude Code slug.

    Returns None when no candidate path exists on disk (project moved or
    deleted). Same greedy filesystem probe as decode_project_slug — the
    only difference is what we return when a match is found.
    """
    if not slug:
        return None
    body = slug.lstrip("-")
    parts = body.split("-")
    if not parts:
        return None
    for split_at in range(len(parts) - 1, 0, -1):
        path_parts = parts[:split_at]
        name = "-".join(parts[split_at:])
        candidate = "/" + "/".join(path_parts) + "/" + name
        try:
            if Path(candidate).is_dir():
                return candidate
        except OSError:
            continue
    # No real path matched — synthesize a best-effort version using the
    # deepest existing prefix; lets the user at least navigate part-way.
    deepest = 0
    for split_at in range(1, len(parts)):
        prefix_path = "/" + "/".join(parts[:split_at])
        try:
            if Path(prefix_path).is_dir():
                deepest = split_at
            else:
                break
        except OSError:
            break
    if deepest > 0:
        return "/" + "/".join(parts[:deepest]) + "/" + "-".join(parts[deepest:])
    return None


@lru_cache(maxsize=4096)
def decode_project_slug(slug: str) -> str:
    """Recover the project name (basename of cwd) from a Claude Code slug.

    Claude Code derives the slug by replacing each '/' in the project path
    with '-'. The mapping is ambiguous when the project name itself contains
    a dash, so we probe the filesystem greedily: split on every possible
    boundary, pick the first one whose reconstructed path is a real
    directory. Falls back to the last dash-segment if no candidate matches
    (e.g. project moved or unmounted).

    Examples:
        '-home-user-Work-cc-usagemonitor' -> 'cc-usagemonitor'
        '-home-user-Work-cc-monitor'      -> 'cc-monitor'
        '-tmp-foo'                        -> 'foo'
    """
    if not slug:
        return slug
    body = slug.lstrip("-")
    parts = body.split("-")
    if not parts:
        return slug
    # Try splits from longest path-prefix down to shortest. The first one
    # whose reconstructed path exists wins; this favors interpreting later
    # dashes as part of the project name.
    for split_at in range(len(parts) - 1, 0, -1):
        path_parts = parts[:split_at]
        name = "-".join(parts[split_at:])
        candidate = "/" + "/".join(path_parts) + "/" + name
        try:
            if Path(candidate).is_dir():
                return name
        except OSError:
            continue
    # No full path matched (project moved/renamed). Find the deepest existing
    # path-prefix and return the remainder as the name — better than just
    # taking the last dash-segment.
    deepest = 0
    for split_at in range(1, len(parts)):
        prefix_path = "/" + "/".join(parts[:split_at])
        try:
            if Path(prefix_path).is_dir():
                deepest = split_at
            else:
                break
        except OSError:
            break
    if deepest > 0 and deepest < len(parts):
        return "-".join(parts[deepest:])
    return parts[-1]
