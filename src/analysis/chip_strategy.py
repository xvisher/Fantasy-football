"""
Chip usage strategy engine.

Decides if and which chip to use for an upcoming gameweek based on:
  - Squad xP vs best possible xP (wildcard trigger)
  - DGW/BGW distribution (free hit, wildcard triggers)
  - Captain xP vs season average (triple captain trigger)
  - Bench xP (bench boost trigger)
  - Chips already used (tracks half-season availability)

FPL chips (per half-season):
  Wildcard  x1 — permanent squad overhaul, no hit
  Free Hit  x1 — one-GW squad overhaul, reverts next GW
  Bench Boost x1 — all 15 players score (only once per season)
  Triple Captain x1 — captain pts × 3 (only once per season)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.config.settings import settings
from src.data.models import ChipType, MyTeam, PlayerWithXP, TeamSelection

logger = logging.getLogger(__name__)

# Chips available per half-season (FPL resets at GW20)
FIRST_HALF_CHIPS = {ChipType.WILDCARD, ChipType.FREE_HIT}
SECOND_HALF_CHIPS = {ChipType.WILDCARD, ChipType.FREE_HIT}
ONCE_PER_SEASON_CHIPS = {ChipType.BENCH_BOOST, ChipType.TRIPLE_CAPTAIN}


@dataclass
class ChipDecision:
    chip: Optional[ChipType]
    reason: str
    confidence: float  # 0.0 - 1.0


class ChipStrategy:
    """
    Rule-based chip decision engine.

    Call evaluate() before each GW to get a chip recommendation.
    """

    def __init__(self, available_chips: list[ChipType], current_gw: int) -> None:
        self._available = set(available_chips)
        self._gw = current_gw

    def evaluate(
        self,
        current_squad_xp: float,
        best_possible_xp: float,
        bgw_starters_count: int,
        captain_xp: float,
        season_avg_gw_xp: float,
        bench_xp: float,
        dgw_count_in_squad: int,
    ) -> ChipDecision:
        """
        Evaluate which chip (if any) to use this GW.

        Priority order:
        1. Wildcard (squad too weak or too many BGW players)
        2. Free Hit (BGW overwhelms current squad)
        3. Triple Captain (exceptional captain opportunity)
        4. Bench Boost (DGW with strong bench)

        Returns the highest-priority recommendation, or no chip.
        """
        # 1. Free Hit — use when BGW hits too many starters
        # Free Hit is preferred over Wildcard for a single BGW because the squad reverts
        if ChipType.FREE_HIT in self._available:
            if bgw_starters_count >= settings.free_hit_bgw_min_starters:
                return ChipDecision(
                    chip=ChipType.FREE_HIT,
                    reason=(
                        f"Free Hit: {bgw_starters_count} starting players have no fixture "
                        f"(BGW threshold: {settings.free_hit_bgw_min_starters})"
                    ),
                    confidence=0.9,
                )

        # 2. Wildcard — use when squad is significantly underperforming
        if ChipType.WILDCARD in self._available:
            xp_ratio = current_squad_xp / max(best_possible_xp, 0.1)
            if xp_ratio < settings.wildcard_xp_threshold:
                return ChipDecision(
                    chip=ChipType.WILDCARD,
                    reason=(
                        f"Wildcard: squad xP ({current_squad_xp:.1f}) is only "
                        f"{xp_ratio:.0%} of best possible ({best_possible_xp:.1f})"
                    ),
                    confidence=0.85,
                )
            if bgw_starters_count >= settings.wildcard_bgw_min_players:
                return ChipDecision(
                    chip=ChipType.WILDCARD,
                    reason=(
                        f"Wildcard: {bgw_starters_count} starters affected by BGW — "
                        f"structural squad overhaul needed"
                    ),
                    confidence=0.8,
                )

        # 3. Triple Captain — exceptional captain opportunity
        if ChipType.TRIPLE_CAPTAIN in self._available and season_avg_gw_xp > 0:
            tc_threshold = season_avg_gw_xp * settings.triple_captain_xp_multiplier
            if captain_xp >= tc_threshold:
                return ChipDecision(
                    chip=ChipType.TRIPLE_CAPTAIN,
                    reason=(
                        f"Triple Captain: captain xP ({captain_xp:.1f}) exceeds "
                        f"{settings.triple_captain_xp_multiplier}× season avg GW xP "
                        f"({season_avg_gw_xp:.1f} × {settings.triple_captain_xp_multiplier} = {tc_threshold:.1f})"
                    ),
                    confidence=0.75,
                )

        # 4. Bench Boost — DGW where bench also has fixtures
        if ChipType.BENCH_BOOST in self._available:
            if bench_xp >= settings.bench_boost_min_bench_xp and dgw_count_in_squad >= 8:
                return ChipDecision(
                    chip=ChipType.BENCH_BOOST,
                    reason=(
                        f"Bench Boost: bench xP={bench_xp:.1f} "
                        f"(min={settings.bench_boost_min_bench_xp}), "
                        f"{dgw_count_in_squad} DGW players in squad"
                    ),
                    confidence=0.7,
                )

        return ChipDecision(chip=None, reason="No chip conditions met this GW", confidence=1.0)

    def compute_season_avg_gw_xp(self, all_gw_scores: list[float]) -> float:
        """Compute rolling season average GW score for TC threshold."""
        if not all_gw_scores:
            return 50.0  # default before data available
        return sum(all_gw_scores) / len(all_gw_scores)

    def is_available(self, chip: ChipType) -> bool:
        return chip in self._available

    @staticmethod
    def count_bgw_in_starting(
        starting_ids: list[int],
        players_with_xp: dict[int, PlayerWithXP],
    ) -> int:
        return sum(
            1 for pid in starting_ids
            if players_with_xp.get(pid) and players_with_xp[pid].is_bgw
        )

    @staticmethod
    def count_dgw_in_squad(
        all_ids: list[int],
        players_with_xp: dict[int, PlayerWithXP],
    ) -> int:
        return sum(
            1 for pid in all_ids
            if players_with_xp.get(pid) and players_with_xp[pid].is_dgw
        )
