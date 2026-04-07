#!/usr/bin/env python3
"""Signal Forge v2 — Dual Account Test Runner

Runs trending trader signals on BOTH accounts simultaneously.
Paper ($100K) and Live ($300) get the same signals at the same time.

Usage:
  PYTHONPATH=. python dual_test.py              # Dry run (no real orders)
  PYTHONPATH=. python dual_test.py --execute    # Place real paper orders
"""

import asyncio
import signal as sig
import argparse
from loguru import logger

from agents.dual_tracker import DualTracker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Place real orders (paper account)")
    args = parser.parse_args()

    tracker = DualTracker(dry_run=not args.execute)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("Dual test stopping")
        comparison = tracker.get_comparison()
        logger.info(f"Final comparison: paper={comparison['paper_count']} trades, live={comparison['live_count']} trades")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(sig.SIGINT, shutdown)
    loop.add_signal_handler(sig.SIGTERM, shutdown)

    try:
        loop.run_until_complete(tracker.run_forever(interval_seconds=900))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
