from __future__ import annotations

import json

from ffbot.projections.sleeper import _row_from_entry, fetch_weekly_rows


def _entry(**overrides) -> dict:
    base = {
        "company": "rotowire",
        "player": {"first_name": "Josh", "last_name": "Allen", "position": "QB", "team": "BUF"},
        "stats": {"pts_ppr": 24.5, "pass_yd": 260.0, "pass_td": 2.1, "pass_int": 0.6, "rush_yd": 35.0, "rush_td": 0.4, "fum_lost": 0.1},
    }
    base.update(overrides)
    return base


def _opener(calls: list, payload):
    def opener(url: str) -> bytes:
        calls.append(url)
        return json.dumps(payload).encode("utf-8")

    return opener


class TestRowFromEntry:
    def test_offense_row_shape_matches_read_fantasypros(self):
        row = _row_from_entry(_entry())
        assert row["name"] == "Josh Allen"
        assert row["team"] == "BUF"
        assert row["position"] == "QB"
        assert row["points"] == 24.5
        assert row["bye"] is None  # Sleeper carries no bye -- board fallback fills it

    def test_offense_stat_line_mapped(self):
        row = _row_from_entry(_entry())
        stats = row["stats"]
        assert stats.pass_yds == 260.0
        assert stats.pass_td == 2.1
        assert stats.pass_int == 0.6
        assert stats.rush_yds == 35.0
        assert stats.rush_td == 0.4
        assert stats.fumbles_lost == 0.1

    def test_receiver_stat_line_mapped(self):
        entry = _entry(
            player={"first_name": "Ja'Marr", "last_name": "Chase", "position": "WR", "team": "CIN"},
            stats={"pts_ppr": 19.8, "rec": 6.2, "rec_yd": 88.0, "rec_td": 0.7, "rush_yd": 2.0, "fum_lost": 0.05},
        )
        row = _row_from_entry(entry)
        stats = row["stats"]
        assert stats.rec == 6.2
        assert stats.rec_yds == 88.0
        assert stats.rec_td == 0.7

    def test_kicker_stat_line_mapped_without_distance_bands(self):
        entry = _entry(
            player={"first_name": "Cameron", "last_name": "Dicker", "position": "K", "team": "LAC"},
            stats={"pts_ppr": 8.3, "fgm": 2.08, "fga": 2.41, "xpm": 2.73, "xpmiss": 0.13, "fgm_20_29": 0.46, "fgm_30_39": 0.65},
        )
        row = _row_from_entry(entry)
        stats = row["stats"]
        assert stats.fg_made == 2.08
        assert stats.fg_att == 2.41
        assert stats.pat_made == 2.73
        assert stats.pat_missed == 0.13
        # Deliberately not mapped -- see sleeper.py's module docstring on why
        # partial distance bands are unsafe to trust.
        assert stats.fg_made_bands is None

    def test_defense_stat_line_mapped(self):
        entry = _entry(
            player={"first_name": "Jacksonville", "last_name": "Jaguars", "position": "DEF", "team": "JAX"},
            stats={"pts_ppr": 8.43, "sack": 2.97, "int": 0.9, "fum_rec": 0.69, "ff": 0.9, "def_td": 0.21, "pts_allow": 16.5},
        )
        row = _row_from_entry(entry)
        assert row["name"] == "Jacksonville Jaguars"
        assert row["team"] == "JAX"
        stats = row["stats"]
        assert stats.sack == 2.97
        assert stats.interception == 0.9
        assert stats.points_allowed_game == 16.5

    def test_entry_with_no_points_is_dropped(self):
        # Matches the real duplicate/inactive-player entries Sleeper's
        # player database carries alongside the real, projected ones.
        entry = _entry(stats={})
        assert _row_from_entry(entry) is None

    def test_entry_from_a_non_rotowire_company_is_dropped(self):
        entry = _entry(company="some_other_provider")
        assert _row_from_entry(entry) is None

    def test_unknown_position_is_dropped(self):
        entry = _entry(player={"first_name": "X", "last_name": "Y", "position": "LB", "team": "BUF"})
        assert _row_from_entry(entry) is None

    def test_missing_name_is_dropped(self):
        entry = _entry(player={"first_name": "", "last_name": "", "position": "QB", "team": "BUF"})
        assert _row_from_entry(entry) is None


class TestFetchWeeklyRows:
    def test_fetches_and_converts_every_valid_entry(self, tmp_path):
        calls: list[str] = []
        payload = [
            _entry(),
            _entry(player={"first_name": "Lamar", "last_name": "Jackson", "position": "QB", "team": "BAL"}, stats={"pts_ppr": 22.0}),
            _entry(stats={}),  # dropped -- no points
        ]
        rows = fetch_weekly_rows(2026, 1, cache_dir=tmp_path, opener=_opener(calls, payload))
        assert len(rows) == 2
        assert {r["name"] for r in rows} == {"Josh Allen", "Lamar Jackson"}
        assert len(calls) == 1

    def test_url_targets_the_right_season_and_week(self, tmp_path):
        calls: list[str] = []
        fetch_weekly_rows(2026, 6, cache_dir=tmp_path, opener=_opener(calls, []))
        assert "/2026/6" in calls[0]
        assert "season_type=regular" in calls[0]

    def test_cache_hit_skips_a_second_fetch(self, tmp_path):
        calls: list[str] = []
        fetch_weekly_rows(2026, 1, cache_dir=tmp_path, opener=_opener(calls, [_entry()]))
        fetch_weekly_rows(2026, 1, cache_dir=tmp_path, opener=_opener(calls, [_entry()]))
        assert len(calls) == 1
