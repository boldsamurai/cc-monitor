"""Bulk export of aggregator state to CSV / JSON.

Triggered from Settings → Export. Writes raw numeric values (no $-signs,
no K/M shortening) so pandas / Excel can sort and sum the columns
directly. Three logical tables — sessions, projects, models — get one
file each in CSV mode, or one combined object in JSON mode.

Files land in ~/.cache/cc-usagemonitor/exports/ (XDG_CACHE_HOME aware).
File names embed an ISO timestamp so successive exports don't overwrite
each other.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .aggregator import Aggregator, TokenSums
from .parser import humanize_model_name
from .project_slug import decode_project_path


SESSION_FIELDS = [
    "session_id",
    "project_slug",
    "cwd",
    "first_seen",
    "last_seen",
    "turns",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "total_tokens",
    "cost_usd",
    "models",
]

PROJECT_FIELDS = [
    "project_slug",
    "cwd",
    "exists",
    "sessions_count",
    "first_seen",
    "last_seen",
    "turns",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "total_tokens",
    "cost_usd",
    "cost_last_7d",
    "models",
]

MODEL_FIELDS = [
    "model_id",
    "model_name",
    "turns",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "total_tokens",
    "cost_usd",
    "cost_per_turn",
]


@dataclass
class ExportResult:
    """Files produced by a single export call.

    `paths` is in deterministic order so callers can show 'sessions →
    projects → models' in the toast even when the OS returns directory
    entries in a different order.
    """

    directory: Path
    paths: list[Path]
    fmt: str  # 'csv' or 'json'


def export_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "cc-usagemonitor" / "exports"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _session_rows(agg: Aggregator) -> list[dict]:
    rows: list[dict] = []
    for sess in agg.sessions.values():
        s = sess.sums
        rows.append({
            "session_id": sess.session_id,
            "project_slug": sess.project_slug,
            "cwd": sess.cwd or "",
            "first_seen": _iso(sess.first_seen),
            "last_seen": _iso(sess.last_seen),
            "turns": s.turns,
            "input_tokens": s.input,
            "output_tokens": s.output,
            "cache_read_tokens": s.cache_read,
            "cache_write_5m_tokens": s.cache_write_5m,
            "cache_write_1h_tokens": s.cache_write_1h,
            "total_tokens": s.total_tokens,
            "cost_usd": round(s.cost_usd, 6),
            "models": ";".join(sorted(sess.by_model)),
        })
    rows.sort(
        key=lambda r: (r["last_seen"] or "", r["session_id"]),
        reverse=True,
    )
    return rows


def _project_rows(agg: Aggregator) -> list[dict]:
    # Mirrors the rollup in tui._refresh_projects_table but emits raw
    # numbers instead of human-formatted strings. cost_last_7d derived
    # from the same _long_window archive the UI uses.
    @dataclass
    class _Acc:
        sessions: int = 0
        first_seen: datetime | None = None
        last_seen: datetime | None = None
        cwd: str | None = None
        models: set = None  # type: ignore[assignment]
        sums: TokenSums = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            self.models = set()
            self.sums = TokenSums()

    by_slug: dict[str, _Acc] = defaultdict(_Acc)
    for sess in agg.sessions.values():
        e = by_slug[sess.project_slug]
        e.sessions += 1
        e.sums.input += sess.sums.input
        e.sums.output += sess.sums.output
        e.sums.cache_read += sess.sums.cache_read
        e.sums.cache_write_5m += sess.sums.cache_write_5m
        e.sums.cache_write_1h += sess.sums.cache_write_1h
        e.sums.cost_usd += sess.sums.cost_usd
        e.sums.turns += sess.sums.turns
        e.models.update(sess.by_model)
        if e.cwd is None and sess.cwd:
            e.cwd = sess.cwd
        for ts, kind in (
            (sess.first_seen, "first"), (sess.last_seen, "last"),
        ):
            if ts is None:
                continue
            ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if kind == "first":
                if e.first_seen is None or ts_aware < e.first_seen:
                    e.first_seen = ts_aware
            else:
                if e.last_seen is None or ts_aware > e.last_seen:
                    e.last_seen = ts_aware

    seven_days_ago = datetime.now(tz=timezone.utc).timestamp() - 7 * 86400
    last_7d: dict[str, float] = defaultdict(float)
    for ts, rec, cost in agg._long_window:
        if ts.timestamp() < seven_days_ago:
            continue
        sess = agg.sessions.get(rec.session_id)
        if sess is None:
            continue
        last_7d[sess.project_slug] += cost

    rows: list[dict] = []
    for slug, e in by_slug.items():
        cwd = e.cwd or decode_project_path(slug) or ""
        exists = bool(cwd) and Path(cwd).is_dir()
        rows.append({
            "project_slug": slug,
            "cwd": cwd,
            "exists": exists,
            "sessions_count": e.sessions,
            "first_seen": _iso(e.first_seen),
            "last_seen": _iso(e.last_seen),
            "turns": e.sums.turns,
            "input_tokens": e.sums.input,
            "output_tokens": e.sums.output,
            "cache_read_tokens": e.sums.cache_read,
            "cache_write_5m_tokens": e.sums.cache_write_5m,
            "cache_write_1h_tokens": e.sums.cache_write_1h,
            "total_tokens": e.sums.total_tokens,
            "cost_usd": round(e.sums.cost_usd, 6),
            "cost_last_7d": round(last_7d.get(slug, 0.0), 6),
            "models": ";".join(sorted(e.models)),
        })
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


def _model_rows(agg: Aggregator) -> list[dict]:
    per_model: dict[str, TokenSums] = defaultdict(TokenSums)
    for sess in agg.sessions.values():
        for model, sums in sess.by_model.items():
            m = per_model[model]
            m.input += sums.input
            m.output += sums.output
            m.cache_read += sums.cache_read
            m.cache_write_5m += sums.cache_write_5m
            m.cache_write_1h += sums.cache_write_1h
            m.cost_usd += sums.cost_usd
            m.turns += sums.turns
    rows: list[dict] = []
    for model, s in per_model.items():
        per_turn = s.cost_usd / s.turns if s.turns else 0.0
        rows.append({
            "model_id": model,
            "model_name": humanize_model_name(model) or model,
            "turns": s.turns,
            "input_tokens": s.input,
            "output_tokens": s.output,
            "cache_read_tokens": s.cache_read,
            "cache_write_5m_tokens": s.cache_write_5m,
            "cache_write_1h_tokens": s.cache_write_1h,
            "total_tokens": s.total_tokens,
            "cost_usd": round(s.cost_usd, 6),
            "cost_per_turn": round(per_turn, 6),
        })
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _timestamp_slug() -> str:
    # Filename-safe ISO: 2026-04-30T12-34-56 (colons would break Windows).
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def export_csv(agg: Aggregator) -> ExportResult:
    """Write three CSVs (sessions / projects / models) to the cache dir."""
    out = export_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp_slug()
    paths = []
    for kind, fields, rows in (
        ("sessions", SESSION_FIELDS, _session_rows(agg)),
        ("projects", PROJECT_FIELDS, _project_rows(agg)),
        ("models", MODEL_FIELDS, _model_rows(agg)),
    ):
        path = out / f"{kind}_{stamp}.csv"
        _write_csv(path, fields, rows)
        paths.append(path)
    return ExportResult(directory=out, paths=paths, fmt="csv")


def export_json(agg: Aggregator) -> ExportResult:
    """Write a single combined JSON document with all three tables."""
    out = export_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp_slug()
    path = out / f"export_{stamp}.json"
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "sessions": _session_rows(agg),
        "projects": _project_rows(agg),
        "models": _model_rows(agg),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return ExportResult(directory=out, paths=[path], fmt="json")
