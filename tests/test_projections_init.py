from __future__ import annotations

import datetime as dt

import pytest

from ffbot.config import LeagueScoring, PassingScoring, ProjectionSourceConfig
from ffbot.projections import current_nfl_season, resolve_provider, ros_rows, weekly_rows
from ffbot.scoring import StatLine


def _row(name, team, position, points, stats=None, bye=None):
    return {"name": name, "team": team, "position": position, "points": points, "bye": bye, "stats": stats}


class TestCurrentNflSeason:
    def test_september_belongs_to_that_calendar_year(self):
        assert current_nfl_season(dt.date(2026, 9, 15)) == 2026

    def test_december_belongs_to_that_calendar_year(self):
        assert current_nfl_season(dt.date(2026, 12, 25)) == 2026

    def test_january_belongs_to_the_previous_calendar_year(self):
        # A January 2027 date is still "season 2026" -- the Super Bowl for
        # the season that started in Sept 2026 hasn't happened yet.
        assert current_nfl_season(dt.date(2027, 1, 20)) == 2026

    def test_february_belongs_to_the_previous_calendar_year(self):
        assert current_nfl_season(dt.date(2027, 2, 5)) == 2026

    def test_march_flips_back_to_the_calendar_year(self):
        # By March the previous season is fully over (Super Bowl is
        # February) -- no season is "current" yet, but this is a fallback
        # default, not a claim that a season is actively being played.
        assert current_nfl_season(dt.date(2027, 3, 1)) == 2027

    def test_default_uses_todays_real_date(self):
        # No date supplied -- must not raise, and must return an int.
        assert isinstance(current_nfl_season(), int)


class TestResolveProvider:
    def test_board_source_needs_no_provider(self):
        assert resolve_provider(ProjectionSourceConfig(source="board")) is None

    def test_csv_source_needs_no_provider(self):
        assert resolve_provider(ProjectionSourceConfig(source="csv")) is None

    def test_sleeper_source_returns_a_callable_provider(self):
        provider = resolve_provider(ProjectionSourceConfig(source="sleeper"))
        assert callable(provider)

    def test_sleeper_provider_uses_the_injected_opener_and_cache_dir(self, tmp_path):
        import json

        calls = []

        def fake_opener(url: str) -> bytes:
            calls.append(url)
            return json.dumps([]).encode("utf-8")

        provider = resolve_provider(
            ProjectionSourceConfig(source="sleeper"), cache_dir=tmp_path, opener=fake_opener,
        )
        rows = provider(2026, 1)
        assert rows == []
        assert len(calls) == 1
        assert (tmp_path / "sleeper_2026_wk01.json").exists()

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError):
            resolve_provider(ProjectionSourceConfig(source="not_a_real_source"))


class TestWeeklyRows:
    def test_delegates_straight_to_the_provider(self):
        calls = []

        def provider(season, week):
            calls.append((season, week))
            return [_row("Josh Allen", "BUF", "QB", 20.0)]

        rows = weekly_rows(2026, 3, provider)
        assert calls == [(2026, 3)]
        assert rows[0]["name"] == "Josh Allen"


class TestRosRows:
    def test_sums_points_across_the_requested_weeks(self):
        def provider(season, week):
            return [_row("Josh Allen", "BUF", "QB", 20.0 + week, stats=StatLine())]

        totals = ros_rows(2026, 1, 3, provider, league=None)
        assert len(totals) == 1
        # weeks 1,2,3 -> (20+1)+(20+2)+(20+3) = 66
        assert totals[0]["points"] == 66.0

    def test_never_reads_a_week_before_from_week(self):
        seen_weeks = []

        def provider(season, week):
            seen_weeks.append(week)
            return [_row("Josh Allen", "BUF", "QB", 10.0, stats=StatLine())]

        ros_rows(2026, 5, 7, provider, league=None)
        assert seen_weeks == [5, 6, 7]

    def test_rescoring_under_a_real_league_beats_the_raw_points_fallback(self):
        # A league that scores passing yards far more richly than the
        # "points" fallback -- confirms ros_rows re-derives from stats
        # under `league`, not just summing the raw consensus number.
        league = LeagueScoring(passing=PassingScoring(yards_per_point=1.0, td=0.0, int=0.0, two_pt=0.0))

        def provider(season, week):
            return [_row("Josh Allen", "BUF", "QB", 5.0, stats=StatLine(pass_yds=100.0))]

        totals = ros_rows(2026, 1, 2, provider, league=league)
        # 100 pass_yds / 1.0 yards_per_point = 100 pts/week * 2 weeks = 200,
        # nowhere near 2 * 5.0 = 10 the raw "points" fallback would give.
        assert totals[0]["points"] == 200.0

    def test_player_missing_from_a_later_week_keeps_only_earlier_weeks(self):
        def provider(season, week):
            if week == 2:
                return []  # e.g. player traded/benched/inactive
            return [_row("Josh Allen", "BUF", "QB", 10.0, stats=StatLine())]

        totals = ros_rows(2026, 1, 3, provider, league=None)
        assert totals[0]["points"] == 20.0  # weeks 1 and 3 only

    def test_team_and_bye_take_the_most_recent_week_seen(self):
        def provider(season, week):
            team = "BUF" if week == 1 else "KC"  # simulated mid-stream trade
            return [_row("Josh Allen", team, "QB", 10.0, stats=StatLine(), bye=week)]

        totals = ros_rows(2026, 1, 2, provider, league=None)
        assert totals[0]["team"] == "KC"
        assert totals[0]["bye"] == 2

    def test_different_positions_for_the_same_name_stay_separate(self):
        def provider(season, week):
            return [
                _row("Someone", "BUF", "QB", 10.0, stats=StatLine()),
                _row("Someone", "BUF", "WR", 5.0, stats=StatLine()),
            ]

        totals = ros_rows(2026, 1, 1, provider, league=None)
        assert len(totals) == 2

    def test_points_fp_sums_the_pre_league_scoring_consensus_across_weeks(self):
        # Regression: without this, an overlaid ROS row's points_fp silently
        # collapsed to equal its own points (board._apply_points_overlay's
        # no-provenance fallback), making edge.scoring_edge structurally
        # zero for the whole live ROS board.
        league = LeagueScoring(passing=PassingScoring(yards_per_point=1.0, td=0.0, int=0.0, two_pt=0.0))

        def provider(season, week):
            return [_row("Josh Allen", "BUF", "QB", 5.0, stats=StatLine(pass_yds=100.0))]

        totals = ros_rows(2026, 1, 2, provider, league=league)
        assert totals[0]["points"] == 200.0  # league-scored, as before
        assert totals[0]["points_fp"] == 10.0  # 5.0 consensus/week * 2 weeks
        assert totals[0]["points_source"] == "league"
        assert totals[0]["points_flags"] == ()

    def test_points_source_is_consensus_when_no_league_configured(self):
        def provider(season, week):
            return [_row("Josh Allen", "BUF", "QB", 5.0, stats=StatLine())]

        totals = ros_rows(2026, 1, 1, provider, league=None)
        assert totals[0]["points_source"] == "consensus"
        assert totals[0]["points_fp"] == 5.0
