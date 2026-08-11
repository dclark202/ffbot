from __future__ import annotations

from ffbot.history.index import (
    _build_games_and_stadiums,
    _build_player_status,
    _games_this_week,
    _implied_totals,
    _roof_dome,
    as_of,
)

_GAMES_CSV = (
    "season,week,home_team,away_team,home_score,away_score,spread_line,total_line,wind,temp,roof\n"
    "2023,5,SF,DAL,28,10,-3.5,47.5,8,68,outdoors\n"      # week 5, outdoor
    "2023,5,DET,GB,20,17,1.0,44.0,,,dome\n"                # week 5, dome
    "2023,6,KC,DEN,30,14,-9.5,45.0,5,55,open\n"             # different week -- must be excluded
)

_INJURIES_CSV = (
    "season,week,team,full_name,report_status\n"
    "2023,5,SF,Real Player,Questionable\n"
    "2023,5,DAL,Bench Guy,Out\n"
    "2023,5,GB,Fine Guy,\n"                 # cleared -- no status override
    "2023,6,SF,Wrong Week Guy,Out\n"        # different week -- must be excluded
)


def _opener(calls: list):
    def opener(url: str) -> bytes:
        calls.append(url)
        if "schedules/games.csv" in url:
            return _GAMES_CSV.encode("utf-8")
        if "/injuries/injuries_" in url:
            return _INJURIES_CSV.encode("utf-8")
        # Anything else (stats_player_week, stats_team_week, the ECR
        # archive, ...) should never be requested by as_of() -- return
        # something so a leaked call doesn't itself crash the test, letting
        # the leakage assertion below report the real problem.
        return b"unexpected,call\n1,2\n"
    return opener


class TestImpliedTotals:
    def test_computes_home_and_away(self):
        home, away = _implied_totals(spread_line=-3.5, total_line=47.5)
        assert round(home, 2) == 22.0
        assert round(away, 2) == 25.5

    def test_missing_line_returns_none_pair(self):
        assert _implied_totals(None, 47.5) == (None, None)
        assert _implied_totals(-3.0, None) == (None, None)


class TestRoofDome:
    def test_dome_and_closed_are_dome(self):
        assert _roof_dome("dome") is True
        assert _roof_dome("closed") is True

    def test_outdoors_and_open_are_not_dome(self):
        assert _roof_dome("outdoors") is False
        assert _roof_dome("open") is False

    def test_unknown_roof_is_none(self):
        assert _roof_dome("") is None
        assert _roof_dome("weird-value") is None


class TestGamesThisWeek:
    def test_filters_by_season_and_week(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(_GAMES_CSV)))
        week5 = _games_this_week(rows, 2023, 5)
        assert len(week5) == 2
        assert all(r["week"] == "5" for r in week5)

    def test_no_match_returns_empty(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(_GAMES_CSV)))
        assert _games_this_week(rows, 2023, 99) == []


class TestBuildGamesAndStadiums:
    def test_outdoor_game_carries_wind_and_totals(self):
        import csv
        import io

        rows = _games_this_week(list(csv.DictReader(io.StringIO(_GAMES_CSV))), 2023, 5)
        games, stadiums = _build_games_and_stadiums(rows)

        assert games["SF"].opponent == "DAL"
        assert games["SF"].home is True
        assert games["SF"].wind_mph == 8.0
        assert games["SF"].precip_pct is None  # nflverse has no precip column
        assert games["DAL"].opponent == "SF"
        assert games["DAL"].home is False
        assert stadiums["SF"].dome is False

    def test_dome_game_has_no_wind_and_dome_true(self):
        import csv
        import io

        rows = _games_this_week(list(csv.DictReader(io.StringIO(_GAMES_CSV))), 2023, 5)
        _games, stadiums = _build_games_and_stadiums(rows)
        assert stadiums["DET"].dome is True


class TestBuildPlayerStatus:
    def test_maps_report_status_to_yahoo_codes(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(_INJURIES_CSV)))
        status = _build_player_status(rows, 2023, 5)
        assert status["real player"].status == "Q"
        assert status["bench guy"].status == "O"

    def test_cleared_report_produces_no_entry(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(_INJURIES_CSV)))
        status = _build_player_status(rows, 2023, 5)
        assert "fine guy" not in status

    def test_filters_to_requested_week(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(_INJURIES_CSV)))
        status = _build_player_status(rows, 2023, 5)
        assert "wrong week guy" not in status


class TestAsOfLeakageGuarantee:
    """The point-in-time boundary is enforced by construction: `as_of()`
    must never touch a source that carries actual results."""

    def test_as_of_never_requests_result_bearing_sources(self, tmp_path):
        calls: list[str] = []
        as_of(2023, 5, cache_dir=tmp_path, opener=_opener(calls))
        for url in calls:
            assert "stats_player" not in url
            assert "stats_team" not in url
            assert "dynastyprocess" not in url  # the FantasyPros ECR archive

    def test_as_of_only_requests_games_and_injuries(self, tmp_path):
        calls: list[str] = []
        as_of(2023, 5, cache_dir=tmp_path, opener=_opener(calls))
        assert any("schedules/games.csv" in u for u in calls)
        assert any("/injuries/injuries_2023.csv" in u for u in calls)
        assert len(calls) == 2

    def test_snapshot_content_end_to_end(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        assert snap.season == 2023
        assert snap.week == 5
        assert set(snap.games.keys()) == {"SF", "DAL", "DET", "GB"}
        assert snap.player_status["real player"].status == "Q"

        intel = snap.weekly_intel()
        assert intel.week == 5
        assert intel.games["SF"].opponent == "DAL"
        # Nothing in the decision view carries a score -- GameInfo simply
        # has no such field, so this is a structural guarantee, not a
        # runtime check of a value.
        assert not hasattr(intel.games["SF"], "home_score")
        assert not hasattr(intel.games["SF"], "away_score")

    def test_game_rows_is_the_separate_ground_truth_channel(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        # Scores DO live in game_rows (deliberately, for actuals.py) -- this
        # confirms the two channels are genuinely different, not that
        # game_rows is empty.
        sf_row = next(r for r in snap.game_rows if r["home_team"] == "SF")
        assert sf_row["home_score"] == "28"

    def test_missing_injuries_degrades_to_no_status_overrides(self, tmp_path):
        def opener(url: str) -> bytes:
            if "schedules/games.csv" in url:
                return _GAMES_CSV.encode("utf-8")
            raise RuntimeError("injuries unreachable")

        # season 2005 predates injuries' first_season (2009) -- source_url()
        # raises internally; as_of() must degrade gracefully rather than
        # propagate, same "missing file = no override" contract as
        # week.load_weekly_intel.
        snap = as_of(2005, 5, cache_dir=tmp_path, opener=opener)
        assert snap.player_status == {}


class TestWithSignals:
    def test_merges_all_five_signal_keys(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        merged = snap.with_signals({
            "someone new": {
                "volatility": 70.0, "upside": 80.0, "usage": 90.0,
                "momentum": 55.0, "divergence": 65.0,
            }
        })
        entry = merged.player_status["someone new"]
        assert (entry.volatility, entry.upside, entry.usage_trend, entry.momentum, entry.divergence) == (
            70.0, 80.0, 90.0, 55.0, 65.0,
        )

    def test_preserves_existing_status_while_adding_signals(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        assert snap.player_status["real player"].status == "Q"  # from the injuries fixture
        merged = snap.with_signals({"real player": {"usage": 60.0}})
        entry = merged.player_status["real player"]
        assert entry.status == "Q"
        assert entry.usage_trend == 60.0

    def test_unknown_signal_keys_are_ignored_not_raised(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        merged = snap.with_signals({"someone new": {"some_future_signal": 42.0}})
        entry = merged.player_status["someone new"]
        assert (entry.volatility, entry.upside, entry.usage_trend, entry.momentum, entry.divergence) == (
            None, None, None, None, None,
        )

    def test_two_calls_merge_rather_than_overwrite(self, tmp_path):
        # The combine_providers alternative to a single call -- calling
        # with_signals twice (once per provider) must compose, not clobber.
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        merged = snap.with_signals({"someone new": {"volatility": 70.0}}).with_signals(
            {"someone new": {"usage": 90.0}}
        ).with_signals({"someone new": {"momentum": 40.0, "divergence": 20.0}})
        entry = merged.player_status["someone new"]
        assert (entry.volatility, entry.usage_trend, entry.momentum, entry.divergence) == (70.0, 90.0, 40.0, 20.0)


class TestWithGameWeather:
    def test_enriches_an_existing_game_without_dropping_vegas_fields(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        assert "SF" in snap.games  # the outdoor game from _GAMES_CSV
        before = snap.games["SF"]
        merged = snap.with_game_weather({"SF": {"temp_f": 68.0, "wind_gust_mph": 22.0, "precip_mm": 3.0}})
        after = merged.games["SF"]
        assert (after.temp_f, after.wind_gust_mph, after.precip_mm) == (68.0, 22.0, 3.0)
        assert after.team_total == before.team_total  # untouched fields survive

    def test_team_absent_from_weather_dict_is_left_untouched(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        merged = snap.with_game_weather({})
        assert merged.games == snap.games

    def test_never_invents_a_game_not_already_present(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        merged = snap.with_game_weather({"ZZZ": {"temp_f": 40.0}})
        assert "ZZZ" not in merged.games

    def test_partial_fields_do_not_clobber_existing_wind(self, tmp_path):
        snap = as_of(2023, 5, cache_dir=tmp_path, opener=_opener([]))
        original_wind = snap.games["SF"].wind_mph  # 8.0, from _GAMES_CSV
        merged = snap.with_game_weather({"SF": {"temp_f": 68.0}})  # no wind_mph key at all
        assert merged.games["SF"].wind_mph == original_wind
