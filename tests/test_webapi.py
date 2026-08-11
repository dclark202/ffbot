from __future__ import annotations

import json
import random

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

    def test_draft_log_and_message_present(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=1)
        key = state.draft.board.players[0].key
        state = handle(state, state.draft.board.players[0].name)
        out = webapi.draft_state_json(state)
        assert out["draft_log"]
        assert out["draft_log"][0]["key"] == key
        assert out["draft_log"][0]["slot"] == state.draft.my_slot
        assert isinstance(out["message"], str) and out["message"]

    def test_opponents_cover_every_slot(self, tmp_path):
        state = _new_draft_state(tmp_path, num_teams=4, my_slot=2)
        out = webapi.draft_state_json(state)
        assert len(out["opponents"]) == 4
        assert {o["slot"] for o in out["opponents"]} == {1, 2, 3, 4}
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

    def _loaded(self, board=True):
        cfg = Config(roster_positions=self._layout(), season=SeasonConfig(ros_blend=1.0))
        return LoadedReport(
            cfg=cfg,
            weekly=week.WeeklyIntel(),
            board=self._board() if board else None,
            players=self._roster(),
            unmatched=[],
            stadiums={},
            league_rosters=LeagueRosters(),
        )

    def test_returns_json_serializable_dict(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        json.dumps(out)  # must not raise

    def test_basic_fields_present(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        assert out["week"] == 3
        assert "lineup" in out
        assert out["committed"] is False

    def test_no_board_skips_roster_status_and_waivers(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(board=False), week_num=3, lineup_state_path=tmp_path / "state.yml",
            stream_positions=["K"], show_waivers=True,
        )
        assert "roster_status" not in out
        assert "streamers" not in out
        assert "waivers" not in out

    def test_board_present_populates_roster_status(self, tmp_path):
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml")
        assert out["roster_status"]["capacity"] == 6  # RB1+WR1+K1+BN3, IR excluded

    def test_stream_positions_populate_streamers(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml", stream_positions=["K"],
        )
        assert "K" in out["streamers"]
        assert out["streamers"]["K"][0]["name"] == "Waiver Kicker"

    def test_show_waivers_populates_candidates_and_ir_stash(self, tmp_path):
        out = webapi.weekly_report_json(
            self._loaded(), week_num=3, lineup_state_path=tmp_path / "state.yml", show_waivers=True,
        )
        assert "waivers" in out
        assert "ir_stash" in out
        assert any(c["add_name"] == "Waiver Gem" for c in out["waivers"]["candidates"])

    def test_commit_lineup_false_does_not_write_state(self, tmp_path):
        state_path = tmp_path / "state.yml"
        webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=state_path, commit_lineup=False)
        assert not state_path.exists()

    def test_commit_lineup_true_writes_state(self, tmp_path):
        state_path = tmp_path / "state.yml"
        out = webapi.weekly_report_json(self._loaded(), week_num=3, lineup_state_path=state_path, commit_lineup=True)
        assert state_path.exists()
        assert out["committed"] is True
