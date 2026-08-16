from __future__ import annotations

import json
import pytest
import random
from dataclasses import replace

from ffbot import webapi
from ffbot import week
from ffbot.board import Board, load_board
from ffbot.config import Config, DraftConfig, SeasonConfig
from ffbot.draft import DraftState
from ffbot.draft_ui import UiState, handle
from ffbot.league_rosters import LeagueRosters
from ffbot.report import LoadedReport
from tests.conftest import mk, mk_bp

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


def _write_board_csv(tmp_path, counts: dict[str, int], rng: random.Random):
    rows = []
    for pos, n in counts.items():
        for i in range(n):
            rows.append(
                f"{pos}{i},XXX,{pos},{rng.randint(5, 14)},"
                f"{round(rng.uniform(20, 340), 1)},{round(rng.uniform(1, 250), 1)}"
            )
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS,AVG\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _new_draft_state(tmp_path, num_teams=12, my_slot=4, rounds=15, seed=1) -> UiState:
    rng = random.Random(seed)
    cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=num_teams, my_slot=my_slot, rounds=rounds))
    path = _write_board_csv(tmp_path, {"QB": 15, "RB": 40, "WR": 50, "TE": 20, "K": 15, "DEF": 15}, rng)
    board = load_board([path], STANDARD_LAYOUT, num_teams, cfg)
    draft = DraftState(board=board, num_teams=num_teams, my_slot=my_slot, rounds=rounds, roster_positions=STANDARD_LAYOUT)
    return UiState(draft=draft, cfg=cfg)


class TestDraftStateJson:
    def test_returns_json_serializable_dict(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        json.dumps(out)  # must not raise

    def test_header_reflects_pick_and_order(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=12, my_slot=4)
        out = webapi.draft_state_json(state)
        assert out["header"]["pick"] == 1
        assert out["header"]["round"] == 1
        assert out["header"]["slot_on_clock"] == 1
        assert out["header"]["my_slot"] == 4
        assert out["header"]["order"] == "snake"
        assert out["header"]["on_the_clock"] is False

    def test_recommendations_match_gui_recommend_count(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        assert len(out["recommendations"]) == state.cfg.draft.gui_recommend_count
        assert out["recommendations"][0]["rank"] == 1
        first_names = {r["name"] for r in out["recommendations"]}
        assert len(first_names) == len(out["recommendations"])  # no dup rows

    def test_recommendation_why_parts_and_intel_note_present(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        rec = out["recommendations"][0]
        assert isinstance(rec["why_parts"], list)
        assert isinstance(rec["intel_note"], str)
        # Joining the parts back must reconstruct information present in the
        # original joined string -- not necessarily identical (the intel
        # note, if any, is deliberately extracted out), but never invented.
        for part in rec["why_parts"]:
            assert part in rec["why"]

    def test_pending_menu_serialized_on_ambiguous_search(self, tmp_path):
        state = _new_draft_state(tmp_path)
        state = handle(state, "Q")  # prefix-matches every QB -> pending menu
        out = webapi.draft_state_json(state)
        assert out["pending"]
        assert out["recommendations"] == []  # recommendations hidden while a menu is up
        assert out["pending"][0]["index"] == 1

    def test_roster_reflects_my_picks(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        key = state.draft.board.players[0].key
        state.draft.record(key, mine=True)
        out = webapi.draft_state_json(state)
        assert len(out["roster"]) == 1
        assert out["roster"][0]["key"] == key

    def test_roster_resolves_a_blank_team_defense_via_defense_key(self, tmp_path):
        # draft/proj_dst.csv ships an empty Team column for every DEF row --
        # names.defense_key is what week._resolve_team already uses to paper
        # over this same gap on the weekly path; draft_state_json's roster
        # payload must resolve it the same way rather than surfacing "".
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        original = next(bp for bp in state.draft.board.players if bp.position == "DEF")
        forced = replace(original, name="Houston Texans", team="")
        state.draft.board.by_key[forced.key] = forced
        state.draft.board.players[state.draft.board.players.index(original)] = forced
        state.draft.record(forced.key, mine=True)

        out = webapi.draft_state_json(state)
        assert out["roster"][0]["team"] == "HOU"

    def test_draft_log_and_message_present(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        key = state.draft.board.players[0].key
        state = handle(state, state.draft.board.players[0].name)
        out = webapi.draft_state_json(state)
        assert out["draft_log"]
        assert out["draft_log"][0]["key"] == key
        assert out["draft_log"][0]["slot"] == state.draft.my_slot
        assert out["draft_log"][0]["round"] == 1  # pick 1 is always round 1
        assert isinstance(out["message"], str) and out["message"]

    def test_draft_log_round_advances_with_pick_number(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        for _ in range(5):  # picks 1-4 (round 1), pick 5 (round 2)
            key = next(bp.key for bp in state.draft.board.players if bp.key not in state.draft.taken_keys())
            state.draft.record(key)
        out = webapi.draft_state_json(state)
        by_number = {p["number"]: p["round"] for p in out["draft_log"]}
        assert by_number[1] == 1
        assert by_number[4] == 1
        assert by_number[5] == 2

    def test_opponents_cover_every_slot(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=2)
        out = webapi.draft_state_json(state)
        assert len(out["opponents"]) == 4
        assert {o["slot"] for o in out["opponents"]} == {1, 2, 3, 4}

    def test_opponent_roster_carries_team_and_bye_week(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=2)
        key = next(bp.key for bp in state.draft.board.players if bp.position != "DEF")
        state.draft.record(key, mine=False)  # pick 1 -- slot 1's roster
        out = webapi.draft_state_json(state)
        opp = next(o for o in out["opponents"] if o["slot"] == 1)
        assert opp["roster"][0]["key"] == key
        assert "team" in opp["roster"][0] and "bye_week" in opp["roster"][0]

    def test_opponent_roster_resolves_a_blank_team_defense(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=2)
        original = next(bp for bp in state.draft.board.players if bp.position == "DEF")
        forced = replace(original, name="Houston Texans", team="")
        state.draft.board.by_key[forced.key] = forced
        state.draft.board.players[state.draft.board.players.index(original)] = forced
        state.draft.record(forced.key, mine=False)  # pick 1 -- slot 1's roster

        out = webapi.draft_state_json(state)
        opp = next(o for o in out["opponents"] if o["slot"] == 1)
        assert opp["roster"][0]["team"] == "HOU"
        me = next(o for o in out["opponents"] if o["slot"] == 2)
        assert me["is_me"] is True

    def test_header_reports_planning_mode_when_not_on_the_clock(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=4)
        out = webapi.draft_state_json(state)
        assert out["header"]["mode"] == "planning"
        assert out["header"]["picks_until_mine"] == 3

    def test_header_reports_on_clock_mode(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        out = webapi.draft_state_json(state)
        assert out["header"]["mode"] == "on_clock"
        assert out["header"]["picks_until_mine"] == 0


class TestDraftSearchJson:
    def test_empty_query_returns_no_matches(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_search_json(state, "   ")
        assert out["matches"] == []

    def test_matches_exclude_taken_players(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        target = next(bp for bp in state.draft.board.players if bp.position == "QB")
        state.draft.record(target.key, mine=True)
        out = webapi.draft_search_json(state, target.name[:4])
        assert target.key not in {m["key"] for m in out["matches"]}

    def test_matches_respect_limit(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_search_json(state, "r", limit=3)
        assert len(out["matches"]) <= 3

    def test_unique_exact_match_is_unambiguous(self, tmp_path):
        # The GUI's entry box (task 14) needs this to auto-pick without
        # showing a menu -- same rule draft_ui._search_and_pick uses.
        state = _new_draft_state(tmp_path)
        target = state.draft.board.players[0]
        out = webapi.draft_search_json(state, target.name)
        assert out["unambiguous_key"] == target.key

    def test_ambiguous_query_has_no_unambiguous_key(self, tmp_path):
        # A bare position prefix ("r" against RB0, RB1, ...) matches many
        # players at once -- must never silently auto-pick one of them.
        state = _new_draft_state(tmp_path)
        out = webapi.draft_search_json(state, "r")
        assert len(out["matches"]) > 1
        assert out["unambiguous_key"] is None

    def test_no_match_has_no_unambiguous_key(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_search_json(state, "zzz_nobody_matches_this_zzz")
        assert out["matches"] == []
        assert out["unambiguous_key"] is None


class TestWeeklyReportJson:
    def _board(self):
        players = [
            mk_bp("Roster Rb", "RB", points=180.0, rank=1, vor=180.0),
            mk_bp("Roster Wr", "WR", points=170.0, rank=2, vor=170.0),
            mk_bp("Waiver Gem", "WR", points=200.0, rank=3, vor=200.0),
            mk_bp("Waiver Kicker", "K", points=90.0, rank=4, vor=10.0),
        ]
        return Board(
            players=players,
            by_key={p.key: p for p in players},
            replacement={"RB": 50.0, "WR": 45.0, "K": 40.0},
            starters_per_pos={},
            tier_last={},
        )

    def _roster(self):
        return [
            mk("Roster Rb", "RB", slot="BN", proj=15.0),
            mk("Roster Wr", "WR", slot="BN", proj=14.0),
        ]

    def _layout(self):
        return {"RB": 1, "WR": 1, "K": 1, "BN": 3}

    def _loaded(
        self, board=True, waiver_priority=None, players=None, weekly=None, slots_source="file",
        league_rosters=None, league_rosters_source="file",
    ):
        cfg = Config(roster_positions=self._layout(), season=SeasonConfig(ros_blend=1.0))
        return LoadedReport(
            cfg=cfg,
            weekly=weekly if weekly is not None else week.WeeklyIntel(),
            board=self._board() if board else None,
            players=players if players is not None else self._roster(),
            unmatched=[],
            stadiums={},
            league_rosters=league_rosters if league_rosters is not None else LeagueRosters(),
            waiver_priority=waiver_priority,
            slots_source=slots_source,
            league_rosters_source=league_rosters_source,
        )

    def test_returns_json_serializable_dict(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        json.dumps(out)  # must not raise

    def test_basic_fields_present(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        assert out["week"] == 3
        assert "lineup" in out
        assert out["committed"] is False

    def test_no_board_skips_add_drop_candidates_but_keeps_start_sit(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(board=False), week_num=3, lineup_state_path=tmp_path / "state.yml",
            show_waivers=True,
        )
        assert out["moves"]["adds"] == []
        assert out["moves"]["claims"] == []
        assert isinstance(out["moves"]["start_sit"], list)  # still computed with no board

    def test_forced_k_need_produces_a_move_row(self, tmp_path):
        # This fixture's roster carries no kicker at all against a layout
        # with a K starting slot -- a real, unfilled need that must
        # surface as an ordinary add/claim row (streaming is a REASON on a
        # row now, not its own category).
        out = webapi.weekly_report_json(
            self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml", show_waivers=True,
        )
        rows = out["moves"]["adds"] + out["moves"]["claims"]
        k_rows = [r for r in rows if r["position"] == "K"]
        assert k_rows
        assert any(r["forced_need"] for r in k_rows)

    def test_show_waivers_populates_candidates_and_ir_stash(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml", show_waivers=True,
        )
        assert "moves" in out
        assert "opponent" in out
        all_rows = out["moves"]["adds"] + out["moves"]["claims"]
        assert any(r["add_name"] == "Waiver Gem" for r in all_rows)
        assert "ir_stash" in out["moves"]

    def test_commit_lineup_false_does_not_write_state(self, tmp_path):
        state_path = tmp_path / "state.yml"
        webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=state_path, commit_lineup=False)
        assert not state_path.exists()

    def test_no_priority_anywhere_stays_none_with_no_source(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        assert out["waiver_priority"] is None
        assert out["waiver_priority_source"] is None

    def test_live_priority_used_when_no_explicit_value_given(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(waiver_priority=6), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        assert out["waiver_priority"] == 6
        assert out["waiver_priority_source"] == "sleeper"

    def test_explicit_priority_wins_over_live_value(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(waiver_priority=6), week_num=3, lineup_state_path=tmp_path / "state.yml",
            my_priority=2,
        )
        assert out["waiver_priority"] == 2
        assert out["waiver_priority_source"] == "explicit"

    def test_resolved_priority_feeds_the_actual_waiver_ranking(self, tmp_path):
        # Not just plumbing -- confirm the resolved value (live, no explicit
        # override) is the one week.waiver_candidates actually sizes
        # claim_cost against, by reading it straight out of claim_note's
        # "priority N/M" text -- and that the typed `kind` agrees with it.
        out = webapi.weekly_report_json(
            self._loaded(waiver_priority=6), week_num=3, lineup_state_path=tmp_path / "state.yml",
            show_waivers=True,
        )
        gem = next(c for c in out["moves"]["claims"] if c["add_name"] == "Waiver Gem")
        assert gem["claim_note"] == "CLAIM (priority 6/12)"
        assert gem["kind"] == "claim"

    def test_commit_lineup_true_writes_state(self, tmp_path):
        state_path = tmp_path / "state.yml"
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=state_path, commit_lineup=True)
        assert state_path.exists()
        assert out["committed"] is True

    def test_assignments_include_position_team_status_and_matchup(self, tmp_path):
        players = [
            mk("Roster Rb", "RB", slot="BN", proj=15.0, team="BUF", status="Q"),
            mk("Roster Wr", "WR", slot="BN", proj=14.0),
        ]
        weekly = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", kickoff_et="2026-09-14T13:00", home=True)})
        out = webapi.weekly_report_json(
            self._loaded(players=players, weekly=weekly), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        rb = next(a for a in out["lineup"]["assignments"] if a["name"] == "Roster Rb")
        assert rb["position"] == "RB"
        assert rb["team"] == "BUF"
        assert rb["status"] == "Q"
        assert rb["opponent"] == "MIA"
        assert rb["kickoff_et"] == "2026-09-14T13:00"
        assert rb["home"] is True

    def test_assignment_matchup_fields_are_none_with_no_researched_game(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        rb = next(a for a in out["lineup"]["assignments"] if a["name"] == "Roster Rb")
        assert rb["opponent"] is None
        assert rb["kickoff_et"] is None
        assert rb["home"] is None

    def test_bench_reason_reflects_benched_for_cause(self, tmp_path):
        # benched_for_cause only fires for a player who WAS in a real
        # starting slot going in (see lineup.optimize) -- a plain BN
        # baseline never triggers it, hence slot="RB" here, not "BN".
        players = [
            mk("Roster Rb", "RB", slot="BN", proj=15.0),
            mk("Bye Guy", "RB", slot="RB", proj=20.0, bye_week=3),
        ]
        out = webapi.weekly_report_json(
            self._loaded(players=players), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        bye_entry = next(b for b in out["lineup"]["bench"] if b["name"] == "Bye Guy")
        assert bye_entry["reason"] == "bye week 3"

    def test_bench_reason_is_none_for_plain_outscored(self, tmp_path):
        players = [
            mk("Starter Rb", "RB", slot="BN", proj=20.0),
            mk("Bench Rb", "RB", slot="BN", proj=10.0),  # loses the single RB slot to Starter Rb
        ]
        out = webapi.weekly_report_json(
            self._loaded(players=players), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        bench_entry = next(b for b in out["lineup"]["bench"] if b["name"] == "Bench Rb")
        assert bench_entry["reason"] is None

    def test_ir_group_reflects_held_in_ir_players(self, tmp_path):
        players = [
            mk("Roster Rb", "RB", slot="BN", proj=15.0),
            mk("Hurt Guy", "RB", slot="IR", proj=18.0, status="IR"),
        ]
        out = webapi.weekly_report_json(
            self._loaded(players=players), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        assert len(out["lineup"]["ir"]) == 1
        ir_entry = out["lineup"]["ir"][0]
        assert ir_entry["name"] == "Hurt Guy"
        assert ir_entry["reason"] == "IR"
        assert all(a["name"] != "Hurt Guy" for a in out["lineup"]["assignments"])
        assert all(b["name"] != "Hurt Guy" for b in out["lineup"]["bench"])

    def test_intel_players_are_roster_filtered_and_ordered_and_matchups_carry_note(self, tmp_path):
        players = [
            mk("Roster Rb", "RB", slot="BN", proj=15.0),
            mk("Roster Wr", "WR", slot="BN", proj=14.0, team="BUF"),
        ]
        weekly = week.WeeklyIntel(
            players={
                "roster rb": week.WeeklyPlayerIntel(name="Roster Rb", note="my guy", status="Q"),
                "someone else": week.WeeklyPlayerIntel(name="Someone Else", note="not on my roster"),
            },
            games={"BUF": week.GameInfo(opponent="MIA", note="shootout expected")},
        )
        out = webapi.weekly_report_json(
            self._loaded(players=players, weekly=weekly), week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        intel_names = [p["name"] for p in out["intel"]["players"]]
        assert intel_names == ["Roster Rb"]  # roster order; non-roster entries excluded
        assert out["intel"]["players"][0]["note"] == "my guy"
        assert out["intel"]["players"][0]["status"] == "Q"
        [matchup] = out["intel"]["matchups"]
        assert matchup["note"] == "shootout expected"

    def test_live_slots_ignores_lineup_state_file(self, tmp_path):
        state_path = tmp_path / "state.yml"
        # If this were applied, Roster Rb's baseline slot would become "K"
        # first, producing a "K -> RB" move -- live slots must skip the
        # file entirely and use the player's own selected_position instead.
        state_path.write_text("roster rb: K\n", encoding="utf-8")
        players = [mk("Roster Rb", "RB", slot="RB", proj=15.0)]
        out = webapi.weekly_report_json(
            self._loaded(players=players, slots_source="sleeper"), week_num=3, lineup_state_path=state_path,
        )
        assert out["slots_source"] == "sleeper"
        assert out["lineup"]["is_noop"] is True

    def test_live_slots_skips_the_commit_write(self, tmp_path):
        state_path = tmp_path / "state.yml"
        players = [mk("Roster Rb", "RB", slot="RB", proj=15.0)]
        out = webapi.weekly_report_json(
            self._loaded(players=players, slots_source="sleeper"), week_num=3, lineup_state_path=state_path,
            commit_lineup=True,
        )
        assert not state_path.exists()
        assert out["committed"] is False

    def test_week_source_and_refreshed_echoed(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml",
            week_source="sleeper", refreshed=True,
        )
        assert out["week_source"] == "sleeper"
        assert out["refreshed"] is True

    def test_week_source_and_refreshed_default(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        assert out["week_source"] == "explicit"
        assert out["refreshed"] is False

    def test_league_rosters_object_shape(self, tmp_path):
        lr = LeagueRosters(week=3, generated="2026-08-13", source="api", teams={"A": ["X"], "B": ["Y"]}, unmatched=["A: 'Z'"])
        out = webapi.weekly_report_json(
            self._loaded(league_rosters=lr, league_rosters_source="sleeper"),
            week_num=3, lineup_state_path=tmp_path / "state.yml",
        )
        assert out["league_rosters"] == {
            "source": "sleeper",
            "teams_count": 2,
            "fetched_live": True,
            "generated": "2026-08-13",
            "unmatched_count": 1,
        }


class TestPickConfidenceInTheTable:
    """`p_best` per row plus the table-level `confidence` block.

    Purely presentational, so the tests that matter are that it is present,
    coherent, and cannot move the ranking.
    """

    def test_every_row_has_a_probability_and_they_sum_to_one(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        probs = [r["p_best"] for r in out["recommendations"]]
        assert all(p is not None for p in probs)
        assert sum(probs) == pytest.approx(1.0)

    def test_confidence_block_shape(self, tmp_path):
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        conf = out["confidence"]
        assert conf["n"] == len(out["recommendations"])
        assert conf["scale"] == state.cfg.draft.pick_confidence_scale
        assert 0.0 <= conf["normalized_entropy"] <= 1.0
        assert 1.0 <= conf["effective_options"] <= conf["n"]
        assert conf["top_p"] == out["recommendations"][0]["p_best"]

    def test_probabilities_descend_with_the_value_ranking(self, tmp_path):
        """A monotone transform of `value`, which is what makes it safe: it
        can only restate the ranking, never disagree with it."""
        state = _new_draft_state(tmp_path)
        out = webapi.draft_state_json(state)
        rows = out["recommendations"]
        for a, b in zip(rows, rows[1:]):
            if a["value"] > b["value"]:
                assert a["p_best"] >= b["p_best"]

    def test_scale_zero_disables_without_removing_the_key(self, tmp_path):
        state = _new_draft_state(tmp_path)
        state.cfg = replace(
            state.cfg, draft=replace(state.cfg.draft, pick_confidence_scale=0.0),
        )
        out = webapi.draft_state_json(state)
        assert all(r["p_best"] is None for r in out["recommendations"])
        assert out["confidence"]["top_p"] is None

    def test_changing_the_scale_never_reorders_the_table(self, tmp_path):
        """The invariant behind calling this presentational: the dial changes
        how confident the ranking LOOKS and nothing about the ranking."""
        state = _new_draft_state(tmp_path)
        sharp = webapi.draft_state_json(state)
        state.cfg = replace(
            state.cfg, draft=replace(state.cfg.draft, pick_confidence_scale=80.0),
        )
        flat = webapi.draft_state_json(state)
        assert [r["key"] for r in sharp["recommendations"]] == [
            r["key"] for r in flat["recommendations"]
        ]
        assert [r["value"] for r in sharp["recommendations"]] == [
            r["value"] for r in flat["recommendations"]
        ]
        # ...but the shape genuinely differs, or the dial does nothing.
        assert sharp["confidence"]["effective_options"] != pytest.approx(
            flat["confidence"]["effective_options"]
        )
