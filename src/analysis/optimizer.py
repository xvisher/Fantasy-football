"""
Squad and transfer optimiser using Integer Linear Programming (PuLP).

Two main problems solved:
1. SQUAD SELECTION — build the best possible 15-player squad from scratch
   (used for wildcard, free hit, and initial squad selection).

2. TRANSFER OPTIMISER — given the current squad, find the optimal set of
   transfers (respecting hit cost) to maximise expected points.

Both use the same ILP framework (maximise xP subject to FPL constraints).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pulp

from src.config.settings import settings
from src.data.models import (
    MyTeam,
    Player,
    PlayerWithXP,
    Position,
    TeamSelection,
    TransferRecommendation,
    ChipType,
)

logger = logging.getLogger(__name__)


@dataclass
class OptimiserResult:
    selected_ids: list[int]
    captain_id: int
    vice_captain_id: int
    starting_ids: list[int]
    bench_ids: list[int]
    total_cost: float
    predicted_xp: float
    transfers: list[TransferRecommendation] = field(default_factory=list)
    chip: Optional[ChipType] = None
    solver_status: str = ""


class SquadOptimiser:
    """
    Solves FPL squad selection and transfer planning as ILP problems.
    """

    def __init__(self, players_with_xp: list[PlayerWithXP]) -> None:
        self._pwxp = players_with_xp
        self._player_map: dict[int, PlayerWithXP] = {p.player.id: p for p in players_with_xp}

    # ── Full squad selection (wildcard / fresh build) ─────────────────────────

    def select_squad(
        self,
        budget: float,
        locked_in: Optional[list[int]] = None,
        excluded: Optional[list[int]] = None,
    ) -> OptimiserResult:
        """
        Select the best 15-player squad within budget.

        Args:
            budget: total budget in £m
            locked_in: player IDs that must be included
            excluded: player IDs that must not be included

        Returns OptimiserResult with selected squad.
        """
        locked_in = locked_in or []
        excluded = excluded or []

        prob = pulp.LpProblem("fpl_squad_selection", pulp.LpMaximize)
        players = [p for p in self._pwxp if p.player.id not in excluded]

        # Binary decision variable per player (1 = in squad)
        x = {p.player.id: pulp.LpVariable(f"x_{p.player.id}", cat="Binary") for p in players}

        # Captain binary (1 = captain, points doubled)
        cap = {p.player.id: pulp.LpVariable(f"cap_{p.player.id}", cat="Binary") for p in players}

        # Objective: maximise total xP (captain gets double)
        prob += pulp.lpSum(
            x[p.player.id] * p.xp_lookahead + cap[p.player.id] * p.xp_lookahead
            for p in players
        )

        # ── Constraints ───────────────────────────────────────────────────────

        # Squad size
        prob += pulp.lpSum(x[p.player.id] for p in players) == settings.squad_size

        # Positional constraints
        for pos, required in [
            (Position.GKP, settings.squad_gk),
            (Position.DEF, settings.squad_def),
            (Position.MID, settings.squad_mid),
            (Position.FWD, settings.squad_fwd),
        ]:
            prob += (
                pulp.lpSum(
                    x[p.player.id] for p in players if p.player.position == pos
                )
                == required
            )

        # Max 3 per club
        teams = set(p.player.team for p in players)
        for team_id in teams:
            prob += (
                pulp.lpSum(
                    x[p.player.id] for p in players if p.player.team == team_id
                )
                <= settings.max_per_club
            )

        # Budget
        prob += (
            pulp.lpSum(x[p.player.id] * p.player.price for p in players)
            <= budget
        )

        # Locked-in players must be selected
        for pid in locked_in:
            if pid in x:
                prob += x[pid] == 1

        # Captain must be in squad
        prob += pulp.lpSum(cap[p.player.id] for p in players) == 1
        for p in players:
            prob += cap[p.player.id] <= x[p.player.id]

        # Captain must be GKP/DEF/MID/FWD (not a BGW player)
        bgw_players = [p for p in players if p.is_bgw]
        for p in bgw_players:
            prob += cap[p.player.id] == 0

        # ── Solve ─────────────────────────────────────────────────────────────
        status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
        status_str = pulp.LpStatus[status]
        logger.info("Squad selection solver status: %s", status_str)

        if status not in (pulp.LpStatusOptimal, 1):
            raise RuntimeError(f"Squad selection ILP failed with status: {status_str}")

        selected = [p for p in players if pulp.value(x[p.player.id]) == 1]
        captain_id = next(
            (p.player.id for p in players if pulp.value(cap[p.player.id]) == 1),
            selected[0].player.id,
        )

        starting, bench = self._pick_starting_xi(selected, captain_id)
        vice_captain_id = self._pick_vice_captain(starting, captain_id)

        return OptimiserResult(
            selected_ids=[p.player.id for p in selected],
            captain_id=captain_id,
            vice_captain_id=vice_captain_id,
            starting_ids=[p.player.id for p in starting],
            bench_ids=[p.player.id for p in bench],
            total_cost=sum(p.player.price for p in selected),
            predicted_xp=pulp.value(prob.objective) or 0.0,
            solver_status=status_str,
        )

    # ── Transfer optimiser (1-5 transfers, hit-aware) ─────────────────────────

    def optimise_transfers(
        self,
        current_team: MyTeam,
        available_budget: float,
        free_transfers: int,
        player_prices: dict[int, float],
        max_hit: int = None,
    ) -> OptimiserResult:
        """
        Find optimal transfers from the current squad.

        Args:
            current_team: current MyTeam state
            available_budget: bank + current squad sell value
            free_transfers: free transfers available (0-5)
            player_prices: {player_id: sell_price} for current players
            max_hit: max hit points to accept (default from settings)

        Returns OptimiserResult with recommended transfers.
        """
        max_hit = max_hit if max_hit is not None else settings.max_hit_per_gw
        current_ids = set(current_team.all_ids)
        all_players = self._pwxp

        prob = pulp.LpProblem("fpl_transfer_optimiser", pulp.LpMaximize)

        # x[pid] = 1 if player is in the new squad
        x = {p.player.id: pulp.LpVariable(f"x_{p.player.id}", cat="Binary") for p in all_players}
        # transfer_in[pid] = 1 if player was not in squad and is now being added
        t_in = {
            p.player.id: pulp.LpVariable(f"tin_{p.player.id}", cat="Binary")
            for p in all_players
        }
        # Captain
        cap = {p.player.id: pulp.LpVariable(f"cap_{p.player.id}", cat="Binary") for p in all_players}

        # Number of transfers made (continuous, bounded by squad size)
        num_transfers = pulp.lpSum(
            t_in[p.player.id] for p in all_players if p.player.id not in current_ids
        )

        # Extra hits beyond free transfers, each costing 4 pts
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits >= num_transfers - free_transfers

        # Objective: maximise xP - transfer hit costs
        prob += pulp.lpSum(
            x[p.player.id] * p.xp_next_gw + cap[p.player.id] * p.xp_next_gw
            for p in all_players
        ) - hits * settings.transfer_hit_cost

        # ── Constraints ───────────────────────────────────────────────────────

        # Squad size = 15
        prob += pulp.lpSum(x[p.player.id] for p in all_players) == settings.squad_size

        # Positional constraints
        for pos, required in [
            (Position.GKP, settings.squad_gk),
            (Position.DEF, settings.squad_def),
            (Position.MID, settings.squad_mid),
            (Position.FWD, settings.squad_fwd),
        ]:
            prob += pulp.lpSum(x[p.player.id] for p in all_players if p.player.position == pos) == required

        # Max 3 per club
        all_team_ids = set(p.player.team for p in all_players)
        for team_id in all_team_ids:
            prob += pulp.lpSum(x[p.player.id] for p in all_players if p.player.team == team_id) <= settings.max_per_club

        # Budget constraint: current squad sell value + bank >= new squad cost
        sell_value = sum(player_prices.get(pid, 0.0) for pid in current_ids)
        new_squad_cost = pulp.lpSum(
            t_in[p.player.id] * p.player.price for p in all_players if p.player.id not in current_ids
        )
        prob += new_squad_cost <= sell_value + available_budget

        # Link t_in to x: a player is transferred in only if they were not in squad and are now selected
        for p in all_players:
            if p.player.id in current_ids:
                prob += t_in[p.player.id] == 0
            else:
                prob += t_in[p.player.id] >= x[p.player.id] - (1 if p.player.id in current_ids else 0)
                prob += t_in[p.player.id] <= x[p.player.id]

        # Max hit constraint
        prob += hits * settings.transfer_hit_cost <= max_hit

        # Captain
        prob += pulp.lpSum(cap[p.player.id] for p in all_players) == 1
        for p in all_players:
            prob += cap[p.player.id] <= x[p.player.id]

        # ── Solve ─────────────────────────────────────────────────────────────
        status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
        status_str = pulp.LpStatus[status]
        logger.info("Transfer optimiser status: %s", status_str)

        if status not in (pulp.LpStatusOptimal, 1):
            logger.warning("Transfer ILP non-optimal (%s) — returning current squad unchanged", status_str)
            return self._no_change_result(current_team, status_str)

        new_squad = [p for p in all_players if pulp.value(x[p.player.id]) == 1]
        new_ids = {p.player.id for p in new_squad}
        captain_id = next(
            (p.player.id for p in all_players if pulp.value(cap[p.player.id]) == 1),
            new_squad[0].player.id,
        )

        # Build transfer list
        transfers_out = current_ids - new_ids
        transfers_in = new_ids - current_ids
        recs = []
        for pid_in, pid_out in zip(sorted(transfers_in), sorted(transfers_out)):
            p_in = self._player_map.get(pid_in)
            p_out = self._player_map.get(pid_out)
            if p_in and p_out:
                xp_gain = p_in.xp_next_gw - p_out.xp_next_gw
                hit_cost = max(0, (len(recs) + 1 - free_transfers) * settings.transfer_hit_cost)
                recs.append(TransferRecommendation(
                    player_out_id=pid_out,
                    player_in_id=pid_in,
                    predicted_xp_gain=round(xp_gain, 3),
                    hit_cost=hit_cost,
                    net_xp_gain=round(xp_gain - hit_cost, 3),
                ))

        starting, bench = self._pick_starting_xi(new_squad, captain_id)
        vice_captain_id = self._pick_vice_captain(starting, captain_id)

        return OptimiserResult(
            selected_ids=list(new_ids),
            captain_id=captain_id,
            vice_captain_id=vice_captain_id,
            starting_ids=[p.player.id for p in starting],
            bench_ids=[p.player.id for p in bench],
            total_cost=sum(p.player.price for p in new_squad),
            predicted_xp=pulp.value(prob.objective) or 0.0,
            transfers=recs,
            solver_status=status_str,
        )

    # ── Starting XI picker ────────────────────────────────────────────────────

    def _pick_starting_xi(
        self,
        squad: list[PlayerWithXP],
        captain_id: int,
    ) -> tuple[list[PlayerWithXP], list[PlayerWithXP]]:
        """
        Choose the best starting XI from the squad.

        Rules:
          - Exactly 1 GK
          - Min 3 DEF
          - Min 1 FWD
          - Max 11 outfield players
          - Sort by xP_next_gw descending; apply constraints greedily
        """
        gks = sorted([p for p in squad if p.player.position == Position.GKP], key=lambda p: -p.xp_next_gw)
        outfield = sorted([p for p in squad if p.player.position != Position.GKP], key=lambda p: -p.xp_next_gw)

        starting_gk = [gks[0]] if gks else []
        bench_gk = gks[1:] if len(gks) > 1 else []

        # Ensure min 3 DEF and min 1 FWD in starting XI
        defs = [p for p in outfield if p.player.position == Position.DEF]
        fwds = [p for p in outfield if p.player.position == Position.FWD]
        mids = [p for p in outfield if p.player.position == Position.MID]

        starting_outfield = []
        bench_outfield = []

        # Lock in minimums
        for p in defs[:settings.starting_min_def]:
            starting_outfield.append(p)
        for p in fwds[:settings.starting_min_fwd]:
            starting_outfield.append(p)

        locked_ids = {p.player.id for p in starting_outfield}

        # Fill remaining slots from best available outfielders
        remaining = [p for p in outfield if p.player.id not in locked_ids]
        slots = settings.starting_xi - 1 - len(starting_outfield)  # -1 for GK
        for p in remaining[:slots]:
            starting_outfield.append(p)
        for p in remaining[slots:]:
            bench_outfield.append(p)

        # Any excess locked-in players go to bench (edge case)
        all_locked = set(p.player.id for p in starting_outfield)
        for p in outfield:
            if p.player.id not in all_locked and p not in bench_outfield:
                bench_outfield.append(p)

        starting = starting_gk + starting_outfield
        bench = bench_gk + bench_outfield

        return starting[:11], bench[:4]

    def _pick_vice_captain(self, starting: list[PlayerWithXP], captain_id: int) -> int:
        """Vice captain = highest xP player in starting XI who isn't captain."""
        candidates = [p for p in starting if p.player.id != captain_id]
        if not candidates:
            return captain_id
        return max(candidates, key=lambda p: p.xp_next_gw).player.id

    def _no_change_result(self, current_team: MyTeam, status_str: str) -> OptimiserResult:
        starting = current_team.starting_ids
        bench = current_team.bench_ids
        captain = current_team.captain_id or (starting[0] if starting else 0)
        vc = starting[1] if len(starting) > 1 else captain
        return OptimiserResult(
            selected_ids=current_team.all_ids,
            captain_id=captain,
            vice_captain_id=vc,
            starting_ids=starting,
            bench_ids=bench,
            total_cost=0.0,
            predicted_xp=0.0,
            transfers=[],
            solver_status=status_str,
        )
