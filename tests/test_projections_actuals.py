"""Season points-to-date: the one live seam that reads REALIZED results.

Two things need proving here, and the second matters more than the first.

1. The ordinary live-seam contract every source in this repo follows: an
   injectable opener, no network in tests, and a fetch failure that degrades
   with a surfaced alert rather than crashing.
2. That this can never be reached from the backtest. `history.index.as_of()`'s
   whole guarantee is that it never fetches a results-bearing source, and
   this fetch is results-bearing by definition -- so the guarantee only holds
   if nothing under `ffbot/history/` or `ffbot/backtest/` can call it. Proven
   structurally, in `tests/test_history_index.py::TestAsOfLeakageGuarantee`'s
   style, rather than by inspection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffbot import projections
from ffbot.projections import sleeper as sleeper_provider
from ffbot.projections.cache import ProjectionFetchError


def _entry(name: str, position: str, points: float, team: str = "KC") -> dict:
    first, _, last = name.partition(" ")
    return {
        "player": {"first_name": first, "last_name": last, "position": position, "team": team},
        "stats": {"pts_ppr": points},
    }


def _opener_for(weeks: dict[int, list[dict]], seen: list[str] | None = None):
    def opener(url: str) -> bytes:
        if seen is not None:
            seen.append(url)
        week = int(url.rsplit("?", 1)[0].rsplit("/", 1)[1])
        return json.dumps(weeks.get(week, [])).encode()

    return opener


class TestFetchActualWeeklyRows:
    def test_parses_rows_without_a_company_field(self, tmp_path):
        """Actuals carry no provider. `_row_from_entry`'s `company` filter
        would drop every single row, which is exactly why this path has its
        own parser rather than reusing that one."""
        opener = _opener_for({1: [_entry("Josh Allen", "QB", 24.5)]})
        rows = sleeper_provider.fetch_actual_weekly_rows(2025, 1, cache_dir=tmp_path, opener=opener)
        assert [r["name"] for r in rows] == ["Josh Allen"]
        assert rows[0]["points"] == 24.5
        assert rows[0]["stats"] is not None  # a real StatLine, so league scoring can recompute

    def test_hits_the_undocumented_stats_endpoint(self, tmp_path):
        seen: list[str] = []
        sleeper_provider.fetch_actual_weekly_rows(
            2025, 4, cache_dir=tmp_path, opener=_opener_for({4: []}, seen),
        )
        assert seen[0].startswith("https://api.sleeper.com/stats/nfl/2025/4?season_type=regular")

    def test_a_player_who_did_not_play_is_dropped_not_zeroed(self, tmp_path):
        """Distinct from playing and scoring 0.0, which comes back as 0.0.
        Dropping the row is what keeps a games-played count honest."""
        rows = sleeper_provider.fetch_actual_weekly_rows(
            2025, 1, cache_dir=tmp_path,
            opener=_opener_for({1: [
                {"player": {"first_name": "Ghost", "last_name": "Player", "position": "WR"}, "stats": {}},
                _entry("Real Player", "WR", 0.0),
            ]}),
        )
        assert [r["name"] for r in rows] == ["Real Player"]

    def test_network_failure_raises_for_the_caller_to_handle(self, tmp_path):
        def boom(url: str) -> bytes:
            raise OSError("network unreachable")

        with pytest.raises(ProjectionFetchError):
            sleeper_provider.fetch_actual_weekly_rows(2025, 1, cache_dir=tmp_path, opener=boom)


class TestSeasonToDateRows:
    def test_sums_scored_points_across_completed_weeks(self, tmp_path):
        opener = _opener_for({
            1: [_entry("Josh Allen", "QB", 20.0)],
            2: [_entry("Josh Allen", "QB", 30.0)],
        })
        points, games = projections.season_to_date_rows(
            2025, 2, None, cache_dir=tmp_path, opener=opener,
        )
        assert points == {"josh allen:QB": pytest.approx(50.0)}
        assert games == {"josh allen:QB": 2}

    def test_games_counts_only_weeks_actually_played(self, tmp_path):
        opener = _opener_for({
            1: [_entry("Josh Allen", "QB", 20.0), _entry("Brock Bowers", "TE", 10.0)],
            2: [_entry("Josh Allen", "QB", 30.0)],  # Bowers out
        })
        _, games = projections.season_to_date_rows(
            2025, 2, None, cache_dir=tmp_path, opener=opener,
        )
        assert games == {"josh allen:QB": 2, "brock bowers:TE": 1}

    def test_fetches_one_week_at_a_time(self, tmp_path):
        """Per-week rather than the season-cumulative endpoint, whose K/DEF
        fields are bucketed and cannot be rebuilt into a StatLine -- and so
        that each week is scored independently, the same discipline
        `ros_rows` documents for the projection side."""
        seen: list[str] = []
        projections.season_to_date_rows(
            2025, 3, None, cache_dir=tmp_path, opener=_opener_for({}, seen),
        )
        assert len(seen) == 3

    def test_keys_match_the_weekly_points_convention(self, tmp_path):
        """`"<normalized name>:<POSITION>"`, so a `MetricsIndex` lookup finds
        the same player `LoadedReport.weekly_points` does."""
        from ffbot.names import normalize_name

        points, _ = projections.season_to_date_rows(
            2025, 1, None, cache_dir=tmp_path,
            opener=_opener_for({1: [_entry("Ja'Marr Chase", "WR", 18.0)]}),
        )
        # Through `normalize_name`, punctuation and all -- an apostrophe in
        # the source name must not produce a key nothing can look up.
        assert list(points) == [f"{normalize_name('Ja\'Marr Chase')}:WR"]


class TestSeasonToDateNeverReachesTheBacktest:
    """A live actual-stats fetch is a results-bearing source by definition.

    `ffbot.history.index.as_of()` guarantees it never fetches one, and
    `ffbot/backtest/` may read outcomes only through
    `ffbot.history.actuals.week_actuals`. Both guarantees hold only if
    nothing in either package can call this seam -- checked mechanically,
    because "we just won't do that" is not a guarantee.
    """

    _FORBIDDEN = ("fetch_actual_weekly_rows", "season_to_date_rows", "season_stats_source")

    def _sources(self, package: str) -> list[Path]:
        return sorted(Path(package).rglob("*.py"))

    def test_history_package_never_references_the_live_actuals_seam(self):
        for path in self._sources("ffbot/history"):
            source = path.read_text(encoding="utf-8")
            for name in self._FORBIDDEN:
                assert name not in source, f"{path} must not reference {name}"

    def test_backtest_package_never_references_the_live_actuals_seam(self):
        for path in self._sources("ffbot/backtest"):
            source = path.read_text(encoding="utf-8")
            for name in self._FORBIDDEN:
                assert name not in source, f"{path} must not reference {name}"
