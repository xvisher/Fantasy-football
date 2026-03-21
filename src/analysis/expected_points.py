"""
Expected Points (xP) model.

Calculates a predicted point score for each player for an upcoming gameweek,
combining:
  - xG / xA from Understat (or FPL season stats as fallback)
  - Position-specific scoring values from settings
  - Clean sheet probability (based on team defensive strength and FDR)
  - Minutes probability (based on form, status, chance_of_playing)
  - Bonus point estimate
  - Defensive contribution estimate (DEF/MID, 2025/26 rule)
  - DGW multiplier (from fixture analyser)

Weights are initially from settings and overridden by learned weights from
the DB once GW >= settings.learner_min_gws.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.analysis.fixtures import FixtureAnalyser
from src.config.settings import settings
from src.data.models import Player, PlayerWithXP, Position

logger = logging.getLogger(__name__)


@dataclass
class XPComponents:
    """Breakdown of xP contributions for transparency / audit logging."""
    player_id: int
    gw: int
    goals_contribution: float = 0.0
    assists_contribution: float = 0.0
    clean_sheet_contribution: float = 0.0
    minutes_contribution: float = 0.0
    bonus_contribution: float = 0.0
    defensive_contribution: float = 0.0
    fixture_multiplier: float = 1.0
    raw_xp: float = 0.0
    final_xp: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__


class ExpectedPointsCalculator:
    """
    Compute xP scores for all players.

    Weights can be overridden (by the learner) at runtime.
    """

    def __init__(
        self,
        fixture_analyser: FixtureAnalyser,
        xg_stats: dict[int, dict],   # {player_id: {xg_per90, xa_per90, ...}}
        model_weights: Optional[dict] = None,
    ) -> None:
        self._fixtures = fixture_analyser
        self._xg_stats = xg_stats
        self._weights = model_weights or {}

    # ── Weights (fall back to settings if not in learned weights) ─────────────

    @property
    def w_xg(self) -> float:
        return self._weights.get("xp_weight_xg", settings.xp_weight_xg)

    @property
    def w_xa(self) -> float:
        return self._weights.get("xp_weight_xa", settings.xp_weight_xa)

    @property
    def w_cs(self) -> float:
        return self._weights.get("xp_weight_cs", settings.xp_weight_cs)

    @property
    def w_bonus(self) -> float:
        return self._weights.get("xp_weight_bonus", settings.xp_weight_bonus)

    @property
    def w_minutes(self) -> float:
        return self._weights.get("xp_weight_minutes", settings.xp_weight_minutes)

    @property
    def w_fdr(self) -> float:
        return self._weights.get("xp_weight_fdr", settings.xp_weight_fdr)

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(self, player: Player, gw: int, lookahead: int = 1) -> PlayerWithXP:
        """
        Compute xP for a player for a single GW and optionally a lookahead window.

        Args:
            player: FPL Player object
            gw: target gameweek
            lookahead: number of additional GWs to include (weighted by decay)

        Returns PlayerWithXP with .xp_next_gw and .xp_lookahead populated.
        """
        xp_gw, _ = self._xp_for_gw(player, gw)
        fixture_info = self._fixtures.get_team_fixture_info(player.team, gw)

        # Lookahead weighted sum
        xp_lookahead = xp_gw
        decay = settings.lookahead_decay
        weight = decay
        for future_gw in range(gw + 1, gw + lookahead):
            future_xp, _ = self._xp_for_gw(player, future_gw)
            xp_lookahead += future_xp * weight
            weight *= decay

        return PlayerWithXP(
            player=player,
            xp_next_gw=round(xp_gw, 3),
            xp_lookahead=round(xp_lookahead, 3),
            fixture_count_next_gw=fixture_info.fixture_count,
            is_dgw=fixture_info.is_dgw,
            is_bgw=fixture_info.is_bgw,
            fdr_next_gw=round(fixture_info.average_fdr, 2),
        )

    def compute_all(
        self,
        players: list[Player],
        gw: int,
        lookahead: int = 1,
    ) -> list[PlayerWithXP]:
        """Compute xP for a list of players."""
        return [self.compute(p, gw, lookahead) for p in players]

    def get_components(self, player: Player, gw: int) -> XPComponents:
        """Return xP breakdown for audit/debug."""
        _, components = self._xp_for_gw(player, gw)
        return components

    # ── Internal ──────────────────────────────────────────────────────────────

    def _xp_for_gw(self, player: Player, gw: int) -> tuple[float, XPComponents]:
        comp = XPComponents(player_id=player.id, gw=gw)
        pos = player.position

        # Availability check
        if not player.available:
            comp.final_xp = 0.0
            return 0.0, comp

        # Fixture info
        fixture_info = self._fixtures.get_team_fixture_info(player.team, gw)
        comp.fixture_multiplier = fixture_info.dgw_multiplier
        if fixture_info.is_bgw:
            comp.final_xp = 0.0
            return 0.0, comp

        # xG / xA — prefer Understat per90, fall back to FPL season rate
        xg_data = self._xg_stats.get(player.id)
        if xg_data and xg_data.get("xg_per90", 0) > 0:
            xg_per90 = xg_data["xg_per90"]
            xa_per90 = xg_data["xa_per90"]
        else:
            # FPL season totals / season minutes as rate
            season_mins = max(player.minutes, 1)
            xg_per90 = (player.xg_season / season_mins) * 90
            xa_per90 = (player.xa_season / season_mins) * 90

        # FDR adjustment (reduce xP for hard fixtures, increase for easy)
        fdr = self._fixtures.enhanced_fdr(player.team, gw)
        fdr_adj = 1.0 - (fdr - 3.0) * self.w_fdr  # fdr=3 neutral, 5=hardest (-30%), 1=easiest (+30%)
        fdr_adj = max(0.5, min(1.5, fdr_adj))

        # Goal contribution
        goal_pts = settings.goal_points.get(pos.value, 4)
        comp.goals_contribution = xg_per90 * goal_pts * self.w_xg * fdr_adj

        # Assist contribution
        comp.assists_contribution = xa_per90 * settings.assist_points * self.w_xa * fdr_adj

        # Clean sheet probability
        cs_pts = settings.clean_sheet_points.get(pos.value, 0)
        if cs_pts > 0:
            cs_prob = self._estimate_cs_probability(player.team, gw, fdr)
            comp.clean_sheet_contribution = cs_prob * cs_pts * self.w_cs

        # Minutes probability (weighted by availability)
        mins_prob = self._estimate_minutes_probability(player)
        minutes_pts = self._expected_minutes_points(mins_prob)
        comp.minutes_contribution = minutes_pts * self.w_minutes

        # Bonus estimate (based on typical form)
        try:
            form = float(player.form)
        except (ValueError, TypeError):
            form = 0.0
        avg_bonus_per_game = min(1.5, form * 0.15)  # rough heuristic
        comp.bonus_contribution = avg_bonus_per_game * self.w_bonus

        # Defensive contribution (DEF/MID new rule)
        if pos == Position.DEF:
            # ~1 bonus point per game if regularly making contributions
            comp.defensive_contribution = mins_prob * 0.8
        elif pos == Position.MID:
            comp.defensive_contribution = mins_prob * 0.4

        # Raw xP per game
        raw = (
            comp.goals_contribution
            + comp.assists_contribution
            + comp.clean_sheet_contribution
            + comp.minutes_contribution
            + comp.bonus_contribution
            + comp.defensive_contribution
        )
        comp.raw_xp = raw

        # Apply fixture multiplier (DGW doubles, BGW already handled above)
        comp.final_xp = raw * comp.fixture_multiplier
        return comp.final_xp, comp

    def _estimate_cs_probability(self, team_id: int, gw: int, fdr: float) -> float:
        """
        Estimate probability of a clean sheet based on FDR.

        Historical Premier League clean sheet rates:
          - FDR 1 (easiest): ~40%
          - FDR 2: ~30%
          - FDR 3: ~22%
          - FDR 4: ~15%
          - FDR 5 (hardest): ~8%
        """
        cs_rate_by_fdr = {1: 0.40, 2: 0.30, 3: 0.22, 4: 0.15, 5: 0.08}
        fdr_int = max(1, min(5, round(fdr)))
        return cs_rate_by_fdr.get(fdr_int, 0.22)

    def _estimate_minutes_probability(self, player: Player) -> float:
        """
        Estimate probability player plays 60+ minutes.

        Uses chance_of_playing (if available), status, and form as proxy.
        """
        cop = player.chance_of_playing_next_round
        if cop is not None:
            base = cop / 100.0
        elif player.status == "a":
            # Available — estimate based on minutes played rate this season
            season_gws = max(1, player.total_points // max(float(player.points_per_game or 1), 1))
            avg_mins = player.minutes / max(season_gws, 1)
            base = min(1.0, avg_mins / 90.0)
        else:
            base = 0.0

        # Players with <30% play chance treated as doubtful
        return max(0.0, min(1.0, base))

    def _expected_minutes_points(self, mins_prob: float) -> float:
        """
        Expected points from appearance bonus.
        P(60+ mins) × 2 + P(1-59 mins) × 1
        """
        prob_60_plus = mins_prob * 0.75   # of those who play, ~75% play 60+
        prob_1_to_59 = mins_prob * 0.25
        return (
            prob_60_plus * settings.minutes_played_2
            + prob_1_to_59 * settings.minutes_played_1
        )
