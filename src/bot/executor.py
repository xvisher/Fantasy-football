"""
Transfer executor and team setter.

This is the action layer — it takes the optimiser's recommendations and
applies them via the FPL API. All actions are logged to the DB.

Dry-run mode (settings.dry_run=True) logs what would happen without
submitting any actual API calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.analysis.chip_strategy import ChipDecision, ChipStrategy
from src.analysis.expected_points import ExpectedPointsCalculator
from src.analysis.fixtures import FixtureAnalyser
from src.analysis.learner import load_current_weights, run_learning_cycle
from src.analysis.optimizer import OptimiserResult, SquadOptimiser
from src.config.settings import settings
from src.data.database import (
    get_xg_stats,
    init_db,
    log_decision,
    update_decision_actual_pts,
    upsert_gameweeks,
    upsert_player_history,
    upsert_players,
    upsert_xg_stats,
)
from src.data.fpl_client import FPLClient
from src.data.models import BootstrapData, ChipType, MyTeam, PlayerWithXP
from src.data.understat_client import UnderstatFPLClient

logger = logging.getLogger(__name__)


class FPLExecutor:
    """
    Orchestrates the full gameweek cycle:
      data refresh → analysis → chip decision → optimisation → execution → logging
    """

    async def run_gameweek_cycle(self, target_gw: Optional[int] = None) -> dict:
        """
        Main entry point for the per-GW automation cycle.

        Args:
            target_gw: Gameweek to optimise for (defaults to next GW)

        Returns summary dict for notification.
        """
        logger.info("=== Starting GW cycle (dry_run=%s) ===", settings.dry_run)
        await init_db()

        async with FPLClient() as client:
            await client.login()

            # ── 1. Fetch data ─────────────────────────────────────────────────
            logger.info("Fetching bootstrap data...")
            bootstrap = await client.get_bootstrap()
            fixtures = await client.get_fixtures()

            # Determine target GW
            gw = target_gw or (bootstrap.next_gw.id if bootstrap.next_gw else None)
            if gw is None:
                logger.error("Could not determine target GW — aborting")
                return {"error": "No target GW found"}

            logger.info("Targeting GW %d", gw)

            # Persist GW data
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

            # Persist players
            await upsert_players(gw, [
                {
                    "id": p.id,
                    "gw": gw,
                    "web_name": p.web_name,
                    "team_id": p.team,
                    "element_type": p.element_type,
                    "now_cost": p.now_cost,
                    "status": p.status,
                    "chance_of_playing": p.chance_of_playing_next_round,
                    "form": float(p.form) if p.form else 0.0,
                    "total_points": p.total_points,
                    "selected_by_pct": float(p.selected_by_percent) if p.selected_by_percent else 0.0,
                    "xg_season": p.xg_season,
                    "xa_season": p.xa_season,
                    "news": p.news,
                    "raw_json": None,
                }
                for p in bootstrap.elements
            ])

            # ── 2. Fetch xG/xA from Understat ─────────────────────────────────
            logger.info("Fetching Understat xG/xA data...")
            season_year = str(gw // 38 + 2026)  # derive season from GW (approximate)
            understat = UnderstatFPLClient(season=season_year)
            try:
                await understat.fetch_and_store(bootstrap.elements, gw)
            except Exception as exc:
                logger.warning("Understat fetch failed (will use FPL xG fallback): %s", exc)

            # ── 3. Fetch current team ─────────────────────────────────────────
            logger.info("Fetching current team...")
            my_team = await client.get_my_team()

            # ── 4. Load model weights (learned or defaults) ───────────────────
            model_weights = await load_current_weights()

            # ── 5. Build analysis objects ─────────────────────────────────────
            fixture_analyser = FixtureAnalyser(bootstrap, fixtures)
            xg_stats = await get_xg_stats(
                [p.id for p in bootstrap.elements], gw
            )
            xp_calc = ExpectedPointsCalculator(fixture_analyser, xg_stats, model_weights)

            players_with_xp = xp_calc.compute_all(
                bootstrap.elements, gw, lookahead=settings.lookahead_gameweeks
            )
            player_xp_map: dict[int, PlayerWithXP] = {p.player.id: p for p in players_with_xp}

            # ── 6. Chip decision ──────────────────────────────────────────────
            current_squad_xp = sum(
                player_xp_map[pid].xp_next_gw
                for pid in my_team.starting_ids
                if pid in player_xp_map
            )
            best_possible_xp = sum(
                sorted([p.xp_next_gw for p in players_with_xp], reverse=True)[:11]
            )
            bench_xp = sum(
                player_xp_map[pid].xp_next_gw
                for pid in my_team.bench_ids
                if pid in player_xp_map
            )
            captain_id_current = my_team.captain_id
            captain_xp = player_xp_map.get(captain_id_current, PlayerWithXP(
                player=bootstrap.elements[0]
            )).xp_next_gw if captain_id_current else 0.0

            bgw_starters = ChipStrategy.count_bgw_in_starting(my_team.starting_ids, player_xp_map)
            dgw_in_squad = ChipStrategy.count_dgw_in_squad(my_team.all_ids, player_xp_map)

            chip_strategy = ChipStrategy(my_team.available_chips, gw)
            chip_decision = chip_strategy.evaluate(
                current_squad_xp=current_squad_xp,
                best_possible_xp=best_possible_xp,
                bgw_starters_count=bgw_starters,
                captain_xp=captain_xp,
                season_avg_gw_xp=50.0,  # TODO: compute from actual GW history
                bench_xp=bench_xp,
                dgw_count_in_squad=dgw_in_squad,
            )
            logger.info("Chip decision: %s — %s", chip_decision.chip, chip_decision.reason)

            # ── 7. Optimise ───────────────────────────────────────────────────
            optimiser = SquadOptimiser(players_with_xp)
            is_wildcard_or_freehit = chip_decision.chip in (ChipType.WILDCARD, ChipType.FREE_HIT)

            if is_wildcard_or_freehit:
                logger.info("Running FULL squad rebuild (chip: %s)...", chip_decision.chip)
                result = optimiser.select_squad(
                    budget=my_team.transfers.bank_value + my_team.transfers.squad_value,
                )
                result.chip = chip_decision.chip
            else:
                logger.info("Running transfer optimiser...")
                sell_prices = {
                    pick.element: bootstrap.player_map.get(pick.element, bootstrap.elements[0]).sell_price
                    for pick in my_team.picks
                }
                result = optimiser.optimise_transfers(
                    current_team=my_team,
                    available_budget=my_team.transfers.bank_value,
                    free_transfers=my_team.transfers.free_transfers,
                    player_prices=sell_prices,
                )
                if chip_decision.chip in (ChipType.TRIPLE_CAPTAIN, ChipType.BENCH_BOOST):
                    result.chip = chip_decision.chip

            # ── 8. Execute ────────────────────────────────────────────────────
            summary = await self._execute(client, bootstrap, my_team, result, gw)
            summary["chip_reason"] = chip_decision.reason
            summary["gw"] = gw
            summary["dry_run"] = settings.dry_run
            return summary

    async def run_post_gw_update(self, completed_gw: int) -> None:
        """
        Called after a GW finishes. Fetches actual scores, updates DB,
        runs the learning cycle.
        """
        logger.info("Running post-GW update for GW %d", completed_gw)
        async with FPLClient() as client:
            await client.login()
            live_scores = await client.get_gw_live_scores(completed_gw)

        # Store actual GW points in decision log
        # (decision_id mapping would require more sophisticated tracking;
        #  here we approximate by updating decisions for this GW)
        logger.info("Fetched live scores for %d players", len(live_scores))
        # TODO: match live_scores back to decision_ids in decisions_log

        # Run learning cycle
        new_weights = await run_learning_cycle(completed_gw)
        if new_weights:
            logger.info("Model weights updated after GW %d", completed_gw)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _execute(
        self,
        client: FPLClient,
        bootstrap: BootstrapData,
        my_team: MyTeam,
        result: OptimiserResult,
        gw: int,
    ) -> dict:
        """Apply the optimiser result via FPL API calls."""
        player_map = bootstrap.player_map
        summary = {
            "transfers": [],
            "captain": None,
            "vice_captain": None,
            "chip": result.chip.value if result.chip else None,
            "predicted_xp": result.predicted_xp,
        }

        # ── Transfers ─────────────────────────────────────────────────────────
        if result.transfers:
            transfer_payload = []
            for rec in result.transfers:
                p_in = player_map.get(rec.player_in_id)
                p_out = player_map.get(rec.player_out_id)
                if not p_in or not p_out:
                    continue

                name_in = p_in.web_name
                name_out = p_out.web_name
                logger.info(
                    "Transfer: OUT %s → IN %s (net xP gain: %.2f, hit cost: %d)",
                    name_out, name_in, rec.net_xp_gain, rec.hit_cost,
                )

                decision_id = await log_decision(
                    gw=gw,
                    action_type="transfer",
                    predicted_xp=rec.predicted_xp_gain,
                    reasoning=f"IN {name_in} xP={self._get_xp(rec.player_in_id):.2f}, OUT {name_out}",
                    player_in_id=rec.player_in_id,
                    player_out_id=rec.player_out_id,
                )

                transfer_payload.append({
                    "element_in": rec.player_in_id,
                    "element_out": rec.player_out_id,
                    "purchase_price": p_in.now_cost,
                    "selling_price": p_out.now_cost,
                })

                summary["transfers"].append({
                    "in": name_in,
                    "out": name_out,
                    "net_xp_gain": rec.net_xp_gain,
                    "hit_cost": rec.hit_cost,
                })

            if transfer_payload:
                chip_to_activate = (
                    result.chip.value
                    if result.chip in (ChipType.WILDCARD, ChipType.FREE_HIT)
                    else None
                )
                await client.submit_transfers(
                    transfers=transfer_payload,
                    chip=chip_to_activate,
                )

        # ── Team selection ─────────────────────────────────────────────────────
        cap = player_map.get(result.captain_id)
        vc = player_map.get(result.vice_captain_id)
        if cap:
            logger.info("Captain: %s (xP=%.2f)", cap.web_name, self._get_xp(result.captain_id))
            summary["captain"] = cap.web_name
        if vc:
            logger.info("Vice-captain: %s", vc.web_name)
            summary["vice_captain"] = vc.web_name

        picks_payload = []
        for pos_idx, pid in enumerate(result.starting_ids + result.bench_ids, start=1):
            picks_payload.append({
                "element": pid,
                "position": pos_idx,
                "is_captain": pid == result.captain_id,
                "is_vice_captain": pid == result.vice_captain_id,
            })

        chip_for_team = (
            result.chip.value
            if result.chip in (ChipType.TRIPLE_CAPTAIN, ChipType.BENCH_BOOST)
            else None
        )
        await client.set_team(picks=picks_payload, chip=chip_for_team)

        await log_decision(
            gw=gw,
            action_type="team_selection",
            predicted_xp=result.predicted_xp,
            reasoning=f"Captain: {cap.web_name if cap else '?'}, chip: {result.chip}",
        )

        return summary

    def _get_xp(self, player_id: int) -> float:
        # Placeholder — in practice the executor would have access to the xP map
        return 0.0
