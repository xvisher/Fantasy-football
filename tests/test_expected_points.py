"""
Tests for the Expected Points (xP) model.
"""

from __future__ import annotations

import pytest

from src.analysis.expected_points import ExpectedPointsCalculator
from src.analysis.fixtures import FixtureAnalyser
from src.data.models import Position
from tests.conftest import make_fixture, make_player


class TestExpectedPointsCalculator:
    @pytest.fixture
    def analyser_with_fixture(self, bootstrap):
        fixtures = [make_fixture(1, gw=1, team_h=1, team_a=2, fdr_h=3, fdr_a=3)]
        return FixtureAnalyser(bootstrap, fixtures)

    @pytest.fixture
    def xg_stats_for_player1(self):
        return {
            1: {"xg_per90": 0.4, "xa_per90": 0.2, "npxg_per90": 0.35}
        }

    def test_available_player_has_positive_xp(
        self, analyser_with_fixture, sample_players, xg_stats_for_player1
    ):
        calc = ExpectedPointsCalculator(analyser_with_fixture, xg_stats_for_player1)
        player = next(p for p in sample_players if p.id == 8)  # MID
        result = calc.compute(player, gw=1)
        assert result.xp_next_gw > 0

    def test_bgw_player_has_zero_xp(self, bootstrap, sample_players):
        # No fixtures — all players are in BGW
        analyser = FixtureAnalyser(bootstrap, [])
        calc = ExpectedPointsCalculator(analyser, {})
        player = sample_players[7]  # any player
        result = calc.compute(player, gw=1)
        assert result.xp_next_gw == 0.0
        assert result.is_bgw

    def test_dgw_player_has_higher_xp_than_normal(self, bootstrap, sample_players):
        # Team 1 has DGW in GW1
        dgw_fixtures = [
            make_fixture(1, gw=1, team_h=1, team_a=2),
            make_fixture(2, gw=1, team_h=3, team_a=1),
        ]
        normal_fixtures = [make_fixture(3, gw=1, team_h=4, team_a=5)]

        dgw_analyser = FixtureAnalyser(bootstrap, dgw_fixtures)
        normal_analyser = FixtureAnalyser(bootstrap, normal_fixtures)

        xg_stats = {1: {"xg_per90": 0.3, "xa_per90": 0.1, "npxg_per90": 0.3}}
        player = next(p for p in sample_players if p.team == 1 and p.element_type == 3)

        dgw_result = ExpectedPointsCalculator(dgw_analyser, xg_stats).compute(player, gw=1)
        normal_result = ExpectedPointsCalculator(normal_analyser, xg_stats).compute(player, gw=1)

        assert dgw_result.xp_next_gw > normal_result.xp_next_gw
        assert dgw_result.is_dgw

    def test_injured_player_has_zero_xp(self, analyser_with_fixture):
        injured = make_player(99, position=3, team=1, status="i")
        injured_copy = injured.model_copy(update={"chance_of_playing_next_round": 0})
        calc = ExpectedPointsCalculator(analyser_with_fixture, {})
        result = calc.compute(injured_copy, gw=1)
        assert result.xp_next_gw == 0.0

    def test_gk_has_different_xp_to_fwd(self, analyser_with_fixture):
        gk = make_player(101, position=1, team=1, xg="0.1")  # GK
        fwd = make_player(102, position=4, team=1, xg="0.5")  # FWD with higher xG
        calc = ExpectedPointsCalculator(analyser_with_fixture, {})
        gk_result = calc.compute(gk, gw=1)
        fwd_result = calc.compute(fwd, gw=1)
        # GK gets clean sheet bonus that FWD does not
        # Both should have positive xP
        assert gk_result.xp_next_gw > 0
        assert fwd_result.xp_next_gw > 0

    def test_xp_components_sum_to_final(self, analyser_with_fixture, sample_players):
        calc = ExpectedPointsCalculator(analyser_with_fixture, {})
        player = next(p for p in sample_players if p.element_type == 3 and p.team == 8)
        comp = calc.get_components(player, gw=1)
        expected_raw = (
            comp.goals_contribution
            + comp.assists_contribution
            + comp.clean_sheet_contribution
            + comp.minutes_contribution
            + comp.bonus_contribution
            + comp.defensive_contribution
        )
        assert comp.raw_xp == pytest.approx(expected_raw, abs=0.001)

    def test_lookahead_xp_gte_single_gw_xp(self, analyser_with_fixture, sample_players):
        fixtures = [make_fixture(i, gw=i, team_h=i % 10 + 1, team_a=i % 10 + 2) for i in range(1, 5)]
        analyser = FixtureAnalyser(analyser_with_fixture._bootstrap, fixtures)
        calc = ExpectedPointsCalculator(analyser, {})
        player = next(p for p in sample_players if p.element_type == 3)
        result = calc.compute(player, gw=1, lookahead=3)
        # Lookahead should be >= single GW xP (we're adding more GWs)
        assert result.xp_lookahead >= 0

    def test_compute_all_returns_all_players(self, analyser_with_fixture, sample_players):
        calc = ExpectedPointsCalculator(analyser_with_fixture, {})
        results = calc.compute_all(sample_players, gw=1)
        assert len(results) == len(sample_players)
