"""
Pydantic models for all FPL data structures.

These mirror the FPL API response shapes and add computed fields used
throughout the analysis layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class Position(str, Enum):
    GKP = "GKP"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"


POSITION_MAP: dict[int, Position] = {1: Position.GKP, 2: Position.DEF, 3: Position.MID, 4: Position.FWD}


class ChipType(str, Enum):
    WILDCARD = "wildcard"
    FREE_HIT = "freehit"
    BENCH_BOOST = "bboost"
    TRIPLE_CAPTAIN = "3xc"


class GameweekStatus(str, Enum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    FINISHED = "finished"


# ── Core API Models ───────────────────────────────────────────────────────────

class Team(BaseModel):
    id: int
    name: str
    short_name: str
    strength: int
    strength_overall_home: int
    strength_overall_away: int
    strength_attack_home: int
    strength_attack_away: int
    strength_defence_home: int
    strength_defence_away: int


class Player(BaseModel):
    id: int
    first_name: str
    second_name: str
    web_name: str
    team: int
    element_type: int  # 1=GKP, 2=DEF, 3=MID, 4=FWD
    now_cost: int      # price in tenths (e.g. 80 = £8.0m)
    cost_change_start: int  # change from season start (tenths)
    selected_by_percent: str
    status: str        # 'a'=available, 'i'=injured, 'd'=doubt, 's'=suspended, 'u'=unavailable
    chance_of_playing_next_round: Optional[int] = None  # 0, 25, 50, 75, 100
    form: str          # rolling average points (string from API)
    points_per_game: str
    total_points: int
    event_points: int
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    bps: int
    influence: str
    creativity: str
    threat: str
    ict_index: str
    expected_goals: str       # xG season total (string)
    expected_assists: str     # xA season total
    expected_goal_involvements: str
    expected_goals_conceded: str
    news: str = ""
    news_added: Optional[datetime] = None

    @property
    def position(self) -> Position:
        return POSITION_MAP[self.element_type]

    @property
    def price(self) -> float:
        return self.now_cost / 10.0

    @property
    def sell_price(self) -> float:
        """FPL applies profit-halving on rises; use now_cost as conservative estimate."""
        return self.now_cost / 10.0

    @property
    def available(self) -> bool:
        cop = self.chance_of_playing_next_round
        return self.status == "a" or (cop is not None and cop >= 75)

    @property
    def xg_season(self) -> float:
        try:
            return float(self.expected_goals)
        except (ValueError, TypeError):
            return 0.0

    @property
    def xa_season(self) -> float:
        try:
            return float(self.expected_assists)
        except (ValueError, TypeError):
            return 0.0


class Fixture(BaseModel):
    id: int
    event: Optional[int]        # gameweek number (None if unscheduled)
    team_h: int                 # home team id
    team_a: int                 # away team id
    team_h_difficulty: int      # FDR for home team (1=easy, 5=hard)
    team_a_difficulty: int      # FDR for away team
    finished: bool
    started: bool
    kickoff_time: Optional[datetime] = None
    team_h_score: Optional[int] = None
    team_a_score: Optional[int] = None


class Gameweek(BaseModel):
    id: int
    name: str
    deadline_time: datetime
    finished: bool
    is_current: bool
    is_next: bool
    average_entry_score: Optional[int] = None
    highest_score: Optional[int] = None
    most_selected: Optional[int] = None
    most_transferred_in: Optional[int] = None
    top_element: Optional[int] = None


class BootstrapData(BaseModel):
    """Top-level response from /bootstrap-static/"""
    events: list[Gameweek]
    teams: list[Team]
    elements: list[Player]

    @property
    def current_gw(self) -> Optional[Gameweek]:
        for gw in self.events:
            if gw.is_current:
                return gw
        return None

    @property
    def next_gw(self) -> Optional[Gameweek]:
        for gw in self.events:
            if gw.is_next:
                return gw
        return None

    @property
    def team_map(self) -> dict[int, Team]:
        return {t.id: t for t in self.teams}

    @property
    def player_map(self) -> dict[int, Player]:
        return {p.id: p for p in self.elements}


# ── My Team (authenticated) ───────────────────────────────────────────────────

class Pick(BaseModel):
    element: int        # player id
    position: int       # 1-11 = starting, 12-15 = bench
    is_captain: bool
    is_vice_captain: bool
    multiplier: int     # 1 = normal, 2 = captain, 3 = triple captain


class ChipStatus(BaseModel):
    name: ChipType
    status_for_entry: str   # 'available', 'played', 'unavailable'
    played_by_entry: list[int]  # GW numbers where used


class MyTeam(BaseModel):
    picks: list[Pick]
    chips: list[ChipStatus]
    transfers: TransferInfo

    @property
    def starting_ids(self) -> list[int]:
        return [p.element for p in self.picks if p.position <= 11]

    @property
    def bench_ids(self) -> list[int]:
        return [p.element for p in self.picks if p.position > 11]

    @property
    def all_ids(self) -> list[int]:
        return [p.element for p in self.picks]

    @property
    def captain_id(self) -> Optional[int]:
        for p in self.picks:
            if p.is_captain:
                return p.element
        return None

    @property
    def available_chips(self) -> list[ChipType]:
        return [c.name for c in self.chips if c.status_for_entry == "available"]


class TransferInfo(BaseModel):
    cost: int             # points cost of transfers this GW (before any hit)
    status: str
    limit: Optional[int]  # None = unlimited (wildcard / free hit)
    made: int             # transfers made this GW
    bank: int             # available bank (tenths £)
    value: int            # squad value (tenths £)

    @property
    def free_transfers(self) -> int:
        if self.limit is None:
            return 99
        return max(0, self.limit - self.made)

    @property
    def bank_value(self) -> float:
        return self.bank / 10.0

    @property
    def squad_value(self) -> float:
        return self.value / 10.0


# ── Player history (for xG rolling window) ───────────────────────────────────

class PlayerGameweekHistory(BaseModel):
    """From /element-summary/{id}/ → history[]"""
    element: int
    fixture: int
    opponent_team: int
    total_points: int
    was_home: bool
    kickoff_time: datetime
    team_h_score: Optional[int]
    team_a_score: Optional[int]
    round: int               # gameweek number
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    bps: int
    expected_goals: str
    expected_assists: str
    expected_goal_involvements: str
    expected_goals_conceded: str
    value: int               # price this GW (tenths)
    selected: int            # ownership count

    @property
    def xg(self) -> float:
        try:
            return float(self.expected_goals)
        except (ValueError, TypeError):
            return 0.0

    @property
    def xa(self) -> float:
        try:
            return float(self.expected_assists)
        except (ValueError, TypeError):
            return 0.0


# ── Optimiser output ──────────────────────────────────────────────────────────

class TransferRecommendation(BaseModel):
    player_out_id: int
    player_in_id: int
    predicted_xp_gain: float
    hit_cost: int           # points cost of this transfer (0 if free)
    net_xp_gain: float      # predicted_xp_gain - hit_cost


class TeamSelection(BaseModel):
    starting_ids: list[int]    # 11 player ids
    bench_ids: list[int]       # 4 player ids (in bench order)
    captain_id: int
    vice_captain_id: int
    chip: Optional[ChipType] = None
    transfers: list[TransferRecommendation] = Field(default_factory=list)
    predicted_gw_xp: float = 0.0
    reasoning: str = ""


# ── Decision audit log ────────────────────────────────────────────────────────

class DecisionLog(BaseModel):
    gw: int
    timestamp: datetime
    action_type: str          # 'transfer', 'captain', 'chip', 'team_selection'
    player_in_id: Optional[int] = None
    player_out_id: Optional[int] = None
    chip_used: Optional[ChipType] = None
    predicted_xp: float = 0.0
    actual_pts: Optional[float] = None   # filled after GW resolves
    reasoning: str = ""


# ── xP enriched player ───────────────────────────────────────────────────────

class PlayerWithXP(BaseModel):
    player: Player
    xp_next_gw: float = 0.0
    xp_lookahead: float = 0.0   # weighted sum over lookahead GWs
    fixture_count_next_gw: int = 1
    is_dgw: bool = False
    is_bgw: bool = False
    fdr_next_gw: float = 3.0    # average FDR for next GW fixtures

    @property
    def ownership_pct(self) -> float:
        try:
            return float(self.player.selected_by_percent)
        except (ValueError, TypeError):
            return 0.0
