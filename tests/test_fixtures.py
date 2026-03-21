"""
Tests for fixture analysis: FDR, DGW/BGW detection.
"""

from __future__ import annotations

import pytest

from src.analysis.fixtures import FixtureAnalyser, TeamFixtureInfo
from src.data.models import Fixture
from tests.conftest import make_fixture, make_gameweek


class TestTeamFixtureInfo:
    def test_normal_gw(self, fixtures, bootstrap):
        analyser = FixtureAnalyser(bootstrap, fixtures)
        info = analyser.get_team_fixture_info(team_id=1, gw=1)
        assert info.fixture_count == 1
        assert not info.is_dgw
        assert not info.is_bgw
        assert info.dgw_multiplier == 1.0

    def test_dgw_detection(self, bootstrap):
        # Give team 1 two fixtures in GW2
        extra_fixtures = [
            make_fixture(100, gw=2, team_h=1, team_a=5),
            make_fixture(101, gw=2, team_h=3, team_a=1),
        ]
        analyser = FixtureAnalyser(bootstrap, extra_fixtures)
        info = analyser.get_team_fixture_info(team_id=1, gw=2)
        assert info.is_dgw
        assert info.fixture_count == 2
        assert info.dgw_multiplier == pytest.approx(1.85)

    def test_bgw_detection(self, bootstrap):
        # No fixtures for team 1 in GW2
        analyser = FixtureAnalyser(bootstrap, [])
        info = analyser.get_team_fixture_info(team_id=1, gw=2)
        assert info.is_bgw
        assert info.fixture_count == 0
        assert info.dgw_multiplier == 0.0

    def test_average_fdr(self, bootstrap):
        fixtures = [make_fixture(1, gw=1, team_h=1, team_a=2, fdr_h=2, fdr_a=4)]
        analyser = FixtureAnalyser(bootstrap, fixtures)
        # Team 1 is home, FDR for home team = fdr_h = 2
        info = analyser.get_team_fixture_info(team_id=1, gw=1)
        assert info.average_fdr == pytest.approx(2.0)
        # Team 2 is away, FDR for away team = fdr_a = 4
        info2 = analyser.get_team_fixture_info(team_id=2, gw=1)
        assert info2.average_fdr == pytest.approx(4.0)


class TestFixtureAnalyser:
    def test_get_dgw_teams(self, bootstrap):
        fixtures = [
            make_fixture(1, gw=1, team_h=1, team_a=2),
            make_fixture(2, gw=1, team_h=1, team_a=3),  # team 1 has DGW
        ]
        analyser = FixtureAnalyser(bootstrap, fixtures)
        dgw_teams = analyser.get_dgw_teams(gw=1)
        assert 1 in dgw_teams

    def test_get_bgw_teams(self, bootstrap):
        # Only teams 1 and 2 have GW1 fixtures
        fixtures = [make_fixture(1, gw=1, team_h=1, team_a=2)]
        analyser = FixtureAnalyser(bootstrap, fixtures)
        bgw_teams = analyser.get_bgw_teams(gw=1)
        # All other 18 teams should be in BGW
        assert len(bgw_teams) == 18
        assert 1 not in bgw_teams
        assert 2 not in bgw_teams

    def test_enhanced_fdr_returns_valid_range(self, bootstrap, fixtures):
        analyser = FixtureAnalyser(bootstrap, fixtures)
        for team_id in range(1, 11):
            fdr = analyser.enhanced_fdr(team_id=team_id, gw=1)
            assert 1.0 <= fdr <= 5.0, f"FDR {fdr} out of range for team {team_id}"

    def test_bgw_team_has_fdr_5(self, bootstrap):
        analyser = FixtureAnalyser(bootstrap, [])
        fdr = analyser.get_fdr(team_id=1, gw=1)
        assert fdr == 5.0  # no fixture = worst FDR

    def test_upcoming_gw_summary(self, bootstrap, fixtures):
        analyser = FixtureAnalyser(bootstrap, fixtures)
        summary = analyser.upcoming_gw_summary(gw=1, lookahead=3)
        assert len(summary) == 20  # 20 teams
        for team_id, infos in summary.items():
            assert len(infos) == 3
