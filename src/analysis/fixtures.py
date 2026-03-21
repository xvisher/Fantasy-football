"""
Fixture analysis: FDR calculation, Double/Blank Gameweek detection.

The FPL API provides a basic Fixture Difficulty Rating (FDR 1-5) per fixture.
We enhance this with relative team strength from bootstrap data and detect
DGW/BGW for every team across all gameweeks.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Fixture, BootstrapData, Team

logger = logging.getLogger(__name__)


@dataclass
class TeamFixtureInfo:
    """Fixture metadata for one team in one gameweek."""
    team_id: int
    gw: int
    fixtures: list[Fixture] = field(default_factory=list)

    @property
    def fixture_count(self) -> int:
        return len(self.fixtures)

    @property
    def is_dgw(self) -> bool:
        return self.fixture_count >= 2

    @property
    def is_bgw(self) -> bool:
        return self.fixture_count == 0

    @property
    def average_fdr(self) -> float:
        """Average fixture difficulty rating across all fixtures this GW."""
        if not self.fixtures:
            return 5.0  # worst possible — no fixture = no points
        fdrs = []
        for f in self.fixtures:
            # Determine if team is home or away
            if f.team_h == self.team_id:
                fdrs.append(f.team_h_difficulty)
            else:
                fdrs.append(f.team_a_difficulty)
        return sum(fdrs) / len(fdrs)

    @property
    def dgw_multiplier(self) -> float:
        """
        Multiplier for expected points due to fixture count.
        DGW = 2.0 (two chances to score), BGW = 0.0, normal = 1.0.
        Slightly less than exact double for DGW because rotation risk increases.
        """
        if self.is_bgw:
            return 0.0
        if self.is_dgw:
            return 1.85  # conservative double (rotation / fatigue discount)
        return 1.0


class FixtureAnalyser:
    """
    Builds a complete fixture map for the season and provides
    per-team-per-GW fixture lookups.
    """

    def __init__(self, bootstrap: BootstrapData, fixtures: list[Fixture]) -> None:
        self._bootstrap = bootstrap
        self._fixtures = fixtures
        self._team_map: dict[int, Team] = bootstrap.team_map
        # {team_id: {gw: TeamFixtureInfo}}
        self._fixture_map: dict[int, dict[int, TeamFixtureInfo]] = {}
        self._build_fixture_map()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_team_fixture_info(self, team_id: int, gw: int) -> TeamFixtureInfo:
        """Get fixture info for a team in a specific GW."""
        return self._fixture_map.get(team_id, {}).get(
            gw,
            TeamFixtureInfo(team_id=team_id, gw=gw, fixtures=[]),
        )

    def get_dgw_teams(self, gw: int) -> list[int]:
        """Return team IDs that have a Double Gameweek in the given GW."""
        result = []
        for team_id, gw_map in self._fixture_map.items():
            info = gw_map.get(gw)
            if info and info.is_dgw:
                result.append(team_id)
        return result

    def get_bgw_teams(self, gw: int) -> list[int]:
        """Return team IDs that have a Blank Gameweek in the given GW."""
        all_team_ids = set(t.id for t in self._bootstrap.teams)
        teams_with_fixtures = set()
        for team_id, gw_map in self._fixture_map.items():
            info = gw_map.get(gw)
            if info and info.fixture_count > 0:
                teams_with_fixtures.add(team_id)
        return list(all_team_ids - teams_with_fixtures)

    def get_fixture_count(self, team_id: int, gw: int) -> int:
        return self.get_team_fixture_info(team_id, gw).fixture_count

    def get_fdr(self, team_id: int, gw: int) -> float:
        return self.get_team_fixture_info(team_id, gw).average_fdr

    def get_dgw_multiplier(self, team_id: int, gw: int) -> float:
        return self.get_team_fixture_info(team_id, gw).dgw_multiplier

    def upcoming_gw_summary(self, gw: int, lookahead: int = 3) -> dict[int, list[TeamFixtureInfo]]:
        """
        For each team, return fixture info for the next `lookahead` GWs starting from `gw`.

        Returns: {team_id: [TeamFixtureInfo, ...]}
        """
        result: dict[int, list[TeamFixtureInfo]] = {}
        for team_id in self._team_map:
            infos = []
            for g in range(gw, gw + lookahead):
                infos.append(self.get_team_fixture_info(team_id, g))
            result[team_id] = infos
        return result

    def enhanced_fdr(self, team_id: int, gw: int) -> float:
        """
        Enhanced FDR using relative team strength from bootstrap.
        Blends API FDR with attack/defence strength differentials.

        Returns a 1-5 score (lower = easier).
        """
        info = self.get_team_fixture_info(team_id, gw)
        if not info.fixtures:
            return 5.0

        scores = []
        for f in info.fixtures:
            is_home = f.team_h == team_id
            opponent_id = f.team_a if is_home else f.team_h

            our_team = self._team_map.get(team_id)
            opp_team = self._team_map.get(opponent_id)

            if not our_team or not opp_team:
                scores.append(info.average_fdr)
                continue

            # Attack vs defence differential (normalised to 1-5 scale)
            if is_home:
                our_attack = our_team.strength_attack_home
                opp_defence = opp_team.strength_defence_away
            else:
                our_attack = our_team.strength_attack_away
                opp_defence = opp_team.strength_defence_home

            # Higher our_attack and lower opp_defence → easier fixture
            differential = (opp_defence - our_attack) / 400.0  # normalise to ~-1..1
            api_fdr = f.team_h_difficulty if is_home else f.team_a_difficulty
            blended = api_fdr + differential * 1.5  # blend: 70% API FDR, 30% strength diff
            scores.append(max(1.0, min(5.0, blended)))

        return sum(scores) / len(scores)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_fixture_map(self) -> None:
        """Populate self._fixture_map from raw fixtures."""
        # Initialise all teams with all GWs empty
        total_gws = len(self._bootstrap.events)
        for team in self._bootstrap.teams:
            self._fixture_map[team.id] = {
                gw: TeamFixtureInfo(team_id=team.id, gw=gw)
                for gw in range(1, total_gws + 1)
            }

        scheduled = [f for f in self._fixtures if f.event is not None]
        for fixture in scheduled:
            gw = fixture.event
            for team_id in (fixture.team_h, fixture.team_a):
                if team_id in self._fixture_map and gw in self._fixture_map[team_id]:
                    self._fixture_map[team_id][gw].fixtures.append(fixture)

        # Log DGW/BGW summary
        dgw_count = sum(
            1
            for gw_map in self._fixture_map.values()
            for info in gw_map.values()
            if info.is_dgw
        )
        logger.info(
            "Fixture map built: %d teams, %d GWs, %d DGW slots detected",
            len(self._bootstrap.teams),
            total_gws,
            dgw_count,
        )
