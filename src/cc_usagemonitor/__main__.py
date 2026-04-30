from __future__ import annotations

import argparse
import asyncio

from .aggregator import Aggregator
from .anthropic_usage import read_credentials
from .config import load_config
from .install_hook import ensure_installed as ensure_hook_installed
from .logger import setup_logging
from .pricing import PricingTable
from .tailer import Tailer
from .tui import run_app


# Approximate per-block (5h Anthropic session) limits per Claude plan.
# Match the values from Maciek-roboblog/Claude-Code-Usage-Monitor; Anthropic
# changes plan terms periodically, so users can override via flags below.
PLAN_LIMITS = {
    "pro": {"tokens": 19_000, "cost": 18.0},
    "max5": {"tokens": 88_000, "cost": 35.0},
    "max20": {"tokens": 220_000, "cost": 140.0},
    "auto": {"tokens": None, "cost": None},  # filled in dynamically via P90
    "custom": {"tokens": None, "cost": None},
    "none": {"tokens": None, "cost": None},
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
            "Anthropic plan for 5h block limits (default: read from "
            "config.json, falling back to 'none' which shows raw "
            "numbers without progress bars). Plan limits match "
            "Maciek-roboblog/Claude-Code-Usage-Monitor: pro=19k/$18, "
            "max5=88k/$35, max20=220k/$140 per 5h session. Heavy users will "
            "blow past these — use --max-5h-tokens / --max-5h-cost overrides."
        ),
    )
    parser.add_argument(
        "--max-5h-tokens",
        type=int,
        default=None,
        help="Override 5h-block token limit.",
    )
    parser.add_argument(
        "--max-5h-cost",
        type=float,
        default=None,
        help="Override 5h-block USD cost limit.",
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
    aggregator.token_limit = args.max_5h_tokens or plan["tokens"]
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

    # Replay every existing JSONL on startup. Tail-only mode (the old
    # default) starts with empty tables and accumulates state only
    # while the app is running, which is useless for almost everyone:
    # closing the terminal wipes the in-memory archive, so the next
    # launch sees nothing again. Replay is slightly slower at startup
    # but presents the user's actual history immediately.
    tailer = Tailer(queue, poll_interval=args.poll, from_start=True)

    run_app(
        aggregator, tailer, queue,
        auto_limits=auto_limits,
        use_api=use_api,
        has_oauth=has_oauth,
    )


if __name__ == "__main__":
    main()
