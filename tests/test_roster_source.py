from __future__ import annotations

import pytest

from ffbot import roster_source as rs
from ffbot.models import Player


def _write_roster(tmp_path, names):
    path = tmp_path / "roster.yml"
    lines = ["players:"] + [f"  - {n}" for n in names]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_projections(tmp_path, rows, filename="proj.csv"):
    path = tmp_path / filename
    lines = ["Player,Team,POS,BYE,FPTS"]
    for name, team, pos, bye, pts in rows:
        lines.append(f"{name},{team},{pos},{bye},{pts}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLoadRosterNames:
    def test_reads_names_in_order(self, tmp_path):
        path = _write_roster(tmp_path, ["Josh Allen", "Bijan Robinson"])
        assert rs.load_roster_names(path) == ["Josh Allen", "Bijan Robinson"]

    def test_missing_file_raises_with_a_helpful_message(self, tmp_path):
        with pytest.raises(rs.RosterError, match="roster.example.yml"):
            rs.load_roster_names(tmp_path / "does_not_exist.yml")

    def test_empty_players_list_raises(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text("players: []\n", encoding="utf-8")
        with pytest.raises(rs.RosterError):
            rs.load_roster_names(path)

    def test_non_mapping_top_level_raises(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(rs.RosterError):
            rs.load_roster_names(path)

    def test_blank_lines_in_the_names_list_are_dropped(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text('players:\n  - "Josh Allen"\n  - ""\n  - "Bijan Robinson"\n', encoding="utf-8")
        assert rs.load_roster_names(path) == ["Josh Allen", "Bijan Robinson"]


class TestLoadWeeklyProjectionRows:
    def test_loads_a_plain_csv(self, tmp_path):
        path = _write_projections(tmp_path, [("Josh Allen", "BUF", "QB", 7, 24.5)])
        rows = rs.load_weekly_projection_rows([path])
        assert rows[0]["name"] == "Josh Allen" and rows[0]["points"] == 24.5

    def test_position_override_for_single_position_export(self, tmp_path):
        # Weekly QB/K/DST exports have the same missing-POS-column problem
        # the draft board already solved -- this proves the same fix reused.
        path = tmp_path / "qb.csv"
        path.write_text("Player,Team,FPTS\nJosh Allen,BUF,24.5\n", encoding="utf-8")
        rows = rs.load_weekly_projection_rows([{"path": path, "position": "QB"}])
        assert rows[0]["position"] == "QB"

    def test_rows_without_points_or_position_are_dropped(self, tmp_path):
        path = tmp_path / "proj.csv"
        path.write_text("Player,Team,POS,FPTS\nA,BUF,WR,\nB,BUF,WR,10\n", encoding="utf-8")
        rows = rs.load_weekly_projection_rows([path])
        assert [r["name"] for r in rows] == ["B"]


class TestMatchRoster:
    def test_exact_match_resolves_to_a_player(self):
        rows = [{"name": "Josh Allen", "team": "BUF", "position": "QB", "points": 24.5, "bye": 7}]
        matches = rs.match_roster(["Josh Allen"], rows)
        assert matches[0].player is not None
        assert matches[0].player.name == "Josh Allen"
        assert matches[0].player.team == "BUF"
        assert matches[0].player.projected_points == 24.5
        assert matches[0].player.bye_week == 7

    def test_suffix_and_punctuation_differences_still_match(self):
        rows = [{"name": "Marvin Harrison Jr.", "team": "ARI", "position": "WR", "points": 12.0, "bye": 14}]
        matches = rs.match_roster(["Marvin Harrison"], rows)
        assert matches[0].player is not None

    def test_unmatched_name_returns_none_player_with_a_suggestion(self):
        # A dropped letter (query is a subsequence of the real name) is the
        # typo shape names.search_scored actually resolves -- see its
        # docstring: prefix/subsequence matching, not difflib-style fuzzing.
        # A query with EXTRA characters (a doubled letter, say) cannot match
        # by this method at all, since it can never be a subsequence of a
        # shorter string; that's a real, pre-existing directional gap in
        # search_scored (also present in ffbot.intel's suggestion path),
        # not something to work around here.
        rows = [{"name": "Puka Nacua", "team": "LAR", "position": "WR", "points": 15.0, "bye": 11}]
        matches = rs.match_roster(["Puka Naca"], rows)
        assert matches[0].player is None
        assert matches[0].suggestion == "Puka Nacua"

    def test_unmatched_name_with_no_candidates_gets_no_suggestion(self):
        matches = rs.match_roster(["Nobody At All"], [])
        assert matches[0].player is None
        assert matches[0].suggestion is None

    def test_each_name_gets_a_distinct_player_id(self):
        rows = [
            {"name": "A", "team": "BUF", "position": "WR", "points": 10.0, "bye": 7},
            {"name": "B", "team": "MIA", "position": "RB", "points": 8.0, "bye": 6},
        ]
        matches = rs.match_roster(["A", "B"], rows)
        ids = {m.player.player_id for m in matches}
        assert len(ids) == 2

    def test_first_source_wins_on_duplicate_names(self):
        # Mirrors board._merge_csv_rows's convention.
        rows = [
            {"name": "A", "team": "BUF", "position": "WR", "points": 10.0, "bye": 7},
            {"name": "A", "team": "BUF", "position": "WR", "points": 99.0, "bye": 7},
        ]
        matches = rs.match_roster(["A"], rows)
        assert matches[0].player.projected_points == 10.0


class TestLoadRosterEndToEnd:
    def test_full_roster_resolves_cleanly(self, tmp_path):
        roster_path = _write_roster(tmp_path, ["Josh Allen", "Bijan Robinson"])
        proj_path = _write_projections(
            tmp_path,
            [("Josh Allen", "BUF", "QB", 7, 24.5), ("Bijan Robinson", "ATL", "RB", 11, 18.2)],
        )
        players, unmatched = rs.load_roster([proj_path], roster_path)
        assert len(players) == 2 and unmatched == []

    def test_partial_match_surfaces_the_gap_without_failing_outright(self, tmp_path):
        roster_path = _write_roster(tmp_path, ["Josh Allen", "A Player Not In Projections"])
        proj_path = _write_projections(tmp_path, [("Josh Allen", "BUF", "QB", 7, 24.5)])
        players, unmatched = rs.load_roster([proj_path], roster_path)
        assert len(players) == 1
        assert len(unmatched) == 1
        assert unmatched[0].query == "A Player Not In Projections"


class TestRosterEntryMapping:
    def test_bare_string_entries_still_work(self, tmp_path):
        # The existing low-upkeep contract must survive unchanged.
        path = _write_roster(tmp_path, ["Josh Allen", "Bijan Robinson"])
        entries = rs.load_roster_entries(path)
        assert [e.name for e in entries] == ["Josh Allen", "Bijan Robinson"]
        assert all(not e.undroppable and e.keeper_round is None for e in entries)

    def test_mapping_entry_sets_metadata(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n"
            "  - Josh Allen\n"
            "  - name: Travis Kelce\n"
            "    undroppable: true\n"
            "    keeper_round: 3\n"
            "    blocking: true\n"
            "    note: \"Can't Cut List\"\n",
            encoding="utf-8",
        )
        entries = rs.load_roster_entries(path)
        assert entries[0] == rs.RosterEntry(name="Josh Allen")
        kelce = entries[1]
        assert kelce.name == "Travis Kelce"
        assert kelce.undroppable is True
        assert kelce.keeper_round == 3
        assert kelce.blocking is True
        assert kelce.note == "Can't Cut List"

    def test_mapping_entry_without_name_raises(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text("players:\n  - undroppable: true\n", encoding="utf-8")
        with pytest.raises(rs.RosterError, match="'name'"):
            rs.load_roster_entries(path)

    def test_mixed_bare_and_mapping_list(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n  - Josh Allen\n  - name: Bijan Robinson\n    undroppable: false\n",
            encoding="utf-8",
        )
        entries = rs.load_roster_entries(path)
        assert [e.name for e in entries] == ["Josh Allen", "Bijan Robinson"]

    def test_load_roster_names_is_a_thin_wrapper(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n  - Josh Allen\n  - name: Travis Kelce\n    undroppable: true\n",
            encoding="utf-8",
        )
        assert rs.load_roster_names(path) == ["Josh Allen", "Travis Kelce"]

    def test_parse_roster_entry_accepts_str_dict_or_roster_entry(self):
        assert rs.parse_roster_entry("Josh Allen") == rs.RosterEntry(name="Josh Allen")
        assert rs.parse_roster_entry({"name": "Josh Allen"}) == rs.RosterEntry(name="Josh Allen")
        entry = rs.RosterEntry(name="Josh Allen", undroppable=True)
        assert rs.parse_roster_entry(entry) is entry


class TestUndroppableEndToEnd:
    def test_undroppable_flag_reaches_the_player_and_policy_refuses_the_drop(self, tmp_path):
        from ffbot import policy
        from ffbot.config import Config

        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n  - name: Travis Kelce\n    undroppable: true\n",
            encoding="utf-8",
        )
        proj_path = _write_projections(tmp_path, [("Travis Kelce", "KC", "TE", 10, 15.0)])
        players, unmatched = rs.load_roster([proj_path], path)
        assert unmatched == []
        assert players[0].is_undroppable is True

        verdict = policy.can_drop(players[0], Config())
        assert verdict.allowed is False
        assert "undroppable" in verdict.reason

    def test_keeper_round_reaches_the_player_and_protects_the_drop(self, tmp_path):
        from ffbot import policy
        from ffbot.config import Config, DropPolicyConfig

        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n  - name: Early Pick\n    keeper_round: 2\n",
            encoding="utf-8",
        )
        proj_path = _write_projections(tmp_path, [("Early Pick", "KC", "TE", 10, 15.0)])
        players, _ = rs.load_roster([proj_path], path)
        assert players[0].draft_round == 2

        cfg = Config(drops=DropPolicyConfig(protect_draft_rounds=4))
        verdict = policy.can_drop(players[0], cfg)
        assert verdict.allowed is False
        assert "round 2" in verdict.reason


class TestSeasonBoardRows:
    def _board(self):
        from ffbot.board import Board
        from tests.conftest import mk_bp

        players = [mk_bp("Josh Allen", "QB", points=340.0, team="BUF")]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def test_rescales_by_weeks_remaining(self):
        rows = rs.season_board_rows(self._board(), weeks_remaining=17)
        assert rows[0]["points"] == pytest.approx(340.0 / 17)

    def test_floors_weeks_remaining_at_one(self):
        # A late-season call with 0 or negative weeks left must not divide by
        # zero or invert the sign -- it should read as "the whole rest of
        # value is due this week," not crash or go negative.
        rows = rs.season_board_rows(self._board(), weeks_remaining=0)
        assert rows[0]["points"] == pytest.approx(340.0)


class TestLoadRosterFallback:
    def test_fallback_fills_names_the_csv_does_not_cover(self, tmp_path):
        roster_path = _write_roster(tmp_path, ["Josh Allen", "Only In Fallback"])
        proj_path = _write_projections(tmp_path, [("Josh Allen", "BUF", "QB", 7, 24.5)])
        fallback = [{"name": "Only In Fallback", "team": "MIA", "position": "WR", "points": 5.0, "bye": 6}]
        players, unmatched = rs.load_roster([proj_path], roster_path, fallback_rows=fallback)
        assert len(players) == 2 and unmatched == []

    def test_weekly_csv_wins_over_fallback_for_the_same_name(self, tmp_path):
        roster_path = _write_roster(tmp_path, ["Josh Allen"])
        proj_path = _write_projections(tmp_path, [("Josh Allen", "BUF", "QB", 7, 24.5)])
        fallback = [{"name": "Josh Allen", "team": "BUF", "position": "QB", "points": 999.0, "bye": 7}]
        players, _ = rs.load_roster([proj_path], roster_path, fallback_rows=fallback)
        assert players[0].projected_points == 24.5

    def test_no_csv_paths_uses_fallback_alone(self, tmp_path):
        roster_path = _write_roster(tmp_path, ["Only In Fallback"])
        fallback = [{"name": "Only In Fallback", "team": "MIA", "position": "WR", "points": 5.0, "bye": 6}]
        players, unmatched = rs.load_roster([], roster_path, fallback_rows=fallback)
        assert len(players) == 1 and unmatched == []


class TestLineupState:
    def _plan(self):
        from ffbot.lineup import LineupPlan

        starter = Player(player_id=1, name="Josh Allen", eligible_positions=["QB"])
        bench_player = Player(player_id=2, name="Bench Guy", eligible_positions=["WR"])
        return LineupPlan(assignments=[("QB", starter)], bench=[bench_player])

    def test_save_reads_the_assignment_slot_not_the_stale_field(self, tmp_path):
        # The regression case: optimize() never mutates Player.selected_position
        # to reflect where it just placed someone -- the new slot only exists
        # as the assignments tuple's first element. Reading .selected_position
        # instead would silently save everyone as BN, defeating the whole
        # point of this function (confirmed by hand before this fix existed).
        path = tmp_path / "state.yml"
        plan = self._plan()
        assert plan.assignments[0][1].selected_position == "BN"  # never mutated
        rs.save_lineup_state(path, plan)
        state = rs.load_lineup_state(path)
        assert state["josh allen"] == "QB"

    def test_bench_players_are_saved_as_bench(self, tmp_path):
        path = tmp_path / "state.yml"
        rs.save_lineup_state(path, self._plan())
        assert rs.load_lineup_state(path)["bench guy"] == "BN"

    def test_load_missing_file_is_empty(self, tmp_path):
        assert rs.load_lineup_state(tmp_path / "nope.yml") == {}

    def test_apply_seeds_selected_position_from_state(self):
        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"])]
        out = rs.apply_lineup_state(players, {"josh allen": "QB"})
        assert out[0].selected_position == "QB"

    def test_apply_leaves_unknown_players_at_the_default(self):
        # A brand-new add has no remembered state -- and "everything moves"
        # is the honest answer the very first time this tool has seen them.
        players = [Player(player_id=1, name="Brand New Guy", eligible_positions=["WR"])]
        out = rs.apply_lineup_state(players, {"someone else": "WR"})
        assert out[0].selected_position == "BN"

    def test_round_trip_produces_no_changes_on_a_second_run(self, tmp_path):
        # The actual promise this mechanism exists for: run once, save state,
        # run again with the same roster -- the second run's optimizer input
        # should already reflect real slots, so nothing "moves."
        from ffbot.config import Config
        from ffbot.lineup import optimize

        path = tmp_path / "state.yml"
        layout = {"QB": 1, "BN": 3}
        roster = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"], projected_points=20.0)]

        plan1 = optimize(roster, layout, None, Config())
        rs.save_lineup_state(path, plan1)

        state = rs.load_lineup_state(path)
        seeded = rs.apply_lineup_state(roster, state)
        plan2 = optimize(seeded, layout, None, Config())
        assert plan2.is_noop()
