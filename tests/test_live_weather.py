from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ffbot.live.cache import LiveFetchError
from ffbot.live.schedule import LiveGame
from ffbot.live.weather import forecast_weather


def _forecast_payload(times, wind, precip_pct, gust, temp, precip_mm):
    return json.dumps({
        "hourly": {
            "time": times,
            "wind_speed_10m": wind,
            "precipitation_probability": precip_pct,
            "wind_gusts_10m": gust,
            "temperature_2m": temp,
            "precipitation": precip_mm,
        }
    }).encode("utf-8")


def _stadiums_yml(tmp_path: Path) -> Path:
    p = tmp_path / "stadiums.yml"
    p.write_text(
        "SEA: {dome: false, lat: 47.5952, lon: -122.3316}\n"
        "DET: {dome: true}\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def _chdir_with_stadiums(tmp_path, monkeypatch):
    _stadiums_yml(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "stadiums.yml").write_text(
        "SEA: {dome: false, lat: 47.5952, lon: -122.3316}\n"
        "DET: {dome: true}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)


def _games_one_outdoor_one_dome():
    kickoff = datetime(2026, 9, 9, 20, 20)
    return {
        "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
        "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        "DET": LiveGame(opponent="GB", home=True, roof="dome", kickoff=kickoff),
        "GB": LiveGame(opponent="DET", home=False, roof="dome", kickoff=kickoff),
    }


class TestNearestHour:
    def test_rounds_down_below_half_past(self):
        from ffbot.live.weather import _nearest_hour
        assert _nearest_hour(datetime(2026, 9, 9, 20, 20)) == datetime(2026, 9, 9, 20, 0)

    def test_rounds_up_at_or_past_half_past(self):
        from ffbot.live.weather import _nearest_hour
        assert _nearest_hour(datetime(2026, 9, 9, 20, 30)) == datetime(2026, 9, 9, 21, 0)

    def test_rounding_up_crosses_midnight_onto_the_next_date(self):
        from ffbot.live.weather import _nearest_hour
        assert _nearest_hour(datetime(2026, 9, 9, 23, 45)) == datetime(2026, 9, 10, 0, 0)


class TestForecastWeather:
    def test_dome_game_skipped_entirely(self, tmp_path):
        calls: list = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return _forecast_payload(["2026-09-09T20:00"], [10.0], [15], [12.0], [60.0], [0.0])

        out = forecast_weather(_games_one_outdoor_one_dome(), cache_dir=tmp_path, opener=opener)
        assert "DET" not in out and "GB" not in out
        # Only SEA's stadium was ever fetched -- the dome game never touched
        # the network at all, not just filtered from the output afterward.
        assert len(calls) == 1

    def test_outdoor_game_fetched_and_both_teams_get_the_same_reading(self, tmp_path):
        def opener(url: str) -> bytes:
            return _forecast_payload(
                ["2026-09-09T19:00", "2026-09-09T20:00", "2026-09-09T21:00"],
                [5.0, 12.0, 15.0], [10, 20, 30], [8.0, 18.0, 22.0], [60.0, 58.0, 55.0], [0.0, 0.1, 0.2],
            )

        out = forecast_weather(_games_one_outdoor_one_dome(), cache_dir=tmp_path, opener=opener)
        assert out["SEA"] == out["NE"]
        assert out["SEA"]["wind_mph"] == 12.0
        assert out["SEA"]["precip_pct"] == 20
        assert out["SEA"]["wind_gust_mph"] == 18.0
        assert out["SEA"]["temp_f"] == 58.0
        assert out["SEA"]["precip_mm"] == 0.1

    def test_missing_stadium_lat_lon_skips_that_game_only(self, tmp_path):
        kickoff = datetime(2026, 9, 9, 20, 20)
        games = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
            "UNKNOWN": LiveGame(opponent="XYZ", home=True, roof="outdoors", kickoff=kickoff),
            "XYZ": LiveGame(opponent="UNKNOWN", home=False, roof="outdoors", kickoff=kickoff),
        }

        def opener(url: str) -> bytes:
            return _forecast_payload(
                ["2026-09-09T20:00"], [10.0], [15], [12.0], [60.0], [0.0],
            )

        out = forecast_weather(games, cache_dir=tmp_path, opener=opener)
        assert "SEA" in out
        assert "UNKNOWN" not in out and "XYZ" not in out

    def test_no_kickoff_time_skips_that_game(self, tmp_path):
        games = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=None),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=None),
        }

        def opener(url: str) -> bytes:
            raise AssertionError("must not fetch with no kickoff time")

        out = forecast_weather(games, cache_dir=tmp_path, opener=opener)
        assert out == {}

    def test_fetch_failure_skips_that_game_only_never_raises(self, tmp_path):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        out = forecast_weather(_games_one_outdoor_one_dome(), cache_dir=tmp_path, opener=failing_opener)
        assert out == {}

    def test_hour_not_found_in_response_skips_that_game(self, tmp_path):
        def opener(url: str) -> bytes:
            return _forecast_payload(
                ["2026-09-09T01:00"], [10.0], [15], [12.0], [60.0], [0.0],
            )

        out = forecast_weather(_games_one_outdoor_one_dome(), cache_dir=tmp_path, opener=opener)
        assert out == {}

    def test_cache_hit_within_ttl_skips_refetch(self, tmp_path):
        calls: list = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return _forecast_payload(["2026-09-09T20:00"], [10.0], [15], [12.0], [60.0], [0.0])

        games = _games_one_outdoor_one_dome()
        forecast_weather(games, cache_dir=tmp_path, ttl_minutes=180.0, opener=opener, now=1000.0)
        forecast_weather(games, cache_dir=tmp_path, ttl_minutes=180.0, opener=opener, now=1000.0 + 60)
        assert len(calls) == 1

    def test_stale_cache_past_ttl_refetches(self, tmp_path):
        calls: list = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return _forecast_payload(["2026-09-09T20:00"], [10.0], [15], [12.0], [60.0], [0.0])

        games = _games_one_outdoor_one_dome()
        forecast_weather(games, cache_dir=tmp_path, ttl_minutes=180.0, opener=opener, now=1000.0)
        forecast_weather(games, cache_dir=tmp_path, ttl_minutes=180.0, opener=opener, now=1000.0 + 3600 * 4)
        assert len(calls) == 2

    def test_late_kickoff_rounding_past_midnight_fetches_the_next_calendar_date(self, tmp_path):
        # A near-midnight kickoff (23:45) rounds up to 00:00 the NEXT day --
        # the day-level forecast fetch must follow that rounded date, not
        # the kickoff's own, or the lookup would search the wrong day's
        # hourly series entirely.
        kickoff = datetime(2026, 9, 9, 23, 45)
        games = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        }
        requested_dates: list = []

        def opener(url: str) -> bytes:
            requested_dates.append(url)
            return _forecast_payload(["2026-09-10T00:00"], [10.0], [15], [12.0], [60.0], [0.0])

        out = forecast_weather(games, cache_dir=tmp_path, opener=opener)
        assert "start_date=2026-09-10" in requested_dates[0]
        assert out["SEA"]["wind_mph"] == 10.0
