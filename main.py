"""
FPL Automation Bot — entry point.

Starts the APScheduler-based bot that runs autonomously:
  - Fetches GW deadlines from FPL API on startup
  - Runs full analysis + transfers 60 minutes before each deadline
  - Updates model weights after each GW resolves (self-improvement)
  - Sends Discord/Slack notifications on every action

Run modes:
  python main.py               → start the scheduler (normal operation)
  python main.py --once <gw>   → run one GW cycle immediately and exit (testing)
  python main.py --dry-run     → override DRY_RUN=true for this invocation
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fpl_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("fpl_bot")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FPL Automation Bot")
    parser.add_argument(
        "--once",
        type=int,
        metavar="GW",
        help="Run a single GW cycle for the given gameweek number and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override DRY_RUN=true for this invocation (no transfers submitted)",
    )
    parser.add_argument(
        "--post-gw",
        type=int,
        metavar="GW",
        help="Run post-GW update (score fetch + learning) for the given GW and exit",
    )
    return parser.parse_args()


async def run_once(gw: int) -> None:
    """Run a single GW optimisation cycle and exit."""
    from src.bot.executor import FPLExecutor
    from src.bot.notifier import Notifier

    executor = FPLExecutor()
    notifier = Notifier()
    logger.info("Running single GW%d cycle...", gw)
    summary = await executor.run_gameweek_cycle(target_gw=gw)
    await notifier.send_gw_summary(summary)
    logger.info("GW%d cycle complete: %s", gw, summary)


async def run_post_gw(gw: int) -> None:
    """Run post-GW update for a specific GW."""
    from src.bot.executor import FPLExecutor

    executor = FPLExecutor()
    logger.info("Running post-GW%d update...", gw)
    await executor.run_post_gw_update(completed_gw=gw)
    logger.info("Post-GW%d update complete", gw)


async def run_scheduler() -> None:
    """Start the full scheduler and run indefinitely."""
    from src.bot.scheduler import FPLScheduler

    scheduler = FPLScheduler()
    try:
        await scheduler.start()
        # Keep alive
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received")
    finally:
        await scheduler.shutdown()
        logger.info("FPL Bot stopped.")


def validate_env() -> None:
    """Check required environment variables are set."""
    from src.config.settings import settings

    missing = []
    if not settings.fpl_email:
        missing.append("FPL_EMAIL")
    if not settings.fpl_password:
        missing.append("FPL_PASSWORD")
    if not settings.fpl_team_id:
        missing.append("FPL_TEAM_ID")

    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Copy .env.example to .env and fill in your credentials.",
            ", ".join(missing),
        )
        sys.exit(1)

    if settings.dry_run:
        logger.warning("*** DRY RUN MODE — no transfers will be submitted ***")


def main() -> None:
    args = parse_args()

    # Override dry_run from CLI
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    # Re-import settings after potential env override
    from src.config.settings import settings  # noqa: F401

    validate_env()

    logger.info("FPL Automation Bot starting (dry_run=%s)", settings.dry_run)

    if args.once:
        asyncio.run(run_once(args.once))
    elif args.post_gw:
        asyncio.run(run_post_gw(args.post_gw))
    else:
        asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
