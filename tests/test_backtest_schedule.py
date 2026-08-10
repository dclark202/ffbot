from __future__ import annotations

import pytest

from ffbot.backtest.schedule import ManagerRecord, round_robin_schedule, score_schedule


class TestRoundRobinSchedule:
    def test_rejects_too_few_teams(self):
        with pytest.raises(ValueError):
            round_robin_schedule(1, 5)

    def test_rejects_zero_weeks(self):
        with pytest.raises(ValueError):
            round_robin_schedule(4, 0)

    def test_even_teams_every_team_plays_every_week(self):
        sched = round_robin_schedule(6, 5)
        for week_pairs in sched:
            teams = {t for a, b in week_pairs for t in (a, b)}
            assert teams == set(range(1, 7))
            assert len(week_pairs) == 3  # 6 teams -> 3 games/week

    def test_odd_teams_one_bye_per_week(self):
        sched = round_robin_schedule(5, 4)
        for week_pairs in sched:
            teams = {t for a, b in week_pairs for t in (a, b)}
            assert len(teams) == 4  # one of the 5 teams sits out
            assert len(week_pairs) == 2

    def test_no_team_plays_itself(self):
        sched = round_robin_schedule(8, 7)
        for week_pairs in sched:
            for a, b in week_pairs:
                assert a != b

    def test_cycle_repeats_past_natural_length(self):
        # 4 teams -> a 3-week cycle; week 4 must repeat week 1's pairings.
        sched = round_robin_schedule(4, 6)
        assert sorted(sched[0]) == sorted(sched[3])
        assert sorted(sched[1]) == sorted(sched[4])

    def test_every_pair_meets_within_one_cycle_for_even_teams(self):
        sched = round_robin_schedule(4, 3)
        seen = {frozenset(pair) for week in sched for pair in week}
        all_pairs = {frozenset((a, b)) for a in range(1, 5) for b in range(1, 5) if a != b}
        assert seen == all_pairs


class TestManagerRecord:
    def test_win_rate_normal_case(self):
        r = ManagerRecord(wins=8, losses=4, ties=0)
        assert r.win_rate == pytest.approx(8 / 12)

    def test_ties_count_as_half_a_win(self):
        r = ManagerRecord(wins=1, losses=1, ties=2)
        assert r.win_rate == pytest.approx((1 + 0.5 * 2) / 4)

    def test_zero_games_is_zero_not_a_crash(self):
        r = ManagerRecord()
        assert r.games == 0
        assert r.win_rate == 0.0


class TestScoreSchedule:
    def test_higher_score_wins(self):
        sched = [[(1, 2)]]
        points = {(1, 1): 100.0, (2, 1): 80.0}
        records = score_schedule(sched, points)
        assert records[1] == ManagerRecord(wins=1, losses=0, ties=0)
        assert records[2] == ManagerRecord(wins=0, losses=1, ties=0)

    def test_exact_tie(self):
        sched = [[(1, 2)]]
        points = {(1, 1): 100.0, (2, 1): 100.0}
        records = score_schedule(sched, points)
        assert records[1] == ManagerRecord(wins=0, losses=0, ties=1)
        assert records[2] == ManagerRecord(wins=0, losses=0, ties=1)

    def test_missing_points_default_to_zero(self):
        sched = [[(1, 2)]]
        records = score_schedule(sched, {})  # nobody scored anything
        assert records[1].ties == 1  # 0.0 == 0.0

    def test_total_games_equals_twice_total_pairings(self):
        sched = round_robin_schedule(6, 5)
        total_pairings = sum(len(w) for w in sched)
        points = {(t, w): float(t + w) for w in range(1, 6) for t in range(1, 7)}
        records = score_schedule(sched, points)
        assert sum(r.games for r in records.values()) == 2 * total_pairings

    def test_every_scheduled_team_gets_a_record(self):
        sched = round_robin_schedule(5, 4)
        records = score_schedule(sched, {})
        assert set(records) == {1, 2, 3, 4, 5}
