from __future__ import annotations

import json

from ffbot.config import KickingScoring, LeagueScoring, PassingScoring
from ffbot.projections.sleeper import fetch_season_points_rows


def _entry(company="rotowire", position="RB", first="Jahmyr", last="Gibbs", team="DET", pts_ppr=331.4):
    return {
        "company": company,
        "player": {"first_name": first, "last_name": last, "position": position, "team": team},
        "stats": {"pts_ppr": pts_ppr, "adp_ppr": 1.6},
    }


def _opener(entries):
    def opener(url: str) -> bytes:
        return json.dumps(entries).encode("utf-8")

    return opener


class TestFetchSeasonPointsRows:
    def test_shape(self, tmp_path):
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([_entry()]))
        assert rows == [{
            "name": "Jahmyr Gibbs", "team": "DET", "position": "RB",
            "points": 331.4, "bye": None, "stats": None,
        }]

    def test_non_rotowire_company_excluded(self, tmp_path):
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([_entry(company="other")]))
        assert rows == []

    def test_non_fantasy_position_excluded(self, tmp_path):
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([_entry(position="LB")]))
        assert rows == []

    def test_missing_points_excluded(self, tmp_path):
        entry = _entry()
        entry["stats"] = {}
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([entry]))
        assert rows == []

    def test_missing_name_excluded(self, tmp_path):
        entry = _entry(first="", last="")
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([entry]))
        assert rows == []

    def test_url_hits_sleeper_projections_season_endpoint(self, tmp_path):
        calls = []

        def opener(url):
            calls.append(url)
            return b"[]"

        fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener)
        assert calls[0].startswith("https://api.sleeper.com/projections/nfl/2026?")
        assert "order_by=adp_ppr" in calls[0]

    def test_caches_under_the_given_dir(self, tmp_path):
        calls = []

        def opener(url):
            calls.append(url)
            return json.dumps([_entry()]).encode("utf-8")

        fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener)
        fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener)
        assert len(calls) == 1  # second call hit the cache
        assert list(tmp_path.glob("*.json"))


def _weekly_entry(position, stats, first="K1", last="Kicker", team="DET"):
    return {
        "company": "rotowire",
        "player": {"first_name": first, "last_name": last, "position": position, "team": team},
        "stats": stats,
    }


def _dispatch_opener(season_payload, weekly_payload_by_week=None, *, weekly_raises=False):
    """Fake opener that returns `season_payload` for the season endpoint
    (`api.sleeper.com/projections/nfl/<season>?...`, no week segment) and
    `weekly_payload_by_week[week]` for a weekly endpoint call
    (`api.sleeper.app/projections/nfl/<season>/<week>?...`), or raises
    OSError for every weekly call when `weekly_raises` is set (simulating a
    network failure on the K/DEF ratio sample)."""
    weekly_payload_by_week = weekly_payload_by_week or {}

    def opener(url: str) -> bytes:
        for wk, payload in weekly_payload_by_week.items():
            if f"/nfl/2026/{wk}?" in url:
                return json.dumps(payload).encode("utf-8")
        if "/nfl/2026/" in url:  # a weekly URL with no configured payload
            if weekly_raises:
                raise OSError("simulated network failure")
            return json.dumps([]).encode("utf-8")
        return json.dumps(season_payload).encode("utf-8")

    return opener


class TestFetchSeasonPointsRowsLeagueScored:
    def test_league_none_keeps_the_exact_old_row_shape(self, tmp_path):
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([_entry()]))
        assert set(rows[0].keys()) == {"name", "team", "position", "points", "bye", "stats"}

    def test_offense_row_is_league_scored_from_season_stats(self, tmp_path):
        league = LeagueScoring(passing=PassingScoring(yards_per_point=1.0, td=0.0, int=0.0, two_pt=0.0))
        entry = _entry(position="QB", pts_ppr=5.0)
        entry["stats"]["pass_yd"] = 100.0
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([entry]), league=league)
        row = rows[0]
        assert row["points"] == 100.0  # 100 pass_yds / 1.0 yards_per_point, td/int/2pt all zeroed
        assert row["points_fp"] == 5.0  # original Sleeper consensus preserved
        assert row["points_source"] == "league"
        assert row["stats"] is None  # never a StatLine on the returned row itself

    def test_offense_row_league_none_keeps_consensus_points_untouched(self, tmp_path):
        league = LeagueScoring(passing=PassingScoring(yards_per_point=1.0))
        entry = _entry(position="QB", pts_ppr=5.0)
        rows_no_league = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=_opener([entry]))
        assert rows_no_league[0]["points"] == 5.0

    def test_kdef_ratio_applied_to_season_consensus(self, tmp_path):
        # A league that scores kickers far more richly than whatever
        # consensus Sleeper's weekly pts_ppr happens to carry.
        league = LeagueScoring(kicking=KickingScoring(fg_made=3.0, pat_made=2.0))
        weekly_k = _weekly_entry("K", {"pts_ppr": 10.0, "fgm": 2.0, "xpm": 3.0})
        # 2 fg * 3.0 + 3 pat * 2.0 = 12.0 league pts vs. 10.0 consensus -> ratio 1.2
        season_entry = _entry(position="K", first="K1", last="Kicker", pts_ppr=100.0)
        opener = _dispatch_opener([season_entry], {wk: [weekly_k] for wk in (1, 2, 3, 4)})
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener, league=league)
        row = rows[0]
        assert round(row["points"], 2) == 120.0
        assert row["points_fp"] == 100.0
        assert row["points_source"] == "league"
        assert "season_ratio_estimated" in row["points_flags"]

    def test_kdef_position_with_no_weekly_sample_falls_back_to_consensus(self, tmp_path):
        league = LeagueScoring()
        season_entry = _entry(position="DEF", first="Some", last="Defense", pts_ppr=80.0)
        # Weekly sample has no DEF rows at all -> consensus_sum["DEF"] stays 0.
        opener = _dispatch_opener([season_entry], {wk: [] for wk in (1, 2, 3, 4)})
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener, league=league)
        row = rows[0]
        assert row["points"] == 80.0
        assert row["points_source"] == "consensus"
        assert row["points_flags"] == ()

    def test_kdef_ratio_sample_fetch_failure_degrades_to_consensus(self, tmp_path):
        league = LeagueScoring(kicking=KickingScoring(fg_made=99.0))
        season_entry = _entry(position="K", first="K1", last="Kicker", pts_ppr=100.0)
        opener = _dispatch_opener([season_entry], weekly_raises=True)
        rows = fetch_season_points_rows(2026, cache_dir=tmp_path, opener=opener, league=league)
        row = rows[0]
        # Every sampled week failed to fetch -> no ratio -> plain consensus,
        # never a crash.
        assert row["points"] == 100.0
        assert row["points_source"] == "consensus"
