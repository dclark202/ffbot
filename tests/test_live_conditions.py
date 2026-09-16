from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from ffbot.config import GameConditionsConfig
from ffbot.live import conditions
from ffbot.live.schedule import LiveGame, ScheduleError
from ffbot.week import GameInfo, WeeklyIntel


def _off_cfg() -> GameConditionsConfig:
    return GameConditionsConfig(weather_source="off", odds_source="off")


_GAMES_CSV_HEADER = "game_id,season,week,gameday,weekday,gametime,away_team,home_team,roof\n"


class TestFetchConditionsOpenerThreading:
    """Proves opener/kalshi_opener actually reach the underlying fetches --
    not just that fetch_conditions calls SOMETHING, which the monkeypatched
    tests in TestFetchConditions below already cover structurally."""

    def test_schedule_opener_is_the_one_actually_used(self, tmp_path):
        calls: list = []

        def opener(url: str) -> bytes:
            calls.append(url)
            assert "games.csv" in url  # the real nflverse schedule release URL
            return _GAMES_CSV_HEADER.encode("utf-8")  # no rows -- no games this week

        cfg = GameConditionsConfig(weather_source="off", odds_source="off")
        games, alerts = conditions.fetch_conditions(2026, 1, cfg, cache_dir=tmp_path, opener=opener)
        assert games == {}
        assert alerts == []
        assert len(calls) == 1  # the injected opener was genuinely invoked, not the real network

    def test_weather_opener_reaches_the_forecast_fetch(self, tmp_path):
        import json

        stadiums_dir = tmp_path / "data"
        stadiums_dir.mkdir()
        (stadiums_dir / "stadiums.yml").write_text(
            "SEA: {dome: false, lat: 47.5952, lon: -122.3316}\nNE: {dome: false}\n", encoding="utf-8",
        )

        calls: list = []

        def opener(url: str) -> bytes:
            calls.append(url)
            if "games.csv" in url:
                return (
                    _GAMES_CSV_HEADER + "2026_01_NE_SEA,2026,1,2026-09-09,Wednesday,20:20,NE,SEA,outdoors\n"
                ).encode("utf-8")
            if "open-meteo.com" in url:
                return json.dumps({
                    "hourly": {"time": ["2026-09-09T20:00"], "wind_speed_10m": [9.0], "precipitation_probability": [5]}
                }).encode("utf-8")
            raise AssertionError(f"unexpected URL: {url}")

        cfg = GameConditionsConfig(weather_source="open_meteo", odds_source="off", cache_ttl_minutes=180)
        import os
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            games, alerts = conditions.fetch_conditions(2026, 1, cfg, cache_dir=tmp_path, opener=opener)
        finally:
            os.chdir(old_cwd)
        assert alerts == []
        assert games["SEA"].wind_mph == 9.0
        assert any("open-meteo.com" in c for c in calls)  # the injected opener genuinely served the forecast


class TestFetchConditions:
    def test_schedule_failure_degrades_to_empty_with_alert(self, monkeypatch):
        def raising(*a, **k):
            raise ScheduleError("simulated network failure")

        monkeypatch.setattr(conditions.schedule_module, "this_week_games", raising)
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="open_meteo"))
        assert games == {}
        assert len(alerts) == 1
        assert "schedule" in alerts[0]

    def test_no_games_this_week_returns_empty_no_alert(self, monkeypatch):
        monkeypatch.setattr(conditions.schedule_module, "this_week_games", lambda *a, **k: {})
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="open_meteo"))
        assert games == {}
        assert alerts == []

    def test_weather_off_skips_weather_fetch_entirely(self, monkeypatch):
        kickoff = datetime(2026, 9, 9, 20, 20)
        one_game = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        }
        monkeypatch.setattr(conditions.schedule_module, "this_week_games", lambda *a, **k: one_game)

        def exploding_weather(*a, **k):
            raise AssertionError("must not fetch weather when weather_source is off")

        monkeypatch.setattr(conditions.weather_module, "forecast_weather", exploding_weather)
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="off", odds_source="off"))
        assert games["SEA"].wind_mph is None
        assert alerts == []

    def test_weather_populates_gameinfo_fields(self, monkeypatch):
        kickoff = datetime(2026, 9, 9, 20, 20)
        one_game = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        }
        monkeypatch.setattr(conditions.schedule_module, "this_week_games", lambda *a, **k: one_game)
        monkeypatch.setattr(
            conditions.weather_module, "forecast_weather",
            lambda *a, **k: {"SEA": {"wind_mph": 12.0, "precip_pct": 20.0}, "NE": {"wind_mph": 12.0, "precip_pct": 20.0}},
        )
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="open_meteo", odds_source="off"))
        assert games["SEA"].wind_mph == 12.0
        assert games["SEA"].precip_pct == 20.0
        assert games["SEA"].kickoff_et == "2026-09-09T20:20"
        assert games["SEA"].home is True
        assert alerts == []

    def test_weather_fetch_failure_degrades_with_alert_odds_unaffected(self, monkeypatch):
        kickoff = datetime(2026, 9, 9, 20, 20)
        one_game = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        }
        monkeypatch.setattr(conditions.schedule_module, "this_week_games", lambda *a, **k: one_game)

        def raising_weather(*a, **k):
            raise RuntimeError("simulated weather failure")

        monkeypatch.setattr(conditions.weather_module, "forecast_weather", raising_weather)

        import ffbot.markets.kalshi_nfl as kalshi_nfl_module
        monkeypatch.setattr(
            kalshi_nfl_module, "game_odds",
            lambda *a, **k: {"SEA": {"team_total": 24.0, "opp_total": 20.0}},
        )
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="open_meteo", odds_source="kalshi"))
        assert games["SEA"].wind_mph is None
        assert games["SEA"].team_total == 24.0  # odds still populated despite weather failing
        assert len(alerts) == 1
        assert "weather" in alerts[0]

    def test_odds_off_skips_odds_fetch_entirely(self, monkeypatch):
        kickoff = datetime(2026, 9, 9, 20, 20)
        one_game = {
            "SEA": LiveGame(opponent="NE", home=True, roof="outdoors", kickoff=kickoff),
            "NE": LiveGame(opponent="SEA", home=False, roof="outdoors", kickoff=kickoff),
        }
        monkeypatch.setattr(conditions.schedule_module, "this_week_games", lambda *a, **k: one_game)
        games, alerts = conditions.fetch_conditions(2026, 1, GameConditionsConfig(weather_source="off", odds_source="off"))
        assert games["SEA"].team_total is None
        assert alerts == []


class TestMergeConditions:
    def test_auto_fetched_used_when_no_hand_typed_entry(self):
        intel = WeeklyIntel()
        auto = {"SEA": GameInfo(opponent="NE", wind_mph=12.0)}
        merged, alerts = conditions.merge_conditions(intel, auto)
        assert merged.games["SEA"].wind_mph == 12.0
        assert alerts == []

    def test_forecast_wins_over_researched_weather(self):
        intel = WeeklyIntel(games={"SEA": GameInfo(opponent="NE", wind_mph=5.0, precip_pct=10.0)})
        auto = {"SEA": GameInfo(opponent="NE", wind_mph=8.0, precip_pct=20.0, wind_gust_mph=14.0)}
        merged, _ = conditions.merge_conditions(intel, auto)
        assert merged.games["SEA"].wind_mph == 8.0
        assert merged.games["SEA"].precip_pct == 20.0
        assert merged.games["SEA"].wind_gust_mph == 14.0

    def test_research_wins_for_non_weather_fields(self):
        intel = WeeklyIntel(games={"SEA": GameInfo(opponent="NE", kickoff_et="2026-09-13T16:25", team_total=24.0, opp_total=20.0)})
        auto = {"SEA": GameInfo(opponent="NE", kickoff_et="2026-09-13T16:05", team_total=21.0, opp_total=23.0)}
        merged, _ = conditions.merge_conditions(intel, auto)
        g = merged.games["SEA"]
        assert (g.kickoff_et, g.team_total, g.opp_total) == ("2026-09-13T16:25", 24.0, 20.0)

    def test_research_fills_weather_when_forecast_missing(self):
        intel = WeeklyIntel(games={"SEA": GameInfo(opponent="NE", wind_mph=18.0)})
        auto = {"SEA": GameInfo(opponent="NE", team_total=21.0)}  # weather fetch failed
        merged, alerts = conditions.merge_conditions(intel, auto)
        assert merged.games["SEA"].wind_mph == 18.0
        assert alerts == []

    def test_auto_totals_fill_when_research_omits_them(self):
        intel = WeeklyIntel(games={"SEA": GameInfo(opponent="NE", kickoff_et="2026-09-13T16:25")})
        auto = {"SEA": GameInfo(opponent="NE", team_total=21.0, opp_total=23.0)}
        merged, _ = conditions.merge_conditions(intel, auto)
        assert (merged.games["SEA"].team_total, merged.games["SEA"].opp_total) == (21.0, 23.0)

    def test_disagreement_alert_above_threshold_only(self):
        intel = WeeklyIntel(games={
            "SEA": GameInfo(opponent="NE", wind_mph=20.0, precip_pct=50.0),
            "NE": GameInfo(opponent="SEA", wind_mph=16.0, precip_pct=30.0),
        })
        auto = {
            "SEA": GameInfo(opponent="NE", wind_mph=10.0, precip_pct=10.0),
            "NE": GameInfo(opponent="SEA", wind_mph=10.1, precip_pct=0.0),
        }
        _, alerts = conditions.merge_conditions(intel, auto)
        assert any(a.startswith("SEA:") and "wind 20 mph" in a for a in alerts)
        assert any(a.startswith("SEA:") and "50% rain" in a for a in alerts)
        assert not any(a.startswith("NE:") for a in alerts)

    def test_jax_cle_2026_week_1_regression(self):
        """Research wrote a storm-gust note as 40 mph sustained / 40% rain; the
        forecast had ~6 mph and none. The forecast must win."""
        hand = GameInfo(opponent="CLE", kickoff_et="2026-09-13T13:00", home=True,
                        wind_mph=40.0, precip_pct=40.0, team_total=24.0, opp_total=15.5)
        intel = WeeklyIntel(games={"JAX": hand, "CLE": replace(hand, opponent="JAX", home=False, team_total=15.5, opp_total=24.0)})
        fetched = GameInfo(opponent="CLE", home=True, wind_mph=6.0, precip_pct=0.0)
        auto = {"JAX": fetched, "CLE": replace(fetched, opponent="JAX", home=False)}
        merged, alerts = conditions.merge_conditions(intel, auto)
        for team in ("JAX", "CLE"):
            assert merged.games[team].wind_mph == 6.0
            assert merged.games[team].precip_pct == 0.0
        assert merged.games["JAX"].team_total == 24.0
        assert merged.games["CLE"].home is False
        assert len([a for a in alerts if "wind 40 mph, forecast 6 mph" in a]) == 2

    def test_team_with_no_auto_data_and_no_hand_entry_absent(self):
        intel = WeeklyIntel()
        merged, _ = conditions.merge_conditions(intel, {})
        assert merged.games == {}

    def test_does_not_mutate_the_original_intel(self):
        intel = WeeklyIntel(games={"SEA": GameInfo(opponent="NE", wind_mph=5.0)})
        auto = {"NE": GameInfo(opponent="SEA", wind_mph=12.0), "SEA": GameInfo(opponent="NE", wind_mph=30.0)}
        merged, _ = conditions.merge_conditions(intel, auto)
        assert "NE" not in intel.games
        assert intel.games["SEA"].wind_mph == 5.0
        assert "NE" in merged.games
