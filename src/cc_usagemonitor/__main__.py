from __future__ import annotations

import argparse
import asyncio

from .aggregator import Aggregator
from .pricing import PricingTable
from .tailer import Tailer
from .tui import run_app


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
    args = parser.parse_args()

    pricing = PricingTable()
    queue: asyncio.Queue = asyncio.Queue()
    aggregator = Aggregator(pricing)
    tailer = Tailer(queue, poll_interval=args.poll, from_start=args.from_start)

    run_app(aggregator, tailer, queue)


if __name__ == "__main__":
    main()
