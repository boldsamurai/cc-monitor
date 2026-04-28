from __future__ import annotations

import itertools
from functools import lru_cache
from pathlib import Path

# Claude Code's slug encoder replaces every non-alphanumeric path char
# with '-'. So a slug part like 'claude-cli' could decode back to any
# of 'claude-cli', 'claude_cli', or 'claude.cli'. We try all three when
# rebuilding the filesystem path.
_NAME_SEPARATORS = ("-", "_", ".")


def _name_candidates(parts: tuple[str, ...]) -> list[str]:
    """All possible name spellings for an N-part slug tail.

    For 5 parts that's 3^4 = 81 candidates — cheap. Cached by tuple
    identity so the same name parts only enumerate once per process.
    """
    if len(parts) == 1:
        return [parts[0]]
    out: list[str] = []
    for combo in itertools.product(_NAME_SEPARATORS, repeat=len(parts) - 1):
        s = parts[0]
        for sep, p in zip(combo, parts[1:]):
            s += sep + p
        out.append(s)
    return out


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
        name_parts = tuple(parts[split_at:])
        base = "/" + "/".join(path_parts)
        for name in _name_candidates(name_parts):
            candidate = f"{base}/{name}"
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

    Claude Code's slug encoder collapses every non-alphanumeric char in
    the project path to '-' (so '/', '_', '.' all map to the same byte).
    Decoding is therefore ambiguous; we probe the filesystem greedily,
    trying separator variants until something matches.

    Examples:
        '-home-user-Work-cc-usagemonitor' -> 'cc-usagemonitor'
        '-home-user-Work-claude-cli'      -> 'claude_cli'
        '-tmp-foo'                        -> 'foo'
    """
    if not slug:
        return slug
    body = slug.lstrip("-")
    parts = body.split("-")
    if not parts:
        return slug
    for split_at in range(len(parts) - 1, 0, -1):
        path_parts = parts[:split_at]
        name_parts = tuple(parts[split_at:])
        base = "/" + "/".join(path_parts)
        for name in _name_candidates(name_parts):
            candidate = f"{base}/{name}"
            try:
                if Path(candidate).is_dir():
                    return name
            except OSError:
                continue
    # No full path matched (project moved/renamed). Find the deepest
    # existing path-prefix and return the remainder as the name — better
    # than just taking the last dash-segment.
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
