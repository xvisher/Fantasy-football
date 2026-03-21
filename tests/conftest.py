"""
Shared test fixtures and mock data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.data.models import (
    BootstrapData,
    Fixture,
    Gameweek,
    Player,
    Position,
    Team,
    MyTeam,
    Pick,
    TransferInfo,
    ChipStatus,
    ChipType,
    PlayerWithXP,
)


def make_team(id: int, name: str = None) -> Team:
    return Team(
        id=id,
        name=name or f"Team {id}",
        short_name=f"T{id}",
        strength=3,
        strength_overall_home=1200,
        strength_overall_away=1100,
        strength_attack_home=1200,
        strength_attack_away=1100,
        strength_defence_home=1100,
        strength_defence_away=1000,
    )


def make_player(
    id: int,
    position: int = 3,  # MID
    team: int = 1,
    cost: int = 80,  # £8.0m
    status: str = "a",
    form: str = "5.0",
    xg: str = "3.0",
    xa: str = "2.0",
    minutes: int = 900,
    total_points: int = 60,
) -> Player:
    return Player(
        id=id,
        first_name="Player",
        second_name=f"Test{id}",
        web_name=f"Player{id}",
        team=team,
        element_type=position,
        now_cost=cost,
        cost_change_start=0,
        selected_by_percent="10.0",
        status=status,
        chance_of_playing_next_round=100,
        form=form,
        points_per_game="6.0",
        total_points=total_points,
        event_points=0,
        minutes=minutes,
        goals_scored=3,
        assists=2,
        clean_sheets=5,
        goals_conceded=10,
        own_goals=0,
        penalties_saved=0,
        penalties_missed=0,
        yellow_cards=1,
        red_cards=0,
        saves=0,
        bonus=5,
        bps=200,
        influence="100.0",
        creativity="80.0",
        threat="90.0",
        ict_index="40.0",
        expected_goals=xg,
        expected_assists=xa,
        expected_goal_involvements="5.0",
        expected_goals_conceded="10.0",
        news="",
    )


def make_fixture(
    id: int,
    gw: int,
    team_h: int,
    team_a: int,
    fdr_h: int = 3,
    fdr_a: int = 3,
) -> Fixture:
    return Fixture(
        id=id,
        event=gw,
        team_h=team_h,
        team_a=team_a,
        team_h_difficulty=fdr_h,
        team_a_difficulty=fdr_a,
        finished=False,
        started=False,
        kickoff_time=datetime(2027, 8, 14, 12, 30, tzinfo=timezone.utc),
    )


def make_gameweek(id: int, is_next: bool = False, finished: bool = False) -> Gameweek:
    return Gameweek(
        id=id,
        name=f"Gameweek {id}",
        deadline_time=datetime(2027, 8, 13, 11, 0, tzinfo=timezone.utc),
        finished=finished,
        is_current=False,
        is_next=is_next,
    )


@pytest.fixture
def teams() -> list[Team]:
    return [make_team(i) for i in range(1, 21)]  # 20 teams


@pytest.fixture
def sample_players() -> list[Player]:
    """15-player squad worth ~£100m with valid FPL positions."""
    players = []
    # 2 GKs
    players.append(make_player(1, position=1, team=1, cost=55))
    players.append(make_player(2, position=1, team=2, cost=45))
    # 5 DEFs
    for i in range(3, 8):
        players.append(make_player(i, position=2, team=i % 20 + 1, cost=55 + i))
    # 5 MIDs
    for i in range(8, 13):
        players.append(make_player(i, position=3, team=i % 20 + 1, cost=75 + i))
    # 3 FWDs
    for i in range(13, 16):
        players.append(make_player(i, position=4, team=i % 20 + 1, cost=80 + i))
    return players


@pytest.fixture
def all_players(sample_players) -> list[Player]:
    """Larger pool of 30 players for optimiser tests."""
    extra = []
    for i in range(16, 31):
        pos = (i % 4) + 1
        extra.append(make_player(i, position=pos, team=i % 20 + 1, cost=60 + i))
    return sample_players + extra


@pytest.fixture
def fixtures(teams) -> list[Fixture]:
    """Simple round-robin fixtures for GW1."""
    fixts = []
    for i, (h, a) in enumerate(zip(range(1, 11), range(11, 21)), start=1):
        fixts.append(make_fixture(i, gw=1, team_h=h, team_a=a))
    return fixts


@pytest.fixture
def bootstrap(sample_players, teams) -> BootstrapData:
    gws = [make_gameweek(i, is_next=(i == 1)) for i in range(1, 39)]
    return BootstrapData(events=gws, teams=teams, elements=sample_players)


@pytest.fixture
def my_team(sample_players) -> MyTeam:
    picks = []
    for pos, player in enumerate(sample_players, start=1):
        picks.append(Pick(
            element=player.id,
            position=pos,
            is_captain=(pos == 1),
            is_vice_captain=(pos == 2),
            multiplier=2 if pos == 1 else 1,
        ))
    return MyTeam(
        picks=picks,
        chips=[
            ChipStatus(
                name=ChipType.WILDCARD,
                status_for_entry="available",
                played_by_entry=[],
            ),
            ChipStatus(
                name=ChipType.TRIPLE_CAPTAIN,
                status_for_entry="available",
                played_by_entry=[],
            ),
            ChipStatus(
                name=ChipType.BENCH_BOOST,
                status_for_entry="available",
                played_by_entry=[],
            ),
            ChipStatus(
                name=ChipType.FREE_HIT,
                status_for_entry="available",
                played_by_entry=[],
            ),
        ],
        transfers=TransferInfo(
            cost=0,
            status="cost",
            limit=1,
            made=0,
            bank=5,    # £0.5m bank
            value=1000, # £100m squad value
        ),
    )
