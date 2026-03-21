"""
Async FPL API client.

Wraps the official (unofficial) FPL API and the `fpl` Python library.
Handles authentication, rate limiting, and data normalisation.

Endpoints used:
  GET  /bootstrap-static/              → all players, teams, GWs
  GET  /fixtures/                       → all season fixtures
  GET  /element-summary/{id}/           → player GW history
  GET  /entry/{team_id}/                → manager info
  GET  /entry/{team_id}/event/{gw}/picks/ → team for GW
  GET  /my-team/{team_id}/              → current team (auth required)
  GET  /event/{gw}/live/                → live/final GW scores (auth required)
  POST /transfers/                      → submit transfers (auth required)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import aiohttp
from fpl import FPL

from src.config.settings import settings
from src.data.models import (
    BootstrapData,
    Fixture,
    Gameweek,
    MyTeam,
    Pick,
    Player,
    PlayerGameweekHistory,
    Team,
    TransferInfo,
    ChipStatus,
    ChipType,
)

logger = logging.getLogger(__name__)

BASE_URL = settings.fpl_api_base


class FPLClient:
    """
    Async context manager for all FPL API interactions.

    Usage:
        async with FPLClient() as client:
            bootstrap = await client.get_bootstrap()
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._fpl: Optional[FPL] = None
        self._authenticated = False

    async def __aenter__(self) -> "FPLClient":
        self._session = aiohttp.ClientSession()
        self._fpl = FPL(self._session)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    # ── Authentication ────────────────────────────────────────────────────────

    async def login(self) -> None:
        if not settings.fpl_email or not settings.fpl_password:
            raise ValueError("FPL_EMAIL and FPL_PASSWORD must be set in environment")
        await self._fpl.login(
            email=settings.fpl_email,
            password=settings.fpl_password,
        )
        self._authenticated = True
        logger.info("Authenticated with FPL as %s", settings.fpl_email)

    def _require_auth(self) -> None:
        if not self._authenticated:
            raise RuntimeError("FPLClient.login() must be called before authenticated endpoints")

    # ── Public endpoints (no auth) ────────────────────────────────────────────

    async def get_bootstrap(self) -> BootstrapData:
        """Fetch all player/team/GW data from bootstrap-static."""
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get("/bootstrap-static/")

        teams = [Team(**t) for t in raw["teams"]]
        elements = []
        for e in raw["elements"]:
            try:
                elements.append(Player(**e))
            except Exception as exc:
                logger.debug("Skipping player %s: %s", e.get("id"), exc)

        events = []
        for ev in raw["events"]:
            try:
                events.append(Gameweek(**ev))
            except Exception as exc:
                logger.debug("Skipping GW %s: %s", ev.get("id"), exc)

        return BootstrapData(events=events, teams=teams, elements=elements)

    async def get_fixtures(self) -> list[Fixture]:
        """Fetch all season fixtures."""
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get("/fixtures/")
        fixtures = []
        for f in raw:
            try:
                fixtures.append(Fixture(**f))
            except Exception as exc:
                logger.debug("Skipping fixture %s: %s", f.get("id"), exc)
        return fixtures

    async def get_player_history(self, player_id: int) -> list[PlayerGameweekHistory]:
        """Fetch per-GW history for a single player."""
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get(f"/element-summary/{player_id}/")
        history = []
        for h in raw.get("history", []):
            try:
                history.append(PlayerGameweekHistory(element=player_id, **h))
            except Exception as exc:
                logger.debug("Skipping history row for player %d GW %s: %s", player_id, h.get("round"), exc)
        return history

    async def get_gw_live_scores(self, gw: int) -> dict[int, int]:
        """
        Fetch live/final points for all players in a specific GW.
        Returns {player_id: total_points}.
        """
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get(f"/event/{gw}/live/")
        return {
            int(elem_id): data["stats"]["total_points"]
            for elem_id, data in raw.get("elements", {}).items()
        }

    async def get_entry_picks(self, gw: int) -> list[dict]:
        """Fetch public pick data for our team for a given GW (no auth required)."""
        team_id = settings.fpl_team_id
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get(f"/entry/{team_id}/event/{gw}/picks/")
        return raw.get("picks", [])

    # ── Authenticated endpoints ───────────────────────────────────────────────

    async def get_my_team(self) -> MyTeam:
        """Fetch current squad, chips, and transfer info (requires auth)."""
        self._require_auth()
        team_id = settings.fpl_team_id
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        raw = await self._get(f"/my-team/{team_id}/")

        picks = [Pick(**p) for p in raw["picks"]]

        chips = []
        for c in raw.get("chips", []):
            try:
                chips.append(ChipStatus(
                    name=ChipType(c["name"]),
                    status_for_entry=c.get("status_for_entry", "unavailable"),
                    played_by_entry=c.get("played_by_entry", []),
                ))
            except ValueError:
                pass  # unknown chip type — ignore

        transfers_raw = raw.get("transfers", {})
        transfer_info = TransferInfo(
            cost=transfers_raw.get("cost", 0),
            status=transfers_raw.get("status", "cost"),
            limit=transfers_raw.get("limit"),
            made=transfers_raw.get("made", 0),
            bank=transfers_raw.get("bank", 0),
            value=transfers_raw.get("value", 1000),
        )

        return MyTeam(picks=picks, chips=chips, transfers=transfer_info)

    async def submit_transfers(
        self,
        transfers: list[dict[str, int]],
        chip: Optional[str] = None,
    ) -> dict:
        """
        Submit transfers to FPL.

        Args:
            transfers: list of {"element_in": id, "element_out": id, "purchase_price": tenths, "selling_price": tenths}
            chip: chip name to activate (e.g. "wildcard", "freehit") or None

        Returns raw API response.
        """
        self._require_auth()

        if settings.dry_run:
            logger.info("[DRY RUN] Would submit transfers: %s (chip=%s)", transfers, chip)
            return {"dry_run": True, "transfers": transfers}

        payload: dict[str, Any] = {
            "confirmed": True,
            "entry": settings.fpl_team_id,
            "event": await self._get_current_gw_id(),
            "transfers": transfers,
        }
        if chip:
            payload["chip"] = chip

        async with self._session.post(
            f"{BASE_URL}/transfers/",
            json=payload,
            headers={"Referer": "https://fantasy.premierleague.com/transfers"},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            logger.info("Transfers submitted successfully: %s", result)
            return result

    async def set_team(
        self,
        picks: list[dict],
        chip: Optional[str] = None,
    ) -> dict:
        """
        Save starting XI, bench order, captain, vice-captain.

        Args:
            picks: list of {"element": id, "position": 1-15, "is_captain": bool, "is_vice_captain": bool}
            chip: chip to activate ("bboost", "3xc") or None
        """
        self._require_auth()

        if settings.dry_run:
            logger.info("[DRY RUN] Would set team: %d picks, chip=%s", len(picks), chip)
            return {"dry_run": True, "picks": picks}

        payload: dict[str, Any] = {"picks": picks}
        if chip:
            payload["chip"] = chip

        async with self._session.post(
            f"{BASE_URL}/my-team/{settings.fpl_team_id}/",
            json=payload,
            headers={"Referer": "https://fantasy.premierleague.com/my-team"},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            logger.info("Team selection saved successfully")
            return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get(self, path: str) -> Any:
        url = f"{BASE_URL}{path}"
        for attempt in range(4):
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning("Rate limited by FPL API, waiting %ds", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                logger.warning("FPL API error on %s (attempt %d): %s — retrying in %ds", path, attempt + 1, exc, wait)
                await asyncio.sleep(wait)
        raise RuntimeError(f"FPL API request failed after 4 attempts: {path}")

    async def _get_current_gw_id(self) -> int:
        bootstrap = await self.get_bootstrap()
        gw = bootstrap.current_gw or bootstrap.next_gw
        if gw is None:
            raise RuntimeError("Could not determine current/next GW from bootstrap data")
        return gw.id

    async def get_manager_info(self) -> dict:
        """Fetch public manager info."""
        await asyncio.sleep(settings.fpl_api_rate_limit_delay)
        return await self._get(f"/entry/{settings.fpl_team_id}/")

    async def bulk_fetch_player_histories(
        self,
        player_ids: list[int],
        concurrency: int = 5,
    ) -> dict[int, list[PlayerGameweekHistory]]:
        """
        Fetch GW history for multiple players with concurrency control.
        Respects rate limits by batching.
        """
        results: dict[int, list[PlayerGameweekHistory]] = {}
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(pid: int) -> None:
            async with semaphore:
                try:
                    results[pid] = await self.get_player_history(pid)
                except Exception as exc:
                    logger.warning("Failed to fetch history for player %d: %s", pid, exc)
                    results[pid] = []

        await asyncio.gather(*[fetch_one(pid) for pid in player_ids])
        return results
