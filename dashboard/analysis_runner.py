"""
Dashboard analysis runner.

Wraps the existing analysis pipeline (src/) and returns structured data
for the web UI. Always runs with DRY_RUN=True — never submits transfers.

Results are cached for 10 minutes to respect FPL API rate limits.
Call run(force=True) to bypass the cache.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.analysis.chip_strategy import ChipDecision, ChipStrategy
from src.analysis.expected_points import ExpectedPointsCalculator
from src.analysis.fixtures import FixtureAnalyser, TeamFixtureInfo
from src.analysis.learner import load_current_weights
from src.analysis.optimizer import OptimiserResult, SquadOptimiser
from src.config.settings import settings
from src.data.database import get_xg_stats, init_db, upsert_gameweeks, upsert_players
from src.data.fpl_client import FPLClient
from src.data.models import (
    BootstrapData,
    ChipType,
    Fixture,
    Gameweek,
    MyTeam,
    PlayerWithXP,
    Team,
)
from src.data.understat_client import UnderstatFPLClient

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # 10 minutes


@dataclass
class TransferScenario:
    label: str                          # "No hit", "1 hit (−4pts)", etc.
    result: Optional[OptimiserResult]
    transfers_out: list[str] = field(default_factory=list)
    transfers_in: list[str] = field(default_factory=list)
    net_xp_gain: float = 0.0
    hit_cost: int = 0
    colour: str = "green"               # green / amber / red


@dataclass
class DashboardData:
    gw: int
    gw_deadline: Optional[datetime]
    bootstrap: BootstrapData
    fixtures: list[Fixture]
    my_team: Optional[MyTeam]

    # xP-enriched players
    my_squad: list[PlayerWithXP]         # current 15, ordered: starting then bench
    starting_ids: list[int]
    bench_ids: list[int]
    captain_id: int
    vice_captain_id: int

    # Recommendations
    chip_decision: ChipDecision
    transfer_scenarios: list[TransferScenario]

    # Full player pool sorted by xP
    all_players_ranked: list[PlayerWithXP]

    # Fixture grid: team_id → list of TeamFixtureInfo (next 6 GWs)
    fixture_grid: dict[int, list[TeamFixtureInfo]]

    # Model metadata
    model_weights: dict
    last_updated: datetime

    # Errors / warnings
    warnings: list[str] = field(default_factory=list)


# ── Cache ─────────────────────────────────────────────────────────────────────

_cache: Optional[DashboardData] = None
_cache_time: Optional[datetime] = None
_lock = asyncio.Lock()


async def get_cached() -> Optional[DashboardData]:
    if _cache and _cache_time:
        age = (datetime.now(tz=timezone.utc) - _cache_time).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _cache
    return None


async def run(force: bool = False) -> DashboardData:
    """
    Run the full analysis pipeline and return DashboardData.

    Args:
        force: if True, bypass cache and re-fetch everything

    Returns DashboardData ready for template rendering.
    """
    global _cache, _cache_time

    async with _lock:
        if not force:
            cached = await get_cached()
            if cached:
                logger.info("Returning cached dashboard data (age < %ds)", CACHE_TTL_SECONDS)
                return cached

        logger.info("Running fresh analysis pipeline...")
        await init_db()
        data = await _run_pipeline()
        _cache = data
        _cache_time = datetime.now(tz=timezone.utc)
        return data


async def _run_pipeline() -> DashboardData:
    warnings: list[str] = []

    async with FPLClient() as client:
        # ── Auth + data fetch ─────────────────────────────────────────────────
        try:
            await client.login()
            my_team = await client.get_my_team()
        except Exception as exc:
            logger.warning("Could not fetch my-team (auth failed?): %s", exc)
            warnings.append(f"Could not load your team: {exc}. Showing analysis only.")
            my_team = None

        bootstrap = await client.get_bootstrap()
        fixtures = await client.get_fixtures()

    # ── Determine GW ─────────────────────────────────────────────────────────
    next_gw = bootstrap.next_gw or bootstrap.current_gw
    gw = next_gw.id if next_gw else 1
    gw_deadline = next_gw.deadline_time if next_gw else None

    # ── Persist to DB ─────────────────────────────────────────────────────────
    await upsert_gameweeks([
        {
            "id": ev.id, "name": ev.name,
            "deadline_time": ev.deadline_time.isoformat(),
            "finished": int(ev.finished), "is_current": int(ev.is_current),
            "is_next": int(ev.is_next),
            "average_score": ev.average_entry_score, "highest_score": ev.highest_score,
        }
        for ev in bootstrap.events
    ])
    await upsert_players(gw, [
        {
            "id": p.id, "gw": gw, "web_name": p.web_name, "team_id": p.team,
            "element_type": p.element_type, "now_cost": p.now_cost, "status": p.status,
            "chance_of_playing": p.chance_of_playing_next_round,
            "form": float(p.form) if p.form else 0.0, "total_points": p.total_points,
            "selected_by_pct": float(p.selected_by_percent) if p.selected_by_percent else 0.0,
            "xg_season": p.xg_season, "xa_season": p.xa_season, "news": p.news, "raw_json": None,
        }
        for p in bootstrap.elements
    ])

    # ── Understat xG/xA ───────────────────────────────────────────────────────
    season_year = str(2026 + (gw - 1) // 38)
    understat = UnderstatFPLClient(season=season_year)
    try:
        await understat.fetch_and_store(bootstrap.elements, gw)
    except Exception as exc:
        warnings.append(f"Understat unavailable — using FPL xG fallback: {exc}")

    # ── Analysis ──────────────────────────────────────────────────────────────
    model_weights = await load_current_weights()
    fixture_analyser = FixtureAnalyser(bootstrap, fixtures)
    xg_stats = await get_xg_stats([p.id for p in bootstrap.elements], gw)
    xp_calc = ExpectedPointsCalculator(fixture_analyser, xg_stats, model_weights)

    all_pwxp = xp_calc.compute_all(
        bootstrap.elements, gw, lookahead=settings.lookahead_gameweeks
    )
    player_xp_map: dict[int, PlayerWithXP] = {p.player.id: p for p in all_pwxp}

    # ── My squad ──────────────────────────────────────────────────────────────
    starting_ids: list[int] = []
    bench_ids: list[int] = []
    captain_id: int = 0
    vice_captain_id: int = 0

    if my_team:
        starting_ids = my_team.starting_ids
        bench_ids = my_team.bench_ids
        captain_id = my_team.captain_id or 0
        vice_captain_id = next(
            (p.element for p in my_team.picks if p.is_vice_captain), 0
        )

    my_squad_ordered = [
        player_xp_map[pid] for pid in (starting_ids + bench_ids)
        if pid in player_xp_map
    ]

    # ── Chip decision ─────────────────────────────────────────────────────────
    current_squad_xp = sum(
        player_xp_map[pid].xp_next_gw for pid in starting_ids if pid in player_xp_map
    )
    best_possible_xp = sum(
        sorted([p.xp_next_gw for p in all_pwxp], reverse=True)[:11]
    )
    bench_xp = sum(
        player_xp_map[pid].xp_next_gw for pid in bench_ids if pid in player_xp_map
    )
    captain_xp = player_xp_map.get(captain_id, all_pwxp[0]).xp_next_gw if captain_id else 0.0
    bgw_starters = ChipStrategy.count_bgw_in_starting(starting_ids, player_xp_map)
    dgw_in_squad = ChipStrategy.count_dgw_in_squad(starting_ids + bench_ids, player_xp_map)

    available_chips = my_team.available_chips if my_team else []
    chip_strategy = ChipStrategy(available_chips, gw)
    chip_decision = chip_strategy.evaluate(
        current_squad_xp=current_squad_xp,
        best_possible_xp=best_possible_xp,
        bgw_starters_count=bgw_starters,
        captain_xp=captain_xp,
        season_avg_gw_xp=50.0,
        bench_xp=bench_xp,
        dgw_count_in_squad=dgw_in_squad,
    )

    # ── Transfer scenarios ─────────────────────────────────────────────────────
    transfer_scenarios = await _build_transfer_scenarios(
        all_pwxp, my_team, bootstrap, player_xp_map
    )

    # ── All players ranked ────────────────────────────────────────────────────
    all_ranked = sorted(all_pwxp, key=lambda p: -p.xp_next_gw)

    # ── Fixture grid (next 6 GWs) ─────────────────────────────────────────────
    fixture_grid = fixture_analyser.upcoming_gw_summary(gw, lookahead=6)

    return DashboardData(
        gw=gw,
        gw_deadline=gw_deadline,
        bootstrap=bootstrap,
        fixtures=fixtures,
        my_team=my_team,
        my_squad=my_squad_ordered,
        starting_ids=starting_ids,
        bench_ids=bench_ids,
        captain_id=captain_id,
        vice_captain_id=vice_captain_id,
        chip_decision=chip_decision,
        transfer_scenarios=transfer_scenarios,
        all_players_ranked=all_ranked,
        fixture_grid=fixture_grid,
        model_weights=model_weights or {},
        last_updated=datetime.now(tz=timezone.utc),
        warnings=warnings,
    )


async def _build_transfer_scenarios(
    all_pwxp: list[PlayerWithXP],
    my_team: Optional[MyTeam],
    bootstrap: BootstrapData,
    player_xp_map: dict[int, PlayerWithXP],
) -> list[TransferScenario]:
    if not my_team:
        return []

    optimiser = SquadOptimiser(all_pwxp)
    player_map = bootstrap.player_map
    sell_prices = {
        pick.element: (player_map[pick.element].sell_price if pick.element in player_map else 8.0)
        for pick in my_team.picks
    }
    free_transfers = my_team.transfers.free_transfers
    bank = my_team.transfers.bank_value
    scenarios: list[TransferScenario] = []

    hit_configs = [
        ("No hit", 0),
        ("1 hit  (−4 pts)", 4),
        ("2 hits (−8 pts)", 8),
    ]

    for label, max_hit in hit_configs:
        try:
            result = optimiser.optimise_transfers(
                current_team=my_team,
                available_budget=bank,
                free_transfers=free_transfers,
                player_prices=sell_prices,
                max_hit=max_hit,
            )
            current_ids = set(my_team.all_ids)
            new_ids = set(result.selected_ids)
            out_ids = current_ids - new_ids
            in_ids = new_ids - current_ids

            out_names = [player_map[pid].web_name for pid in out_ids if pid in player_map]
            in_names = [player_map[pid].web_name for pid in in_ids if pid in player_map]
            net_gain = sum(
                player_xp_map[pid].xp_next_gw for pid in in_ids if pid in player_xp_map
            ) - sum(
                player_xp_map[pid].xp_next_gw for pid in out_ids if pid in player_xp_map
            ) - max_hit

            colour = "green" if net_gain > 2 else ("amber" if net_gain > 0 else "red")
            scenarios.append(TransferScenario(
                label=label, result=result,
                transfers_out=out_names, transfers_in=in_names,
                net_xp_gain=round(net_gain, 2), hit_cost=max_hit, colour=colour,
            ))
        except Exception as exc:
            logger.warning("Transfer scenario '%s' failed: %s", label, exc)
            scenarios.append(TransferScenario(label=label, result=None, colour="red"))

    # Wildcard scenario
    try:
        wc_result = optimiser.select_squad(
            budget=my_team.transfers.bank_value + my_team.transfers.squad_value,
        )
        new_ids = set(wc_result.selected_ids)
        current_ids = set(my_team.all_ids)
        out_names = [player_map[pid].web_name for pid in current_ids - new_ids if pid in player_map]
        in_names = [player_map[pid].web_name for pid in new_ids - current_ids if pid in player_map]
        scenarios.append(TransferScenario(
            label="Wildcard", result=wc_result,
            transfers_out=out_names, transfers_in=in_names,
            net_xp_gain=round(wc_result.predicted_xp, 2), hit_cost=0, colour="green",
        ))
    except Exception as exc:
        logger.warning("Wildcard scenario failed: %s", exc)

    return scenarios
