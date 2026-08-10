from __future__ import annotations

from ffbot.backtest.baselines import Lineup
from ffbot.backtest.metrics import (
    Decision,
    block_bootstrap_mean_ci,
    discordant_deltas,
    lineup_efficiency,
    lineups_differ,
    paired_deltas,
    points_left_on_bench,
    score_lineup,
)
from tests.conftest import mk


def _lineup(name: str, players) -> Lineup:
    return Lineup(name=name, assignments=[("WR", p) for p in players], bench=[])


class TestScoreLineup:
    def test_sums_realized_points_via_key_lookup(self):
        p1, p2 = mk("A", "WR"), mk("B", "WR")
        lineup = _lineup("x", [p1, p2])
        key_by_id = {p1.player_id: "a:WR", p2.player_id: "b:WR"}
        actuals = {"a:WR": 10.0, "b:WR": 5.0}
        assert score_lineup(lineup, actuals, key_by_id) == 15.0

    def test_missing_key_scores_zero_not_an_error(self):
        p1 = mk("A", "WR")
        lineup = _lineup("x", [p1])
        assert score_lineup(lineup, {}, {}) == 0.0


class TestLineupEfficiency:
    def test_normal_ratio(self):
        assert lineup_efficiency(80.0, 100.0) == 0.8

    def test_clamped_above_one(self):
        assert lineup_efficiency(150.0, 100.0) == 1.0

    def test_clamped_below_zero(self):
        assert lineup_efficiency(-10.0, 100.0) == 0.0

    def test_nonpositive_oracle_matching_or_beating_is_one(self):
        assert lineup_efficiency(0.0, 0.0) == 1.0
        assert lineup_efficiency(5.0, -3.0) == 1.0

    def test_nonpositive_oracle_falling_short_is_zero(self):
        assert lineup_efficiency(-5.0, 0.0) == 0.0


class TestPointsLeftOnBench:
    def test_gap_to_oracle(self):
        assert points_left_on_bench(80.0, 100.0) == 20.0

    def test_never_negative(self):
        assert points_left_on_bench(120.0, 100.0) == 0.0


class TestLineupsDiffer:
    def test_same_players_same_slots_do_not_differ(self):
        p1, p2 = mk("A", "WR"), mk("B", "RB")
        a = _lineup("a", [p1, p2])
        b = _lineup("b", [p1, p2])
        assert not lineups_differ(a, b)

    def test_different_player_set_differs(self):
        p1, p2, p3 = mk("A", "WR"), mk("B", "RB"), mk("C", "TE")
        a = _lineup("a", [p1, p2])
        b = _lineup("b", [p1, p3])
        assert lineups_differ(a, b)


class TestPairedAndDiscordantDeltas:
    def _decisions(self):
        return [
            Decision(season=2023, week=1, roster_index=0, points={"agent": 10.0, "control": 8.0}),
            Decision(season=2023, week=1, roster_index=1, points={"agent": 5.0, "control": 5.0}),
        ]

    def test_paired_deltas_are_agent_minus_control(self):
        deltas = paired_deltas(self._decisions(), "agent", "control")
        assert deltas == [2.0, 0.0]

    def test_discordant_deltas_only_include_differing_lineups(self):
        p1, p2 = mk("A", "WR"), mk("B", "WR")
        same = _lineup("same", [p1])
        different_a = _lineup("agent", [p1])
        different_c = _lineup("control", [p2])
        decisions = self._decisions()
        lineups = [
            {"agent": different_a, "control": different_c},  # differs -> counts
            {"agent": same, "control": same},                 # identical -> excluded
        ]
        out = discordant_deltas(decisions, lineups, "agent", "control")
        assert out == [2.0]


class TestBlockBootstrapMeanCi:
    def test_empty_values_returns_zeros(self):
        assert block_bootstrap_mean_ci([], []) == (0.0, 0.0, 0.0)

    def test_single_block_returns_point_estimate_as_its_own_ci(self):
        values = [1.0, 2.0, 3.0]
        keys = [(2023, 1), (2023, 1), (2023, 1)]
        mean, lo, hi = block_bootstrap_mean_ci(values, keys)
        assert mean == lo == hi == 2.0

    def test_ci_brackets_the_true_mean_for_a_multi_block_sample(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        keys = [(2023, w) for w in (1, 1, 2, 2, 3, 3)]
        mean, lo, hi = block_bootstrap_mean_ci(values, keys, iterations=500, seed=3)
        assert lo <= mean <= hi

    def test_deterministic_given_seed(self):
        values = [1.0, 5.0, 2.0, 8.0]
        keys = [(2023, 1), (2023, 2), (2023, 3), (2023, 4)]
        r1 = block_bootstrap_mean_ci(values, keys, iterations=200, seed=9)
        r2 = block_bootstrap_mean_ci(values, keys, iterations=200, seed=9)
        assert r1 == r2
