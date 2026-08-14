from __future__ import annotations

from ffbot import week
from ffbot import weekly_editor as we


class TestWeeklyIntelEditorJson:
    def test_missing_file_returns_empty_template(self, tmp_path):
        out = we.weekly_intel_editor_json(tmp_path / "week-03.yml")
        assert out == {"week": None, "generated": "", "source_notes": "", "players": {}, "matchups": []}

    def test_one_matchup_produces_one_row_not_two(self, tmp_path):
        p = tmp_path / "week-03.yml"
        p.write_text(
            "week: 3\n"
            "games:\n"
            "  BUF:\n"
            "    opponent: MIA\n"
            "    home: true\n"
            "    wind_mph: 12\n"
            "    team_total: 26.5\n"
            "    opp_total: 20.0\n"
            "  MIA:\n"
            "    opponent: BUF\n"
            "    home: false\n"
            "    team_total: 20.0\n"
            "    opp_total: 26.5\n",
            encoding="utf-8",
        )
        out = we.weekly_intel_editor_json(p)
        assert len(out["matchups"]) == 1
        m = out["matchups"][0]
        assert m["home_team"] == "BUF"
        assert m["away_team"] == "MIA"
        assert m["wind_mph"] == 12
        assert m["home_team_total"] == 26.5
        assert m["away_team_total"] == 20.0

    def test_players_carried_through(self, tmp_path):
        p = tmp_path / "week-03.yml"
        p.write_text(
            "players:\n"
            "  Josh Allen:\n"
            "    status: Q\n"
            "    note: limited in practice\n",
            encoding="utf-8",
        )
        out = we.weekly_intel_editor_json(p)
        assert out["players"]["Josh Allen"]["status"] == "Q"
        assert out["players"]["Josh Allen"]["note"] == "limited in practice"

    def test_trend_fields_carried_through(self, tmp_path):
        # Regression: usage_trend/momentum/divergence parse fine
        # (week._parse_player_entry) and write fine (write_weekly_intel is
        # generic), but the editor's *read* side used to hardcode a field
        # list that omitted them -- so opening the GUI intel editor and
        # clicking Save with no edits would silently erase them.
        p = tmp_path / "week-03.yml"
        p.write_text(
            "players:\n"
            "  Some Guy:\n"
            "    usage_trend: 78\n"
            "    momentum: 61\n"
            "    divergence: 40\n",
            encoding="utf-8",
        )
        out = we.weekly_intel_editor_json(p)
        entry = out["players"]["Some Guy"]
        assert entry["usage_trend"] == 78
        assert entry["momentum"] == 61
        assert entry["divergence"] == 40


class TestWriteWeeklyIntel:
    def test_matchup_mirrored_onto_both_teams(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {
                "week": 3,
                "matchups": [
                    {
                        "home_team": "buf",
                        "away_team": "mia",
                        "wind_mph": 12,
                        "home_team_total": 26.5,
                        "away_team_total": 20.0,
                    }
                ],
            },
        )
        intel = week.load_weekly_intel(p)
        assert intel.games["BUF"].opponent == "MIA"
        assert intel.games["BUF"].home is True
        assert intel.games["BUF"].wind_mph == 12
        assert intel.games["BUF"].team_total == 26.5
        assert intel.games["BUF"].opp_total == 20.0
        assert intel.games["MIA"].opponent == "BUF"
        assert intel.games["MIA"].home is False
        assert intel.games["MIA"].wind_mph == 12  # mirrored, not left blank
        assert intel.games["MIA"].team_total == 20.0
        assert intel.games["MIA"].opp_total == 26.5

    def test_editing_one_side_cannot_drift_from_the_other(self, tmp_path):
        # The whole point of the matchup-centric write: there is only one
        # wind_mph field to edit, so the two teams can never disagree.
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {"matchups": [{"home_team": "BUF", "away_team": "MIA", "wind_mph": 18}]},
        )
        intel = week.load_weekly_intel(p)
        assert intel.games["BUF"].wind_mph == intel.games["MIA"].wind_mph == 18

    def test_round_trip_through_editor_json(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {
                "week": 3,
                "matchups": [
                    {"home_team": "BUF", "away_team": "MIA", "wind_mph": 12, "home_team_total": 26.5, "away_team_total": 20.0}
                ],
                "players": {"Josh Allen": {"status": "Q", "note": "limited"}},
            },
        )
        out = we.weekly_intel_editor_json(p)
        assert out["week"] == 3
        assert len(out["matchups"]) == 1
        assert out["matchups"][0]["home_team"] == "BUF"
        assert out["players"]["Josh Allen"]["status"] == "Q"

    def test_trend_fields_survive_load_editor_json_write_round_trip(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {
                "week": 3,
                "players": {"Some Guy": {"usage_trend": 78, "momentum": 61, "divergence": 40}},
            },
        )
        # Simulate the GUI: load into editor JSON, then write that straight
        # back with no edits -- the trend fields must not be lost.
        loaded = we.weekly_intel_editor_json(p)
        we.write_weekly_intel(p, loaded)
        intel = week.load_weekly_intel(p)
        entry = intel.players["some guy"]
        assert entry.usage_trend == 78
        assert entry.momentum == 61
        assert entry.divergence == 40

    def test_players_with_no_flags_write_as_blank(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(p, {"players": {"Josh Allen": {}}})
        intel = week.load_weekly_intel(p)
        assert intel.players["josh allen"].status == ""

    def test_incomplete_matchup_is_skipped(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(p, {"matchups": [{"home_team": "", "away_team": "MIA"}]})
        intel = week.load_weekly_intel(p)
        assert intel.games == {}

    def test_passthrough_fields_written_and_read_back(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {"matchups": [{"home_team": "TB", "away_team": "ATL", "venue": "LONDON_TOT", "international": True}]},
        )
        import yaml

        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert raw["games"]["TB"]["venue"] == "LONDON_TOT"
        assert raw["games"]["TB"]["international"] is True
        assert raw["games"]["ATL"]["venue"] == "LONDON_TOT"

    def test_note_field_round_trips_through_editor_json(self, tmp_path):
        p = tmp_path / "week-03.yml"
        we.write_weekly_intel(
            p,
            {"matchups": [{"home_team": "TB", "away_team": "ATL", "note": "shootout expected"}]},
        )
        intel = week.load_weekly_intel(p)
        assert intel.games["TB"].note == "shootout expected"
        assert intel.games["ATL"].note == "shootout expected"

        editor_json = we.weekly_intel_editor_json(p)
        assert editor_json["matchups"][0]["note"] == "shootout expected"
