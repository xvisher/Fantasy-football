"""
Understat client for xG / xA data.

Fetches per-player expected goals metrics and maps them to FPL player IDs
using name matching. Results are persisted to the xg_stats table.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp
from understatapi import UnderstatClient

from src.config.settings import settings
from src.data.database import upsert_xg_stats
from src.data.models import Player

logger = logging.getLogger(__name__)

# Name corrections: understat name → FPL web_name or second_name
# Extend this mapping as new players arrive who have name discrepancies
NAME_FIXES: dict[str, str] = {
    "Patrick Bamford": "Bamford",
    "Emiliano Martínez": "Martínez",
    "Rúben Dias": "Dias",
    "Diogo Jota": "Jota",
    "João Cancelo": "Cancelo",
}


class UnderstatFPLClient:
    """
    Fetches xG/xA data from Understat and maps it onto FPL players.

    Understat does not expose FPL IDs directly, so matching is done by
    normalised player name. The mapping is imperfect for players with
    non-ASCII names or multiple aliases; NAME_FIXES handles known mismatches.
    """

    def __init__(self, season: str) -> None:
        """
        Args:
            season: e.g. "2026" for the 2026/27 season
        """
        self.season = season

    async def fetch_and_store(self, fpl_players: list[Player], current_gw: int) -> None:
        """
        Main entry point: fetch Understat data, match to FPL players, persist.
        """
        try:
            understat_players = await self._fetch_understat_players()
        except Exception as exc:
            logger.error("Failed to fetch Understat data: %s", exc)
            return

        name_lookup = self._build_name_lookup(understat_players)
        records = []

        for fpl_player in fpl_players:
            match = self._match_player(fpl_player, name_lookup)
            if match is None:
                continue

            minutes = float(match.get("time", 0) or 0)
            if minutes < 1:
                continue

            per90 = 90.0 / minutes

            records.append({
                "player_id": fpl_player.id,
                "gw": current_gw,
                "understat_id": int(match.get("id", 0)),
                "xg_per90": float(match.get("xG", 0) or 0) * per90,
                "xa_per90": float(match.get("xA", 0) or 0) * per90,
                "npxg_per90": float(match.get("npxG", 0) or 0) * per90,
                "shots_per90": float(match.get("shots", 0) or 0) * per90,
                "key_passes_per90": float(match.get("key_passes", 0) or 0) * per90,
                "source": "understat",
            })

        if records:
            await upsert_xg_stats(records)
            logger.info("Stored xG/xA stats for %d/%d FPL players", len(records), len(fpl_players))
        else:
            logger.warning("No Understat matches found for any FPL player")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _fetch_understat_players(self) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            client = UnderstatClient(session=session)
            players = await client.league(league=settings.understat_league).get_player_data(
                season=self.season
            )
            return list(players) if players else []

    def _build_name_lookup(self, players: list[dict]) -> dict[str, dict]:
        """Build name → player-data dict for fuzzy matching."""
        lookup: dict[str, dict] = {}
        for p in players:
            raw_name = p.get("player_name", "")
            normalised = self._normalise(raw_name)
            lookup[normalised] = p
            # Also index by last word (surname) for simple matching
            parts = raw_name.split()
            if parts:
                lookup[self._normalise(parts[-1])] = p
        return lookup

    def _match_player(self, fpl_player: Player, lookup: dict[str, dict]) -> Optional[dict]:
        """Try to find a Understat match for a FPL player."""
        candidates = [
            fpl_player.web_name,
            fpl_player.second_name,
            f"{fpl_player.first_name} {fpl_player.second_name}",
        ]
        # Apply known name fixes
        full_name = f"{fpl_player.first_name} {fpl_player.second_name}"
        if full_name in NAME_FIXES:
            candidates.insert(0, NAME_FIXES[full_name])

        for name in candidates:
            key = self._normalise(name)
            if key in lookup:
                return lookup[key]
        return None

    @staticmethod
    def _normalise(name: str) -> str:
        """Lowercase, strip accents approximation, strip non-alpha."""
        import unicodedata
        name = unicodedata.normalize("NFD", name)
        name = "".join(c for c in name if unicodedata.category(c) != "Mn")
        return name.lower().strip()
