from __future__ import annotations

from ffbot.board import _board_key
from ffbot.history.names import (
    TEAM_RELOCATIONS,
    actuals_key,
    canonical_team,
    coverage_summary,
    index_by_key,
    match_actuals,
)


class TestCanonicalTeam:
    def test_relocated_franchises_map_to_current_abbreviation(self):
        assert canonical_team("OAK") == "LV"
        assert canonical_team("SD") == "LAC"
        assert canonical_team("STL") == "LAR"

    def test_current_abbreviations_pass_through(self):
        assert canonical_team("LV") == "LV"
        assert canonical_team("KC") == "KC"

    def test_case_and_whitespace_insensitive(self):
        assert canonical_team(" oak ") == "LV"

    def test_jac_alternate_spelling_maps_to_jax(self):
        # Not a relocation -- Fantasy Football Calculator's ADP API spells
        # Jacksonville "JAC" while nflverse (and ffbot.names.NFL_TEAMS) use
        # "JAX". Found via scripts/demo_season.py's coverage report: every
        # JAC-tagged historical_board() row was silently missing its
        # weather/Vegas/injury-report game lookup every week.
        assert canonical_team("JAC") == "JAX"

    def test_empty_or_none_passes_through(self):
        assert canonical_team(None) == ""
        assert canonical_team("") == ""

    def test_every_relocation_maps_to_a_stable_target(self):
        # No relocation target should itself need remapping (no chains).
        for target in TEAM_RELOCATIONS.values():
            assert canonical_team(target) == target


class TestActualsKey:
    def test_matches_board_key_convention(self):
        # Same convention as `board._board_key` — a historical row and a
        # board row must join with a plain dict lookup.
        assert actuals_key("Justin Jefferson", "WR") == _board_key("Justin Jefferson", "WR")

    def test_normalizes_position(self):
        assert actuals_key("Bob Smith", "wr") == actuals_key("Bob Smith", "WR")


class TestIndexByKey:
    def test_builds_lookup_from_rows(self):
        rows = [
            {"player_display_name": "Justin Jefferson", "position": "WR"},
            {"player_display_name": "Josh Allen", "position": "QB"},
        ]
        idx = index_by_key(rows)
        assert len(idx) == 2
        assert idx[actuals_key("Justin Jefferson", "WR")]["position"] == "WR"

    def test_rows_missing_name_or_position_are_skipped(self):
        rows = [{"player_display_name": "", "position": "WR"}, {"player_display_name": "Bob", "position": ""}]
        assert index_by_key(rows) == {}

    def test_custom_field_names(self):
        rows = [{"full_name": "Bob Smith", "pos": "RB"}]
        idx = index_by_key(rows, name_field="full_name", position_field="pos")
        assert actuals_key("Bob Smith", "RB") in idx


class TestMatchActuals:
    def test_exact_match(self):
        actual_rows = [{"player_display_name": "Justin Jefferson", "position": "WR", "team": "MIN"}]
        target_rows = [{"name": "Justin Jefferson", "position": "WR", "team": "MIN"}]
        results = match_actuals(actual_rows, target_rows)
        assert len(results) == 1
        assert results[0].matched_id == 0
        assert results[0].confidence == "exact"

    def test_relocated_team_still_matches(self):
        # nflverse row uses the historical abbreviation; target uses current.
        actual_rows = [{"player_display_name": "Derek Carr", "position": "QB", "team": "OAK"}]
        target_rows = [{"name": "Derek Carr", "position": "QB", "team": "LV"}]
        results = match_actuals(actual_rows, target_rows)
        assert results[0].matched_id == 0

    def test_unmatched_reports_none(self):
        actual_rows = [{"player_display_name": "Real Player", "position": "WR", "team": "MIN"}]
        target_rows = [{"name": "Nobody At All", "position": "WR", "team": "MIN"}]
        results = match_actuals(actual_rows, target_rows)
        assert results[0].matched_id is None
        assert results[0].confidence == "none"


class TestCoverageSummary:
    def test_all_matched(self):
        actual_rows = [{"player_display_name": "A", "position": "WR", "team": "MIN"}]
        target_rows = [{"name": "A", "position": "WR", "team": "MIN"}]
        summary = coverage_summary(match_actuals(actual_rows, target_rows))
        assert summary == {"matched": 1, "total": 1, "pct": 100.0}

    def test_empty_is_zero_not_a_crash(self):
        assert coverage_summary([]) == {"matched": 0, "total": 0, "pct": 0.0}
