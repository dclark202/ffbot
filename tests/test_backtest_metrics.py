from __future__ import annotations

from ffbot.backtest.baselines import Lineup
from ffbot.backtest.metrics import (
    Decision,
    block_bootstrap_mean_ci,
    delta_quantiles,
    discordant_deltas,
    field_win_prob_deltas,
    lineup_efficiency,
    lineups_differ,
    paired_deltas,
    points_left_on_bench,
    score_lineup,
    tail_rates,
    underdog_split,
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


class TestDecisionProjectedDefault:
    """B5 -- Decision gained a `projected` field; every prior direct
    construction (this test file's own `_decisions()` above, and anything
    else that predates B5) must keep working with no changes."""

    def test_defaults_to_empty_dict(self):
        d = Decision(season=2023, week=1, roster_index=0, points={"agent": 10.0})
        assert d.projected == {}

    def test_two_decisions_get_independent_dicts(self):
        # A mutable default via field(default_factory=dict) must never be
        # shared across instances -- the classic dataclass footgun this
        # guards against.
        a = Decision(season=2023, week=1, roster_index=0, points={})
        b = Decision(season=2023, week=1, roster_index=1, points={})
        a.projected["control"] = 5.0
        assert b.projected == {}


class TestDeltaQuantiles:
    def test_empty_returns_zeros_for_every_requested_quantile(self):
        assert delta_quantiles([], qs=(0.1, 0.5, 0.9)) == {0.1: 0.0, 0.5: 0.0, 0.9: 0.0}

    def test_median_of_odd_length_is_the_middle_value(self):
        out = delta_quantiles([1.0, 5.0, 9.0], qs=(0.5,))
        assert out[0.5] == 5.0

    def test_p10_and_p90_bracket_the_middle(self):
        values = list(range(1, 11))  # 1..10
        out = delta_quantiles([float(v) for v in values], qs=(0.1, 0.5, 0.9))
        assert out[0.1] < out[0.5] < out[0.9]


class TestTailRates:
    def test_empty_returns_zero_zero(self):
        assert tail_rates([], threshold=5.0) == (0.0, 0.0)

    def test_counts_above_and_below_threshold(self):
        values = [10.0, 10.0, -10.0, 0.0]  # 2 above +5, 1 below -5, 1 neither
        hi, lo = tail_rates(values, threshold=5.0)
        assert hi == 0.5
        assert lo == 0.25

    def test_exactly_at_threshold_is_not_counted(self):
        hi, lo = tail_rates([5.0, -5.0], threshold=5.0)
        assert hi == 0.0
        assert lo == 0.0


class TestFieldWinProbDeltas:
    def _decision(self, idx, agent, control, week=1):
        return Decision(season=2023, week=week, roster_index=idx, points={"agent": agent, "control": control})

    def test_beats_every_rival_in_the_field_is_a_full_win(self):
        decisions = [
            self._decision(0, agent=100.0, control=100.0),  # this roster's own control (irrelevant to itself)
            self._decision(1, agent=50.0, control=10.0),    # rival 1's control floor: 10
            self._decision(2, agent=50.0, control=20.0),    # rival 2's control floor: 20
        ]
        deltas = field_win_prob_deltas(decisions, "agent", "control")
        # Decision 0: agent=100 beats both rivals' control (10, 20) -> winprob 1.0
        # its own control=100 also beats both rivals' control -> winprob 1.0 -> delta 0.0
        assert deltas[0] == 0.0

    def test_paired_alignment_matches_input_order_regardless_of_block_grouping(self):
        # Two different weeks interleaved -- output must stay positionally
        # aligned with `decisions`, not reordered by internal grouping.
        decisions = [
            self._decision(0, agent=10.0, control=10.0, week=1),
            self._decision(0, agent=10.0, control=10.0, week=2),
            self._decision(1, agent=10.0, control=10.0, week=1),
        ]
        deltas = field_win_prob_deltas(decisions, "agent", "control")
        assert len(deltas) == 3

    def test_a_lone_decision_in_its_block_has_no_rivals_and_scores_zero(self):
        decisions = [self._decision(0, agent=50.0, control=10.0)]
        deltas = field_win_prob_deltas(decisions, "agent", "control")
        assert deltas == [0.0]

    def test_ties_count_as_half_a_win(self):
        decisions = [
            self._decision(0, agent=10.0, control=10.0),
            self._decision(1, agent=10.0, control=10.0),
        ]
        deltas = field_win_prob_deltas(decisions, "agent", "control")
        # decision 0's control (10) ties decision 1's control (10) exactly ->
        # both winprob(agent) and winprob(control) are 0.5 -> delta 0.0
        assert deltas[0] == 0.0


class TestUnderdogSplit:
    def _decision(self, idx, projected_control, week=1):
        d = Decision(season=2023, week=week, roster_index=idx, points={})
        d.projected["control"] = projected_control
        return d

    def test_below_median_projection_buckets_as_underdog(self):
        decisions = [self._decision(0, 10.0), self._decision(1, 20.0), self._decision(2, 30.0)]
        values = [1.0, 2.0, 3.0]
        (under_vals, under_blocks), (fav_vals, fav_blocks) = underdog_split(decisions, values)
        # median is 20.0 -- decision 0 (10.0) is strictly below -> underdog;
        # decisions 1 and 2 (20.0, 30.0) are at-or-above -> favorite.
        assert under_vals == [1.0]
        assert fav_vals == [2.0, 3.0]

    def test_block_keys_match_each_bucketed_decisions_own_block(self):
        # Two decisions in the SAME block (week 1) so the median split
        # actually has something to compare, plus one in a different block
        # (week 2) to prove block keys travel with the right bucket.
        decisions = [
            self._decision(0, 10.0, week=1), self._decision(1, 30.0, week=1),
            self._decision(2, 5.0, week=2),
        ]
        values = [1.0, 2.0, 3.0]
        (under_vals, under_blocks), (fav_vals, fav_blocks) = underdog_split(decisions, values)
        assert under_blocks == [(2023, 1)]
        assert (2023, 2) in fav_blocks  # a lone decision in its block is never below its own median

    def test_missing_projected_key_defaults_to_zero_not_a_crash(self):
        decisions = [Decision(season=2023, week=1, roster_index=0, points={})]
        (under_vals, _), (fav_vals, _) = underdog_split(decisions, [5.0])
        assert under_vals + fav_vals == [5.0]
