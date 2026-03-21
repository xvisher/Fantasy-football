"""
Tests for the chip usage decision engine.
"""

from __future__ import annotations

import pytest

from src.analysis.chip_strategy import ChipStrategy
from src.config.settings import settings
from src.data.models import ChipType


class TestChipStrategy:
    @pytest.fixture
    def all_chips(self):
        return [ChipType.WILDCARD, ChipType.FREE_HIT, ChipType.TRIPLE_CAPTAIN, ChipType.BENCH_BOOST]

    def test_no_chip_when_squad_is_strong(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=5)
        decision = strategy.evaluate(
            current_squad_xp=60.0,
            best_possible_xp=65.0,  # only 92% gap, above wildcard threshold
            bgw_starters_count=0,
            captain_xp=10.0,
            season_avg_gw_xp=50.0,
            bench_xp=15.0,
            dgw_count_in_squad=2,
        )
        assert decision.chip is None

    def test_wildcard_triggers_when_squad_weak(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=5)
        decision = strategy.evaluate(
            current_squad_xp=40.0,   # only 57% of best possible
            best_possible_xp=70.0,
            bgw_starters_count=0,
            captain_xp=8.0,
            season_avg_gw_xp=50.0,
            bench_xp=10.0,
            dgw_count_in_squad=0,
        )
        # 40/70 = 0.57 < wildcard_xp_threshold (0.85) → should trigger wildcard
        assert decision.chip == ChipType.WILDCARD

    def test_free_hit_takes_priority_over_wildcard_for_bgw(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=5)
        decision = strategy.evaluate(
            current_squad_xp=30.0,
            best_possible_xp=70.0,
            bgw_starters_count=settings.free_hit_bgw_min_starters,  # exactly at threshold
            captain_xp=5.0,
            season_avg_gw_xp=50.0,
            bench_xp=8.0,
            dgw_count_in_squad=0,
        )
        assert decision.chip == ChipType.FREE_HIT

    def test_triple_captain_triggers_on_exceptional_captain(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=10)
        captain_xp = 50.0 * settings.triple_captain_xp_multiplier + 1
        decision = strategy.evaluate(
            current_squad_xp=65.0,
            best_possible_xp=68.0,
            bgw_starters_count=0,
            captain_xp=captain_xp,
            season_avg_gw_xp=50.0,
            bench_xp=10.0,
            dgw_count_in_squad=3,
        )
        assert decision.chip == ChipType.TRIPLE_CAPTAIN

    def test_bench_boost_triggers_on_high_bench_xp_and_dgw(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=10)
        decision = strategy.evaluate(
            current_squad_xp=65.0,
            best_possible_xp=68.0,
            bgw_starters_count=0,
            captain_xp=8.0,
            season_avg_gw_xp=50.0,
            bench_xp=settings.bench_boost_min_bench_xp + 5,
            dgw_count_in_squad=9,  # > 8 threshold
        )
        assert decision.chip == ChipType.BENCH_BOOST

    def test_unavailable_chip_not_used(self):
        # No chips available
        strategy = ChipStrategy([], current_gw=5)
        decision = strategy.evaluate(
            current_squad_xp=30.0,
            best_possible_xp=80.0,
            bgw_starters_count=10,
            captain_xp=100.0,
            season_avg_gw_xp=50.0,
            bench_xp=50.0,
            dgw_count_in_squad=15,
        )
        assert decision.chip is None

    def test_is_available(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=1)
        assert strategy.is_available(ChipType.WILDCARD)
        assert strategy.is_available(ChipType.FREE_HIT)
        assert not strategy.is_available(ChipType("wildcard"))  # same test

    def test_count_bgw_in_starting(self):
        from src.data.models import PlayerWithXP
        from tests.conftest import make_player

        players = {
            i: PlayerWithXP(
                player=make_player(i, position=3, team=1),
                xp_next_gw=5.0,
                is_bgw=(i % 2 == 0),  # even IDs are BGW
            )
            for i in range(1, 12)
        }
        starting_ids = list(range(1, 12))
        count = ChipStrategy.count_bgw_in_starting(starting_ids, players)
        assert count == 5  # ids 2, 4, 6, 8, 10 → 5 BGW players

    def test_decision_has_reason(self, all_chips):
        strategy = ChipStrategy(all_chips, current_gw=5)
        decision = strategy.evaluate(
            current_squad_xp=30.0,
            best_possible_xp=70.0,
            bgw_starters_count=0,
            captain_xp=5.0,
            season_avg_gw_xp=50.0,
            bench_xp=5.0,
            dgw_count_in_squad=0,
        )
        assert len(decision.reason) > 0
        assert 0.0 <= decision.confidence <= 1.0
