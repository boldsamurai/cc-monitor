from __future__ import annotations

import argparse
import asyncio

from .aggregator import Aggregator
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
        "--from-start",
        action="store_true",
        help="Replay all existing JSONL content (default: only tail new lines).",
    )
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
    plan_name = args.plan or cfg.get("plan", "none")
    plan = PLAN_LIMITS.get(plan_name, PLAN_LIMITS["none"])
    aggregator.token_limit = args.max_5h_tokens or plan["tokens"]
    aggregator.cost_limit = args.max_5h_cost or plan["cost"]

    # --plan auto needs the historical archive populated to compute P90,
    # so silently turn on --from-start and recompute periodically.
    auto_limits = plan_name == "auto"
    if auto_limits and not args.from_start:
        args.from_start = True

    # CLI --no-api always disables; otherwise honor the persisted
    # Settings toggle (defaults to enabled).
    use_api = False if args.no_api else cfg.get("use_api", True)

    tailer = Tailer(queue, poll_interval=args.poll, from_start=args.from_start)

    run_app(
        aggregator, tailer, queue,
        auto_limits=auto_limits,
        use_api=use_api,
    )


if __name__ == "__main__":
    main()
