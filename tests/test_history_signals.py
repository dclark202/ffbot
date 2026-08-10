from __future__ import annotations

import csv
import io

import pytest

from ffbot.config import Config, LeagueScoring, ReceivingScoring
from ffbot.history.signals import (
    _percentile_rank_within_position,
    combine_providers,
    historical_form,
    usage_form,
)


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


_USAGE_FIELDS = ["player_display_name", "position", "team", "season", "week", "wopr"]


def _usage_row(name, week, wopr, position="WR"):
    return {"player_display_name": name, "position": position, "team": "MIA", "season": 2023, "week": week, "wopr": wopr}


def _usage_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_USAGE_FIELDS, restval="")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _usage_opener(player_csv: bytes):
    def opener(url: str) -> bytes:
        if "stats_player_week" in url:
            return player_csv
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


class TestUsageForm:
    def test_below_min_games_gets_no_entry(self, tmp_path):
        rows = [_usage_row("Steady Guy", w, 0.5) for w in (1, 2)]
        out = usage_form(2023, 3, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rows)), min_games=3)
        assert "steady guy" not in out

    def test_trending_up_ranks_above_trending_down(self, tmp_path):
        # Rising role: recent games run hotter than the season average.
        rising = [_usage_row("Rising Guy", w, wopr) for w, wopr in [(1, 0.1), (2, 0.1), (3, 0.1), (4, 0.9), (5, 0.9)]]
        # Fading role: recent games run colder than the season average.
        fading = [_usage_row("Fading Guy", w, wopr) for w, wopr in [(1, 0.9), (2, 0.9), (3, 0.9), (4, 0.1), (5, 0.1)]]
        out = usage_form(
            2023, 6, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rising + fading)),
            min_games=3, recent_games=2,
        )
        assert out["rising guy"]["usage"] > out["fading guy"]["usage"]

    def test_leakage_future_weeks_never_affect_an_earlier_call(self, tmp_path):
        rows = [_usage_row("Real Wideout", w, 0.3) for w in (1, 2)] + [
            _usage_row("Real Wideout", w, 0.9) for w in (3, 4, 5)
        ]
        out = usage_form(2023, 3, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rows)), min_games=3)
        assert "real wideout" not in out  # only 2 prior games visible before week 3

    def test_qb_k_def_are_excluded(self, tmp_path):
        rows = [_usage_row("Some Qb", w, 0.9, position="QB") for w in (1, 2, 3, 4)]
        out = usage_form(2023, 5, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rows)), min_games=3)
        assert "some qb" not in out

    def test_missing_wopr_is_skipped_not_a_crash(self, tmp_path):
        rows = [{"player_display_name": "No Wopr", "position": "WR", "team": "MIA", "season": 2023, "week": w, "wopr": ""} for w in (1, 2, 3, 4)]
        out = usage_form(2023, 5, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rows)), min_games=3)
        assert "no wopr" not in out

    def test_output_keyed_by_bare_normalized_name(self, tmp_path):
        rows = [_usage_row("Real Wideout", w, 0.3 + w * 0.1) for w in (1, 2, 3, 4)]
        out = usage_form(2023, 5, Config(), cache_dir=tmp_path, opener=_usage_opener(_usage_csv(rows)), min_games=3)
        assert "real wideout" in out
        assert "real wideout:WR" not in out


class TestCombineProviders:
    def test_merges_disjoint_signals_for_the_same_name(self, tmp_path):
        # Build one combined CSV with BOTH the scoring fields historical_form
        # needs and the wopr field usage_form needs, since both providers hit
        # the identical stats_player_week source in a real fetch.
        combined_fields = list(dict.fromkeys(_PLAYER_FIELDS + _USAGE_FIELDS))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=combined_fields, restval="")
        w.writeheader()
        for wk in (1, 2, 3, 4):
            row = _row("Real Wideout", wk, 50)
            row["wopr"] = 0.3 + wk * 0.1
            w.writerow(row)
        combined_csv = buf.getvalue().encode("utf-8")

        def combined_opener(url: str) -> bytes:
            if "stats_player_week" in url:
                return combined_csv
            if "stats_team_week" in url:
                return b"team,season,week\n"
            if "schedules/games.csv" in url:
                return b"season,week,home_team,away_team,home_score,away_score\n"
            raise AssertionError(f"unexpected fetch: {url}")

        provider = combine_providers(historical_form, usage_form)
        out = provider(2023, 5, _cfg(), cache_dir=tmp_path, opener=combined_opener)
        assert "volatility" in out["real wideout"]
        assert "upside" in out["real wideout"]
        assert "usage" in out["real wideout"]

    def test_single_provider_output_is_unchanged_by_wrapping(self, tmp_path):
        rows = [_row("Real Wideout", w, 50 + w) for w in (1, 2, 3, 4)]
        cfg = _cfg()
        direct = historical_form(2023, 5, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)))
        wrapped = combine_providers(historical_form)(2023, 5, cfg, cache_dir=tmp_path, opener=_opener(_csv(rows)))
        assert direct == wrapped
