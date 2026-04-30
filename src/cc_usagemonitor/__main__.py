from __future__ import annotations

import argparse
import asyncio

from .aggregator import Aggregator
from .anthropic_usage import read_credentials
from .config import load_config
from .install_hook import ensure_installed as ensure_hook_installed
from .logger import setup_logging
from .pricing import PricingTable
from . import state as state_io
from .tailer import Tailer
from .tui import run_app


# Per-block (5h Anthropic session) cost ceilings by plan. Anthropic
# publishes these as USD limits; the older token-based variants of
# the same presets were unreliable in cache-heavy modern usage and
# have been removed (cache_read tokens dominate every prompt now, so
# a 19k-token preset reads as 13K%+ within minutes — actively
# misleading). 'auto' = P90 over the user's last 8 days of blocks.
PLAN_LIMITS = {
    "pro": {"cost": 18.0},
    "max5": {"cost": 35.0},
    "max20": {"cost": 140.0},
    "auto": {"cost": None},  # filled in dynamically via P90
    "custom": {"cost": None},
    "none": {"cost": None},
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="cc-usagemonitor")
    parser.add_argument(
        "--poll",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--plan",
        choices=list(PLAN_LIMITS.keys()),
        default=None,
        help=(
            "Anthropic plan for the 5h-block cost ceiling shown on the "
            "BlockPanel progress bar (default: read from config.json, "
            "falling back to 'auto' which derives a P90 ceiling from "
            "your own 8-day archive). Preset cost values: pro=$18, "
            "max5=$35, max20=$140 per 5h block. Use --max-5h-cost to "
            "override with a custom number."
        ),
    )
    parser.add_argument(
        "--max-5h-cost",
        type=float,
        default=None,
        help="Override 5h-block USD cost limit (wins over --plan).",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help=(
            "Disable Anthropic /api/oauth/usage polling. By default the "
            "monitor reads OAuth credentials from the system keychain or "
            "Claude Code's .credentials.json and queries the API every 60s "
            "for authoritative 5h/7d utilization. Use this flag to stay "
            "fully offline (falls back to local --plan limits or P90)."
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
    args = parser.parse_args()

    log = setup_logging(debug=args.debug)
    log.info("cc-usagemonitor starting (debug=%s)", args.debug)

    # Make sure Claude Code is wired to feed us tool_start / tool_end
    # events for Skill and Agent. Idempotent — skips silently when an
    # existing cc-usagemonitor entry is already in settings.json.
    ensure_hook_installed()

    pricing = PricingTable()
    queue: asyncio.Queue = asyncio.Queue()
    aggregator = Aggregator(pricing)

    # Layer config under CLI: explicit flags always win, otherwise fall
    # back to whatever the user persisted via the Settings screen, then
    # the historical defaults if config is empty.
    cfg = load_config()
    # Default to 'auto' — Anthropic doesn't publish per-plan token
    # limits, so static presets (pro/max5/max20) all show wildly off
    # percentages once cache_read tokens kick in. P90 of the user's
    # last 8d is the only offline metric that adapts to actual usage.
    plan_name = args.plan or cfg.get("plan", "auto")
    plan = PLAN_LIMITS.get(plan_name, PLAN_LIMITS["none"])
    aggregator.cost_limit = args.max_5h_cost or plan["cost"]

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

    # 'auto' plan needs the 8-day archive populated to compute P90.
    # Replay mode is always on (see Tailer below), so this flag is
    # purely a signal to recompute P90 periodically as the archive
    # rolls forward in time.
    auto_limits = plan_name == "auto" and not use_api

    tailer = Tailer(queue, poll_interval=args.poll)

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
