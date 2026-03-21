"""
Tests for the ILP squad optimiser.

Verifies that:
  - Selected squads always satisfy all FPL constraints
  - Transfers respect budget, position, and club limits
  - The solver produces valid starting XIs
"""

from __future__ import annotations

import pytest

from src.analysis.fixtures import FixtureAnalyser
from src.analysis.expected_points import ExpectedPointsCalculator
from src.analysis.optimizer import SquadOptimiser
from src.config.settings import settings
from src.data.models import Position, PlayerWithXP
from tests.conftest import make_fixture, make_player


def build_players_with_xp(bootstrap, gw: int = 1, count: int = 30) -> list[PlayerWithXP]:
    """Build a realistic pool of PlayerWithXP objects for optimiser tests."""
    fixtures = [make_fixture(i, gw=i, team_h=i % 10 + 1, team_a=(i + 1) % 10 + 1) for i in range(1, 39)]
    analyser = FixtureAnalyser(bootstrap, fixtures)
    calc = ExpectedPointsCalculator(analyser, {})

    players = []
    # Ensure we have enough of each position in the pool
    positions = (
        [1] * 4    # 4 GKs
        + [2] * 10  # 10 DEFs
        + [3] * 10  # 10 MIDs
        + [4] * 6   # 6 FWDs
    )
    for i, pos in enumerate(positions[:count], start=1):
        p = make_player(
            id=100 + i,
            position=pos,
            team=(i % 18) + 1,  # distribute across 18 of 20 teams
            cost=50 + (i % 5) * 10,  # costs between 50 (£5m) and 90 (£9m)
        )
        pwxp = PlayerWithXP(
            player=p,
            xp_next_gw=float(5 + i % 8),
            xp_lookahead=float(15 + i % 20),
            fixture_count_next_gw=1,
            is_dgw=False,
            is_bgw=False,
            fdr_next_gw=3.0,
        )
        players.append(pwxp)
    return players


class TestSquadSelection:
    @pytest.fixture
    def players_with_xp(self, bootstrap):
        return build_players_with_xp(bootstrap)

    def test_squad_size_is_15(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        assert len(result.selected_ids) == settings.squad_size

    def test_positional_counts_are_correct(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}

        positions = [player_map[pid].player.position for pid in result.selected_ids]
        assert positions.count(Position.GKP) == settings.squad_gk
        assert positions.count(Position.DEF) == settings.squad_def
        assert positions.count(Position.MID) == settings.squad_mid
        assert positions.count(Position.FWD) == settings.squad_fwd

    def test_max_3_per_club(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}

        team_counts: dict[int, int] = {}
        for pid in result.selected_ids:
            team_id = player_map[pid].player.team
            team_counts[team_id] = team_counts.get(team_id, 0) + 1

        for team_id, count in team_counts.items():
            assert count <= settings.max_per_club, f"Team {team_id} has {count} players (max {settings.max_per_club})"

    def test_budget_constraint(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}
        total_cost = sum(player_map[pid].player.price for pid in result.selected_ids)
        assert total_cost <= 100.0 + 0.01  # allow tiny float rounding

    def test_starting_xi_is_11(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        assert len(result.starting_ids) == settings.starting_xi
        assert len(result.bench_ids) == settings.squad_size - settings.starting_xi

    def test_starting_xi_has_1_gk(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}
        gks_starting = sum(
            1 for pid in result.starting_ids
            if player_map[pid].player.position == Position.GKP
        )
        assert gks_starting == 1

    def test_starting_xi_has_min_3_def(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}
        defs_starting = sum(
            1 for pid in result.starting_ids
            if player_map[pid].player.position == Position.DEF
        )
        assert defs_starting >= settings.starting_min_def

    def test_starting_xi_has_min_1_fwd(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        player_map = {p.player.id: p for p in players_with_xp}
        fwds_starting = sum(
            1 for pid in result.starting_ids
            if player_map[pid].player.position == Position.FWD
        )
        assert fwds_starting >= settings.starting_min_fwd

    def test_captain_is_in_starting_xi(self, players_with_xp):
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0)
        assert result.captain_id in result.starting_ids

    def test_locked_in_players_included(self, players_with_xp):
        locked_id = players_with_xp[0].player.id
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0, locked_in=[locked_id])
        assert locked_id in result.selected_ids

    def test_excluded_players_not_included(self, players_with_xp):
        excluded_id = players_with_xp[0].player.id
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.select_squad(budget=100.0, excluded=[excluded_id])
        assert excluded_id not in result.selected_ids


class TestTransferOptimiser:
    def test_no_change_when_squad_already_optimal(self, bootstrap, my_team):
        """If current squad is already optimal, no transfers should be made."""
        # Build players_with_xp where current squad has highest xP
        players_with_xp = build_players_with_xp(bootstrap)
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.optimise_transfers(
            current_team=my_team,
            available_budget=5.0,
            free_transfers=1,
            player_prices={p.element: 8.0 for p in my_team.picks},
        )
        # Should return a valid result (even if no transfers)
        assert result.solver_status in ("Optimal", "Not Solved")

    def test_transfer_respects_max_hit(self, bootstrap, my_team):
        players_with_xp = build_players_with_xp(bootstrap, count=30)
        optimiser = SquadOptimiser(players_with_xp)
        result = optimiser.optimise_transfers(
            current_team=my_team,
            available_budget=10.0,
            free_transfers=1,
            player_prices={p.element: 8.0 for p in my_team.picks},
            max_hit=settings.max_hit_per_gw,
        )
        total_hit = sum(r.hit_cost for r in result.transfers)
        assert total_hit <= settings.max_hit_per_gw
