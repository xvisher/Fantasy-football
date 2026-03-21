"""
Notification module.

Sends action reports to Discord and/or Slack via incoming webhooks.
Both are optional — if the webhook URL is not configured the notification
is silently skipped.

Message format: rich embed for Discord, plain text for Slack.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import aiohttp

from src.config.settings import settings

logger = logging.getLogger(__name__)


class Notifier:
    """Send FPL bot action reports to Discord and/or Slack."""

    async def send_gw_summary(self, summary: dict) -> None:
        """
        Send a pre-GW action summary (what the bot did this GW).

        Args:
            summary: dict from FPLExecutor.run_gameweek_cycle()
        """
        message = self._format_gw_summary(summary)
        await self._dispatch(message, summary)

    async def send_post_gw_report(self, gw: int, actual_pts: int, predicted_xp: float) -> None:
        """Send a post-GW score vs prediction report."""
        diff = actual_pts - predicted_xp
        sign = "+" if diff >= 0 else ""
        message = (
            f"GW{gw} result: **{actual_pts} pts** "
            f"(predicted {predicted_xp:.1f}, {sign}{diff:.1f})\n"
            f"Model weights updated for next GW."
        )
        await self._dispatch(message, {"gw": gw})

    async def send_error(self, error: str, gw: int = None) -> None:
        """Send an error alert."""
        gw_str = f" (GW{gw})" if gw else ""
        message = f"⚠️ FPL Bot error{gw_str}: {error}"
        await self._dispatch(message, {})

    # ── Formatting ────────────────────────────────────────────────────────────

    def _format_gw_summary(self, summary: dict) -> str:
        gw = summary.get("gw", "?")
        dry = " [DRY RUN]" if summary.get("dry_run") else ""
        lines = [f"**FPL Bot — GW{gw} Actions{dry}**"]

        transfers = summary.get("transfers", [])
        if transfers:
            lines.append(f"\n**Transfers ({len(transfers)}):**")
            for t in transfers:
                hit = f" (-{t['hit_cost']}pts)" if t.get("hit_cost") else ""
                lines.append(
                    f"  OUT {t['out']} → IN {t['in']} "
                    f"(+{t['net_xp_gain']:.1f} net xP{hit})"
                )
        else:
            lines.append("\nNo transfers made this GW.")

        captain = summary.get("captain")
        vc = summary.get("vice_captain")
        if captain:
            lines.append(f"\n**Captain:** {captain}")
        if vc:
            lines.append(f"**Vice-captain:** {vc}")

        chip = summary.get("chip")
        if chip:
            lines.append(f"\n**Chip activated:** {chip.upper()}")
            reason = summary.get("chip_reason", "")
            if reason:
                lines.append(f"  Reason: {reason}")

        xp = summary.get("predicted_xp", 0)
        lines.append(f"\n**Predicted GW xP:** {xp:.1f}")
        lines.append(f"*{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*")

        return "\n".join(lines)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, message: str, data: dict) -> None:
        tasks = []
        if settings.discord_webhook_url:
            tasks.append(self._send_discord(message, data))
        if settings.slack_webhook_url:
            tasks.append(self._send_slack(message))

        if not tasks:
            logger.debug("No notification webhooks configured — skipping")
            return

        import asyncio
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Notification failed: %s", r)

    async def _send_discord(self, message: str, data: dict) -> None:
        gw = data.get("gw", "")
        transfers = data.get("transfers", [])

        # Build Discord embed
        embed: dict[str, Any] = {
            "title": f"FPL Bot — GW{gw} Update",
            "description": message,
            "color": 0x00FF85,  # FPL green
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "FPL Automation Bot"},
        }

        if transfers:
            embed["fields"] = [
                {
                    "name": "Transfers",
                    "value": "\n".join(
                        f"OUT {t['out']} → IN {t['in']}" for t in transfers
                    ),
                    "inline": False,
                }
            ]

        payload = {"embeds": [embed]}
        async with aiohttp.ClientSession() as session:
            async with session.post(settings.discord_webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    logger.warning("Discord webhook returned %d: %s", resp.status, text)
                else:
                    logger.info("Discord notification sent for GW%s", gw)

    async def _send_slack(self, message: str) -> None:
        # Slack expects plain text in "text" field (no markdown embedding)
        plain = message.replace("**", "*").replace("\n", "\n")
        payload = {"text": plain}
        async with aiohttp.ClientSession() as session:
            async with session.post(settings.slack_webhook_url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Slack webhook returned %d: %s", resp.status, text)
                else:
                    logger.info("Slack notification sent")

