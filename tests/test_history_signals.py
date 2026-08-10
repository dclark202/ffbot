from __future__ import annotations

import csv
import io

import pytest

from ffbot.config import Config, LeagueScoring, ReceivingScoring
from ffbot.history.signals import _percentile_rank_within_position, historical_form


class TestPercentileRankWithinPosition:
    def test_single_player_ranks_at_neutral_midpoint(self):
        out = _percentile_rank_within_position({"a:WR": 5.0}, {"a:WR": "WR"})
        assert out == {"a:WR": 50.0}

    def test_ranks_low_to_high_across_zero_to_hundred(self):
        raw = {"a:WR": 1.0, "b:WR": 2.0, "c:WR": 3.0}
        pos = {"a:WR": "WR", "b:WR": "WR", "c:WR": "WR"}
        out = _percentile_rank_within_position(raw, pos)
        assert out["a:WR"] == 0.0
        assert out["b:WR"] == 50.0
        assert out["c:WR"] == 100.0

    def test_positions_are_ranked_independently(self):
        raw = {"a:WR": 1.0, "b:WR": 2.0, "c:RB": 1.0, "d:RB": 2.0}
        pos = {"a:WR": "WR", "b:WR": "WR", "c:RB": "RB", "d:RB": "RB"}
        out = _percentile_rank_within_position(raw, pos)
        # WR's own low value ranks 0, RB's own low value ALSO ranks 0 --
        # they don't compete against each other's raw scale.
        assert out["a:WR"] == 0.0
        assert out["c:RB"] == 0.0


_PLAYER_FIELDS = ["player_display_name", "position", "team", "season", "week", "receptions", "receiving_yards", "receiving_tds", "fumbles_lost_total"]


def _row(name, week, recyd):
    return {
        "player_display_name": name, "position": "WR", "team": "MIA", "season": 2023, "week": week,
        "receptions": 5, "receiving_yards": recyd, "receiving_tds": 0, "fumbles_lost_total": 0,
    }


def _csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_PLAYER_FIELDS, restval="")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _opener(player_csv: bytes):
    def opener(url: str) -> bytes:
        if "stats_player_week" in url:
            return player_csv
        if "stats_team_week" in url:
            return b"team,season,week\n"
        if "schedules/games.csv" in url:
            return b"season,week,home_team,away_team,home_score,away_score\n"
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


def _cfg() -> Config:
    cfg = Config()
    cfg.league = LeagueScoring(receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0))
    return cfg


class TestHistoricalForm:
    def test_below_min_games_gets_no_entry(self, tmp_path):
        rows = [_row("Steady Guy", w, 50) for w in (1, 2)]  # only 2 games, min_games default 3
        cfg = _cfg()
        out = historical_form(2023, 3, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)), min_games=3)
        assert "steady guy" not in out

    def test_consistent_player_has_low_volatility(self, tmp_path):
        # Same output every week -- coefficient of variation is exactly 0.
        steady = [_row("Steady Guy", w, 50) for w in (1, 2, 3, 4)]
        # A second player with wildly different weeks, to give the
        # percentile ranking something to rank against.
        boom_bust = [
            _row("Boom Bust Guy", 1, 5), _row("Boom Bust Guy", 2, 200),
            _row("Boom Bust Guy", 3, 5), _row("Boom Bust Guy", 4, 200),
        ]
        cfg = _cfg()
        out = historical_form(2023, 5, cfg, cache_dir=tmp_path, opener=_opener(_csv(steady + boom_bust)), min_games=3)
        assert out["steady guy"]["volatility"] < out["boom bust guy"]["volatility"]

    def test_leakage_week_5_data_never_affects_a_week_3_call(self, tmp_path):
        rows = [_row("Real Wideout", w, 50) for w in (1, 2)] + [_row("Real Wideout", w, 500) for w in (3, 4, 5)]
        cfg = _cfg()
        # Projecting week 3 should only ever see weeks 1-2 -- with min_games=3
        # that's not even enough history to produce an entry at all. If week
        # 5's huge outlier leaked in, this player would show up with a
        # wildly different (and non-neutral) volatility/upside reading.
        out = historical_form(2023, 3, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)), min_games=3)
        assert "real wideout" not in out

    def test_output_keyed_by_bare_normalized_name_not_actuals_key(self, tmp_path):
        rows = [_row("Real Wideout", w, 50 + w) for w in (1, 2, 3, 4)]
        cfg = _cfg()
        out = historical_form(2023, 5, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)), min_games=3)
        assert "real wideout" in out
        assert "real wideout:WR" not in out

    def test_zero_variance_and_zero_median_do_not_crash(self, tmp_path):
        # A player who scored exactly 0 every game -- both mean and median
        # are 0, which would divide-by-zero a naive cv/upside formula.
        rows = [_row("Scored Nothing", w, -50) for w in (1, 2, 3, 4)]  # -50 yards -> 0-ish/negative points
        cfg = _cfg()
        out = historical_form(2023, 5, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)), min_games=3)
        assert "scored nothing" in out
        assert 0.0 <= out["scored nothing"]["volatility"] <= 100.0
