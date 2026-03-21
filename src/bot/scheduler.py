"""
Scheduler module.

Uses APScheduler to run the bot automatically before each GW deadline.
Deadlines are fetched dynamically from the FPL API at startup and
refreshed weekly (because fixtures get rescheduled mid-season).

Jobs:
  - per-GW pre-deadline job: runs N minutes before each GW deadline
  - daily 02:00 BST: price/injury data refresh
  - post-GW: actual score fetch + learning cycle
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from src.bot.executor import FPLExecutor
from src.bot.notifier import Notifier
from src.config.settings import settings
from src.data.database import get_upcoming_deadlines, init_db, upsert_gameweeks
from src.data.fpl_client import FPLClient

logger = logging.getLogger(__name__)

TZ_LONDON = pytz.timezone("Europe/London")


class FPLScheduler:
    """
    Manages all scheduled FPL bot jobs.

    Usage:
        scheduler = FPLScheduler()
        await scheduler.start()
        # runs forever until Ctrl+C or shutdown()
        await scheduler.shutdown()
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=TZ_LONDON)
        self._executor = FPLExecutor()
        self._notifier = Notifier()
        self._running = False

    async def start(self) -> None:
        """Initialise DB, schedule all jobs, start the scheduler."""
        await init_db()
        logger.info("Database ready. Fetching GW deadlines...")

        await self._refresh_deadlines()
        await self._schedule_all_gw_jobs()

        # Daily refresh job at 02:00 London time
        self._scheduler.add_job(
            self._daily_data_refresh,
            trigger=CronTrigger(hour=2, minute=0, timezone=TZ_LONDON),
            id="daily_refresh",
            replace_existing=True,
            name="Daily data refresh (prices, injuries)",
        )

        # Weekly deadline re-sync (fixtures can be rescheduled mid-season)
        self._scheduler.add_job(
            self._refresh_and_reschedule,
            trigger=CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=TZ_LONDON),
            id="weekly_deadline_sync",
            replace_existing=True,
            name="Weekly GW deadline re-sync",
        )

        self._scheduler.start()
        self._running = True
        logger.info("FPL Bot scheduler started. Waiting for GW deadlines...")
        logger.info("Scheduled jobs:")
        for job in self._scheduler.get_jobs():
            logger.info("  • %s (next: %s)", job.name, job.next_run_time)

    async def shutdown(self) -> None:
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Scheduler shut down.")

    # ── Job handlers ──────────────────────────────────────────────────────────

    async def _run_gw_job(self, gw: int) -> None:
        """Pre-deadline job: run full analysis and apply transfers."""
        logger.info("=== GW%d pre-deadline job triggered ===", gw)
        try:
            summary = await self._executor.run_gameweek_cycle(target_gw=gw)
            await self._notifier.send_gw_summary(summary)
        except Exception as exc:
            logger.exception("GW%d job failed: %s", gw, exc)
            await self._notifier.send_error(str(exc), gw=gw)

    async def _run_post_gw_job(self, completed_gw: int) -> None:
        """Post-GW job: fetch actual scores, run learning cycle."""
        logger.info("=== GW%d post-GW update triggered ===", completed_gw)
        try:
            await self._executor.run_post_gw_update(completed_gw)
        except Exception as exc:
            logger.exception("Post-GW%d job failed: %s", completed_gw, exc)

    async def _daily_data_refresh(self) -> None:
        """Daily price and injury data refresh (no transfers)."""
        logger.info("Daily data refresh triggered")
        try:
            async with FPLClient() as client:
                bootstrap = await client.get_bootstrap()
                # Re-persist player data with latest prices and statuses
                current_gw = bootstrap.next_gw
                if current_gw:
                    await upsert_gameweeks([
                        {
                            "id": ev.id,
                            "name": ev.name,
                            "deadline_time": ev.deadline_time.isoformat(),
                            "finished": int(ev.finished),
                            "is_current": int(ev.is_current),
                            "is_next": int(ev.is_next),
                            "average_score": ev.average_entry_score,
                            "highest_score": ev.highest_score,
                        }
                        for ev in bootstrap.events
                    ])
                    logger.info("Daily refresh: updated %d players, %d GWs", len(bootstrap.elements), len(bootstrap.events))
        except Exception as exc:
            logger.warning("Daily refresh failed: %s", exc)

    async def _refresh_and_reschedule(self) -> None:
        """Re-fetch deadlines and reschedule any that have moved."""
        logger.info("Weekly deadline re-sync triggered")
        await self._refresh_deadlines()
        await self._schedule_all_gw_jobs()

    # ── Scheduling logic ──────────────────────────────────────────────────────

    async def _refresh_deadlines(self) -> None:
        """Fetch latest GW deadlines from FPL API and persist to DB."""
        try:
            async with FPLClient() as client:
                bootstrap = await client.get_bootstrap()
            await upsert_gameweeks([
                {
                    "id": ev.id,
                    "name": ev.name,
                    "deadline_time": ev.deadline_time.isoformat(),
                    "finished": int(ev.finished),
                    "is_current": int(ev.is_current),
                    "is_next": int(ev.is_next),
                    "average_score": ev.average_entry_score,
                    "highest_score": ev.highest_score,
                }
                for ev in bootstrap.events
            ])
            logger.info("Refreshed %d GW deadlines from FPL API", len(bootstrap.events))
        except Exception as exc:
            logger.error("Failed to refresh deadlines from API: %s", exc)
            raise

    async def _schedule_all_gw_jobs(self) -> None:
        """Schedule pre-deadline and post-GW jobs for all upcoming GWs."""
        upcoming = await get_upcoming_deadlines()
        now = datetime.now(tz=timezone.utc)
        scheduled_count = 0

        for gw_row in upcoming:
            gw_id = gw_row["id"]
            deadline_str = gw_row["deadline_time"]

            try:
                deadline = datetime.fromisoformat(deadline_str)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("Could not parse deadline for GW%d: %s", gw_id, deadline_str)
                continue

            # Pre-deadline job: N minutes before deadline
            run_at = deadline - timedelta(minutes=settings.minutes_before_deadline)
            if run_at > now:
                job_id = f"gw_{gw_id}_pre_deadline"
                self._scheduler.add_job(
                    self._run_gw_job,
                    trigger=DateTrigger(run_date=run_at),
                    args=[gw_id],
                    id=job_id,
                    replace_existing=True,
                    name=f"GW{gw_id} pre-deadline ({run_at.strftime('%Y-%m-%d %H:%M UTC')})",
                )
                scheduled_count += 1
                logger.debug("Scheduled GW%d job at %s", gw_id, run_at)

            # Post-GW job: 2 hours after deadline (GW should be fully live by then)
            post_run_at = deadline + timedelta(hours=26)  # roughly after last match finishes
            if post_run_at > now:
                post_job_id = f"gw_{gw_id}_post"
                self._scheduler.add_job(
                    self._run_post_gw_job,
                    trigger=DateTrigger(run_date=post_run_at),
                    args=[gw_id],
                    id=post_job_id,
                    replace_existing=True,
                    name=f"GW{gw_id} post-GW update",
                )

        logger.info("Scheduled %d pre-deadline jobs for upcoming GWs", scheduled_count)
