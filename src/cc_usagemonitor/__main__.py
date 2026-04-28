from __future__ import annotations

import argparse
import asyncio

from .aggregator import Aggregator
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
        default="none",
        help=(
            "Anthropic plan for 5h block limits (default: none — BlockPanel "
            "shows raw numbers without progress bars). Plan limits match "
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
    args = parser.parse_args()

    pricing = PricingTable()
    queue: asyncio.Queue = asyncio.Queue()
    aggregator = Aggregator(pricing)

    plan = PLAN_LIMITS[args.plan]
    aggregator.token_limit = args.max_5h_tokens or plan["tokens"]
    aggregator.cost_limit = args.max_5h_cost or plan["cost"]

    # --plan auto needs the historical archive populated to compute P90,
    # so silently turn on --from-start and recompute periodically.
    auto_limits = args.plan == "auto"
    if auto_limits and not args.from_start:
        args.from_start = True

    tailer = Tailer(queue, poll_interval=args.poll, from_start=args.from_start)

    run_app(aggregator, tailer, queue, auto_limits=auto_limits)


if __name__ == "__main__":
    main()
