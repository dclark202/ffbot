from __future__ import annotations

import json

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
