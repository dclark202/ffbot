from __future__ import annotations

import random

from ffbot.backtest.rosters import _target_counts, sample_roster, sample_rosters
from ffbot.models import roster_capacity

_STANDARD = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1}


def _pool_row(name: str, position: str) -> dict:
    return {"key": f"{name.lower()}:{position}", "name": name, "position": position, "team": "MIA"}


def _pool(position: str, n: int) -> list[dict]:
    return [_pool_row(f"{position}{i}", position) for i in range(n)]


class TestTargetCounts:
    def test_sums_exactly_to_capacity(self):
        for capacity in (10, 13, 19, 25):
            counts = _target_counts(capacity)
            assert sum(counts.values()) == capacity

    def test_rb_and_wr_never_below_two(self):
        counts = _target_counts(6)  # a tiny capacity that would otherwise round below 2
        assert counts["RB"] >= 2
        assert counts["WR"] >= 2

    def test_every_position_present(self):
        counts = _target_counts(19)
        assert set(counts) == {"QB", "RB", "WR", "TE", "K", "DEF"}


class TestSampleRoster:
    def test_respects_target_counts_when_pool_is_deep_enough(self):
        pool = _pool("QB", 10) + _pool("RB", 10) + _pool("WR", 10) + _pool("TE", 10) + _pool("K", 10) + _pool("DEF", 10)
        projections = {row["key"]: 10.0 for row in pool}
        target = _target_counts(roster_capacity(_STANDARD))
        roster = sample_roster(pool, projections, _STANDARD, random.Random(1))
        counts: dict[str, int] = {}
        for row in roster:
            counts[row["position"]] = counts.get(row["position"], 0) + 1
        assert counts == target

    def test_short_pool_takes_whatever_is_available_without_crashing(self):
        pool = _pool("QB", 1) + _pool("RB", 1) + _pool("WR", 1) + _pool("TE", 1) + _pool("K", 1) + _pool("DEF", 1)
        projections = {row["key"]: 5.0 for row in pool}
        roster = sample_roster(pool, projections, _STANDARD, random.Random(1))
        assert len(roster) == 6  # exactly one per position, capped by pool depth

    def test_missing_position_entirely_is_skipped_not_an_error(self):
        pool = _pool("QB", 5) + _pool("RB", 5) + _pool("WR", 5) + _pool("TE", 5) + _pool("K", 5)  # no DEF
        projections = {row["key"]: 5.0 for row in pool}
        roster = sample_roster(pool, projections, _STANDARD, random.Random(1))
        assert all(row["position"] != "DEF" for row in roster)

    def test_same_seed_reproduces_the_same_roster(self):
        pool = _pool("QB", 10) + _pool("RB", 10) + _pool("WR", 10) + _pool("TE", 10) + _pool("K", 10) + _pool("DEF", 10)
        projections = {row["key"]: 10.0 for row in pool}
        r1 = sample_roster(pool, projections, _STANDARD, random.Random(42))
        r2 = sample_roster(pool, projections, _STANDARD, random.Random(42))
        assert [row["key"] for row in r1] == [row["key"] for row in r2]

    def test_different_seeds_can_differ(self):
        pool = _pool("QB", 10) + _pool("RB", 10) + _pool("WR", 10) + _pool("TE", 10) + _pool("K", 10) + _pool("DEF", 10)
        projections = {row["key"]: 10.0 for row in pool}
        r1 = sample_roster(pool, projections, _STANDARD, random.Random(1))
        r2 = sample_roster(pool, projections, _STANDARD, random.Random(2))
        assert [row["key"] for row in r1] != [row["key"] for row in r2]

    def test_candidates_per_position_cutoff_is_respected(self):
        # 20 WRs available, but only the top 3 (by projection) may ever be
        # drawn -- give the bottom 17 a projection so low the roster would
        # never sample from them if the cutoff works.
        pool = _pool("WR", 20)
        projections = {row["key"]: (100.0 if i < 3 else 0.0) for i, row in enumerate(pool)}
        target = {"QB": 0, "RB": 0, "WR": 3, "TE": 0, "K": 0, "DEF": 0}
        # sample_roster always uses the module's own _target_counts, so
        # force a tiny capacity that maps to WR:2 minimum + rounding --
        # instead, just sample repeatedly and check every draw stays in the
        # top-3 pool across many seeds.
        for seed in range(20):
            roster = sample_roster(pool, projections, _STANDARD, random.Random(seed), candidates_per_position=3)
            wr_keys = {row["key"] for row in roster if row["position"] == "WR"}
            assert wr_keys <= {pool[0]["key"], pool[1]["key"], pool[2]["key"]}


class TestSampleRosters:
    def test_returns_requested_count(self):
        pool = _pool("QB", 5) + _pool("RB", 5) + _pool("WR", 5) + _pool("TE", 5) + _pool("K", 5) + _pool("DEF", 5)
        projections = {row["key"]: 5.0 for row in pool}
        rosters = sample_rosters(pool, projections, _STANDARD, n=4, seed=7)
        assert len(rosters) == 4

    def test_deterministic_given_seed(self):
        pool = _pool("QB", 5) + _pool("RB", 5) + _pool("WR", 5) + _pool("TE", 5) + _pool("K", 5) + _pool("DEF", 5)
        projections = {row["key"]: 5.0 for row in pool}
        r1 = sample_rosters(pool, projections, _STANDARD, n=3, seed=7)
        r2 = sample_rosters(pool, projections, _STANDARD, n=3, seed=7)
        assert [[row["key"] for row in roster] for roster in r1] == [[row["key"] for row in roster] for roster in r2]
