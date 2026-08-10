from __future__ import annotations

import json

from ffbot.config import Config
from ffbot.history.openmeteo import open_meteo_game_weather

_GAMES_CSV = (
    "season,week,home_team,away_team,gameday,gametime,roof\n"
    "2023,5,BUF,MIA,2023-10-01,13:00,outdoors\n"   # outdoor, has lat/lon in data/stadiums.yml
    "2023,5,DET,GB,2023-10-01,13:00,dome\n"          # dome -- must be skipped, no fetch
    "2023,6,BUF,NE,2023-10-08,13:00,outdoors\n"      # different week -- must be excluded
)

_DAY_JSON = json.dumps({
    "hourly": {
        "time": ["2023-10-01T12:00", "2023-10-01T13:00", "2023-10-01T14:00"],
        "temperature_2m": [55.0, 52.0, 50.0],
        "precipitation": [0.0, 1.2, 0.5],
        "wind_speed_10m": [8.0, 12.0, 14.0],
        "wind_gusts_10m": [15.0, 20.0, 22.0],
    }
}).encode("utf-8")


def _opener(calls: list):
    def opener(url: str) -> bytes:
        calls.append(url)
        if "schedules/games.csv" in url:
            return _GAMES_CSV.encode("utf-8")
        if "archive-api.open-meteo.com" in url:
            return _DAY_JSON
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


class TestOpenMeteoGameWeather:
    def test_outdoor_game_gets_both_teams_keyed_to_the_same_reading(self, tmp_path):
        calls: list = []
        out = open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=_opener(calls))
        assert out["BUF"] == out["MIA"]
        assert out["BUF"]["wind_mph"] == 12.0  # the 13:00 hour, not 12:00 or 14:00
        assert out["BUF"]["temp_f"] == 52.0
        assert out["BUF"]["wind_gust_mph"] == 20.0
        assert out["BUF"]["precip_mm"] == 1.2

    def test_dome_game_is_never_fetched(self, tmp_path):
        calls: list = []
        out = open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=_opener(calls))
        assert "DET" not in out and "GB" not in out
        assert not any("archive-api" in c and "44." in c for c in calls)  # DET's lat never queried

    def test_different_week_is_excluded(self, tmp_path):
        out = open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=_opener([]))
        assert "NE" not in out

    def test_same_stadium_date_shares_one_fetch(self, tmp_path):
        # Both BUF's week-5 home game rows in a hypothetical doubleheader
        # scenario would collapse to one HTTP call via the on-disk cache --
        # simulated here by calling twice against the same tmp_path and
        # counting calls the second time.
        calls_first: list = []
        open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=_opener(calls_first))
        n_first = sum(1 for c in calls_first if "archive-api" in c)

        calls_second: list = []
        open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=_opener(calls_second))
        n_second = sum(1 for c in calls_second if "archive-api" in c)
        assert n_first >= 1
        assert n_second == 0  # fully served from the on-disk cache the second time

    def test_fetch_failure_for_one_game_does_not_lose_the_others(self, tmp_path):
        def flaky_opener(url: str) -> bytes:
            if "schedules/games.csv" in url:
                return _GAMES_CSV.encode("utf-8")
            if "archive-api.open-meteo.com" in url:
                raise RuntimeError("simulated network failure")
            raise AssertionError(f"unexpected fetch: {url}")

        out = open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=flaky_opener)
        assert out == {}  # BUF/MIA's only source failed -- degrades to empty, not a crash

    def test_missing_lat_lon_stadium_is_skipped(self, tmp_path):
        games_csv = (
            "season,week,home_team,away_team,gameday,gametime,roof\n"
            "2023,5,ZZZ,MIA,2023-10-01,13:00,outdoors\n"  # ZZZ has no stadiums.yml row at all
        )

        def opener(url: str) -> bytes:
            if "schedules/games.csv" in url:
                return games_csv.encode("utf-8")
            raise AssertionError(f"unexpected fetch: {url}")  # must never reach open-meteo

        out = open_meteo_game_weather(2023, 5, Config(), cache_dir=tmp_path, opener=opener)
        assert out == {}
