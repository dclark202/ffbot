from __future__ import annotations

from datetime import date, datetime

import pytest

from ffbot.history.fetch import FetchError
from ffbot.live.schedule import LiveGame, ScheduleError, current_week, this_week_games

_HEADER = "game_id,season,week,gameday,weekday,gametime,away_team,home_team,roof\n"


def _csv(rows: list[str]) -> bytes:
    return (_HEADER + "\n".join(rows)).encode("utf-8")


def _opener_for(rows: list[str], calls: list | None = None):
    def opener(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        return _csv(rows)
    return opener


class TestThisWeekGames:
    def test_filters_to_season_and_week(self, tmp_path):
        rows = [
            "2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors",
            "2026_02_KC_BUF,2026,2,2026-09-15,Monday,20:15,KC,BUF,outdoors",
            "2025_01_NE_SEA,2025,1,2025-09-09,Tuesday,20:20,NE,SEA,outdoors",
        ]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert set(games.keys()) == {"NE", "SEA"}

    def test_both_teams_keyed_with_correct_home_away(self, tmp_path):
        rows = ["2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games["NE"].home is False
        assert games["NE"].opponent == "SEA"
        assert games["SEA"].home is True
        assert games["SEA"].opponent == "NE"

    def test_kickoff_parsed_from_gameday_and_gametime(self, tmp_path):
        rows = ["2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games["NE"].kickoff == datetime(2026, 9, 9, 20, 20)

    def test_malformed_kickoff_degrades_to_none(self, tmp_path):
        rows = ["2026_01_NE_SEA,2026,1,garbage,Wednesday,,NE,SEA,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games["NE"].kickoff is None

    def test_dome_roof_detected(self, tmp_path):
        rows = ["2026_01_DET_GB,2026,1,2026-09-09,Wednesday,20:20,GB,DET,dome"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games["DET"].is_dome is True
        assert games["GB"].is_dome is True

    def test_outdoor_roof_not_dome(self, tmp_path):
        rows = ["2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games["NE"].is_dome is False

    def test_missing_team_row_skipped(self, tmp_path):
        rows = ["2026_01_X,2026,1,2026-09-09,Wednesday,20:20,,SEA,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games == {}

    def test_no_games_this_week_returns_empty_dict(self, tmp_path):
        rows = ["2026_02_KC_BUF,2026,2,2026-09-15,Monday,20:15,KC,BUF,outdoors"]
        games = this_week_games(2026, 1, cache_dir=tmp_path, opener=_opener_for(rows))
        assert games == {}

    def test_always_refetches_even_with_a_cache_hit(self, tmp_path):
        # A live in-progress week's schedule can change (flex scheduling,
        # postponements) -- unlike a completed season, a cache hit must
        # never be trusted here.
        calls: list = []
        rows = ["2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors"]
        opener = _opener_for(rows, calls)
        this_week_games(2026, 1, cache_dir=tmp_path, opener=opener)
        this_week_games(2026, 1, cache_dir=tmp_path, opener=opener)
        assert len(calls) == 2

    def test_transport_failure_raises_schedule_error(self, tmp_path):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        with pytest.raises(ScheduleError):
            this_week_games(2026, 1, cache_dir=tmp_path, opener=failing_opener)


_MULTI_WEEK_ROWS = [
    "2026_01_A_B,2026,1,2026-09-08,Tuesday,20:00,A,B,outdoors",
    "2026_02_A_B,2026,2,2026-09-15,Tuesday,20:00,A,B,outdoors",
    "2026_03_A_B,2026,3,2026-09-22,Tuesday,20:00,A,B,outdoors",
]


class TestCurrentWeek:
    def test_before_season_starts_returns_week_one(self, tmp_path):
        wk = current_week(2026, today=date(2026, 8, 1), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 1

    def test_day_before_week_twos_game_still_returns_week_two(self, tmp_path):
        wk = current_week(2026, today=date(2026, 9, 14), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 2

    def test_day_after_week_twos_only_game_rolls_over_to_week_three(self, tmp_path):
        # Once week 2's one game has been played, week 2 is "over" -- the
        # smallest week whose games haven't all finished is week 3.
        wk = current_week(2026, today=date(2026, 9, 16), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 3

    def test_exactly_on_a_game_date_counts_as_that_week(self, tmp_path):
        wk = current_week(2026, today=date(2026, 9, 15), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 2

    def test_after_season_ends_returns_final_week(self, tmp_path):
        wk = current_week(2026, today=date(2026, 12, 1), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 3

    def test_no_rows_for_season_returns_week_one(self, tmp_path):
        wk = current_week(2099, today=date(2026, 9, 16), cache_dir=tmp_path, opener=_opener_for(_MULTI_WEEK_ROWS))
        assert wk == 1

    def test_transport_failure_raises_schedule_error(self, tmp_path):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        with pytest.raises(ScheduleError):
            current_week(2026, cache_dir=tmp_path, opener=failing_opener)
