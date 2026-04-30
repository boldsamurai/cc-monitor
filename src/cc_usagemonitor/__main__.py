from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .aggregator import Aggregator
from .anthropic_usage import read_credentials
from .install_hook import ensure_installed as ensure_hook_installed
from .logger import setup_logging
from .pricing import PricingTable
from . import state as state_io
from .tailer import Tailer
from .tui import run_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="cc-usagemonitor")
    parser.add_argument(
        "--poll",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--max-5h-cost",
        type=float,
        default=None,
        help=(
            "Override the 5h-block USD cost ceiling rendered on the "
            "BlockPanel progress bar. Without this, --no-api mode "
            "auto-derives a P90 ceiling from your last 8 days of "
            "blocks; API mode pulls the authoritative value from "
            "Anthropic and ignores this flag."
        ),
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help=(
            "Disable Anthropic /api/oauth/usage polling. By default the "
            "monitor reads OAuth credentials from the system keychain or "
            "Claude Code's .credentials.json and queries the API every 60s "
            "for authoritative 5h/7d utilization. Use this flag to stay "
            "fully offline (falls back to a P90-derived cost ceiling)."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Enable DEBUG-level logging to "
            "~/.cache/cc-usagemonitor/usagemonitor.log. Useful when "
            "tracking down ingest/parser issues; the log rotates at 10MB."
        ),
    )
    parser.add_argument(
        "--reinstall-hook",
        action="store_true",
        help=(
            "Re-run the Claude Code hook installer and exit without "
            "launching the TUI. Idempotent — safe to run from "
            "provisioning scripts."
        ),
    )
    parser.add_argument(
        "--rescan",
        action="store_true",
        help=(
            "Discard the cached state snapshot before launch so the "
            "next run replays every JSONL from scratch. CLI equivalent "
            "of Settings → Force re-scan."
        ),
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help=(
            "Override the directory the tailer reads JSONL session "
            "logs from. Default: ~/.claude/projects. Useful for "
            "running against a synthetic dataset (debugging, demos, "
            "screenshots) without touching real session data."
        ),
    )
    args = parser.parse_args()

    log = setup_logging(debug=args.debug)
    log.info("cc-usagemonitor starting (debug=%s)", args.debug)

    # One-shot maintenance commands run before any TUI setup. Each
    # short-circuits with sys.exit so we don't pay startup cost when
    # the user only wanted the side effect.
    if args.reinstall_hook:
        ensure_hook_installed()
        print("Hook installer ran (idempotent).")
        sys.exit(0)
    if args.rescan:
        state_io.discard()
        log.info("CLI --rescan: snapshot discarded; next launch will full-replay")

    # Make sure Claude Code is wired to feed us tool_start / tool_end
    # events for Skill and Agent. Idempotent — skips silently when an
    # existing cc-usagemonitor entry is already in settings.json.
    ensure_hook_installed()

    pricing = PricingTable()
    queue: asyncio.Queue = asyncio.Queue()
    aggregator = Aggregator(pricing)

    # Cost ceiling: explicit --max-5h-cost wins; otherwise we let the
    # auto-P90 worker fill it in once the archive is populated (only
    # in --no-api mode — API mode trusts Anthropic's published values
    # and renders progress bars from those instead).
    aggregator.cost_limit = args.max_5h_cost

    # Auto-detect OAuth credentials. Three resulting modes:
    #   has_oauth + use_api   → poll /api/oauth/usage, authoritative view
    #   has_oauth + --no-api  → local view ('user explicitly opted out')
    #   no oauth              → pay-as-you-go view (API-key user; the
    #                            usage endpoint rejects API-key tokens)
    has_oauth = read_credentials() is not None
    if args.no_api:
        use_api = False
    else:
        # Auto-detect: poll only when we actually have credentials that
        # the endpoint accepts. API-key users would just get 401 spam.
        use_api = has_oauth

    # P90 auto-derivation only matters when we don't have authoritative
    # API data and the user didn't pin a custom number. The recompute
    # worker also runs every 30s so the ceiling rolls forward as the
    # 8-day archive evolves.
    auto_limits = not use_api and args.max_5h_cost is None

    tailer_kwargs = {"poll_interval": args.poll}
    if args.projects_dir is not None:
        tailer_kwargs["projects_dir"] = args.projects_dir
    tailer = Tailer(queue, **tailer_kwargs)

    # Cross-run snapshot: if the previous quit pickled state, restore
    # both the aggregator's in-memory archive and the tailer's per-file
    # offsets. The next polling tick then only reads JSONL bytes that
    # landed since the last save — turning multi-second cold starts
    # into sub-second warm starts. A missing / corrupt / version-
    # mismatched snapshot falls through to a full replay.
    snap = state_io.load()
    if snap is not None:
        aggregator.restore(snap.aggregator)
        tailer.restore(snap.tailer)

    run_app(
        aggregator, tailer, queue,
        auto_limits=auto_limits,
        use_api=use_api,
        has_oauth=has_oauth,
    )


if __name__ == "__main__":
    main()
