from __future__ import annotations

import json

import pytest

from ffbot.board import load_board
from ffbot.config import Config, DraftConfig
from ffbot.draft import DraftState
from ffbot.draft_report import taken_block
from ffbot.draft_ui import UiState
from ffbot import training

LAYOUT = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "BN": 4}


def _board(tmp_path, cfg):
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    for pos, base in (("QB", 300.0), ("RB", 290.0), ("WR", 285.0), ("TE", 230.0)):
        for i in range(24):
            lines.append(f"{pos}{i},XXX,{pos},7,{base - 4.0 * i},{i * 4 + 1}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return load_board([path], cfg.roster_positions, 12, cfg)


@pytest.fixture
def setup(tmp_path):
    cfg = Config(roster_positions=LAYOUT, draft=DraftConfig(num_teams=12, gui_recommend_count=5))
    board = _board(tmp_path, cfg)
    return cfg, board


def _state(board, cfg, my_slot=1, rounds=14):
    draft = DraftState(board=board, num_teams=12, my_slot=my_slot, rounds=rounds, roster_positions=LAYOUT)
    return UiState(draft=draft, cfg=cfg)


class TestBuildScenario:
    def test_shape(self, setup):
        cfg, board = setup
        ui_state = _state(board, cfg, my_slot=1)
        scenario = training.build_scenario(ui_state, scenario_id="d1-p1", source_draft="d1")

        assert scenario["id"] == "d1-p1"
        assert scenario["source_draft"] == "d1"
        assert scenario["round"] == 1
        assert scenario["pick"] == 1
        assert scenario["my_slot"] == 1
        assert scenario["round_bucket"] == "R1-2"
        assert scenario["top_rec_position"] in {"QB", "RB", "WR", "TE"}
        # The whole webapi.draft_state_json() shape rides along unmodified --
        # this is the entire point of reusing it rather than a second copy.
        assert "recommendations" in scenario["state"]
        assert "roster" in scenario["state"]
        assert "opponents" in scenario["state"]
        assert "draft_log" in scenario["state"]

    def test_round_bucket_boundaries(self):
        assert training._round_bucket(1) == "R1-2"
        assert training._round_bucket(2) == "R1-2"
        assert training._round_bucket(3) == "R3-5"
        assert training._round_bucket(5) == "R3-5"
        assert training._round_bucket(6) == "R6-9"
        assert training._round_bucket(9) == "R6-9"
        assert training._round_bucket(10) == "R10+"
        assert training._round_bucket(999) == "R10+"

    def test_no_recommendations_gives_none_top_position(self, setup):
        cfg, board = setup
        # Fill every board player onto SOME roster so recommend() returns nothing.
        draft = DraftState(board=board, num_teams=12, my_slot=1, rounds=14, roster_positions=LAYOUT)
        for bp in board.players:
            try:
                draft.record(bp.key, mine=False)
            except ValueError:
                pass
        ui_state = UiState(draft=draft, cfg=cfg)
        scenario = training.build_scenario(ui_state, scenario_id="x", source_draft="d1")
        assert scenario["top_rec_position"] is None


def _fake_candidate(round_bucket: str, position: str, source_draft: str, cid: str) -> dict:
    """A minimal stand-in candidate for stratify() -- it only reads
    round_bucket/top_rec_position/source_draft, so a full scenario isn't
    needed to test the selection logic in isolation."""
    return {
        "id": cid,
        "source_draft": source_draft,
        "round": 1,
        "pick": 1,
        "my_slot": 1,
        "round_bucket": round_bucket,
        "top_rec_position": position,
        "state": {},
    }


class TestStratify:
    def test_returns_everything_when_count_exceeds_pool(self):
        candidates = [_fake_candidate("R1-2", "RB", "d1", f"c{i}") for i in range(5)]
        result = training.stratify(candidates, 10, seed=1)
        assert {c["id"] for c in result} == {c["id"] for c in candidates}

    def test_deterministic_under_seed(self):
        candidates = [
            _fake_candidate(bucket, pos, f"d{i % 4}", f"c{i}")
            for i, (bucket, pos) in enumerate(
                [("R1-2", "RB"), ("R3-5", "WR"), ("R6-9", "QB"), ("R10+", "TE")] * 10
            )
        ]
        a = training.stratify(candidates, 12, seed=42)
        b = training.stratify(candidates, 12, seed=42)
        assert [c["id"] for c in a] == [c["id"] for c in b]

    def test_spreads_across_buckets(self):
        candidates = [
            _fake_candidate(bucket, pos, f"d{i % 6}", f"c{i}")
            for i, (bucket, pos) in enumerate(
                [("R1-2", "RB"), ("R3-5", "WR"), ("R6-9", "QB"), ("R10+", "TE")] * 15
            )
        ]
        result = training.stratify(candidates, 16, seed=3)
        buckets = {c["round_bucket"] for c in result}
        # All four buckets were available in equal supply -- a genuinely
        # stratified pick should touch every one, not collapse onto one.
        assert buckets == {"R1-2", "R3-5", "R6-9", "R10+"}

    def test_no_single_draft_dominates(self):
        # 10 drafts contribute one candidate apiece to the SAME bucket/position
        # except one draft, which contributes 20 -- without a per-draft cap
        # that one draft would flood a small pack.
        candidates = [_fake_candidate("R1-2", "RB", "flood", f"f{i}") for i in range(20)]
        candidates += [_fake_candidate("R1-2", "RB", f"d{i}", f"c{i}") for i in range(10)]
        result = training.stratify(candidates, 10, seed=1)
        counts = {}
        for c in result:
            counts[c["source_draft"]] = counts.get(c["source_draft"], 0) + 1
        assert counts.get("flood", 0) < 10, "one draft should not supply the entire pack"


class TestGradeResponse:
    @pytest.fixture
    def scenario(self):
        table = [
            {"rank": 1, "key": "p1:RB", "name": "Player One", "position": "RB", "proj": 200.0, "value": 100.0, "p_best": 0.6},
            {"rank": 2, "key": "p2:WR", "name": "Player Two", "position": "WR", "proj": 190.0, "value": 80.0, "p_best": 0.3},
            {"rank": 3, "key": "p3:QB", "name": "Player Three", "position": "QB", "proj": 180.0, "value": 60.0, "p_best": 0.1},
        ]
        return {
            "id": "s1", "round": 3, "pick": 30, "round_bucket": "R3-5", "top_rec_position": "RB",
            "state": {"recommendations": table, "confidence": {"effective_options": 1.4}},
        }

    def test_top_choice_matches_engine_top(self, scenario):
        answer = {"choices": ["p1:RB"], "verdict": "agree", "note": ""}
        record = training.grade_response(scenario, answer)
        assert record["scenario_id"] == "s1"
        assert len(record["graded"]) == 1
        assert record["graded"][0]["was_top_recommendation"] is True
        assert record["graded"][0]["rank_in_table"] == 1
        assert record["graded"][0]["value_gap_to_top"] == 0.0

    def test_off_table_choice_grades_as_unranked(self, scenario):
        answer = {"choices": ["someone-else:TE"], "verdict": "disagree", "note": "not in the table"}
        record = training.grade_response(scenario, answer)
        assert record["graded"][0]["rank_in_table"] is None
        assert record["graded"][0]["was_top_recommendation"] is False
        assert record["graded"][0]["value_gap_to_top"] is None

    def test_no_choices_grades_to_empty_list(self, scenario):
        answer = {"choices": [], "verdict": "none", "note": "none of these"}
        record = training.grade_response(scenario, answer)
        assert record["graded"] == []
        assert record["verdict"] == "none"

    def test_second_choice_graded_against_same_table(self, scenario):
        answer = {"choices": ["p2:WR", "p1:RB"], "verdict": "close", "note": ""}
        record = training.grade_response(scenario, answer)
        assert record["graded"][0]["rank_in_table"] == 2
        assert record["graded"][1]["rank_in_table"] == 1

    def test_grade_response_reuses_taken_block_shape(self, scenario):
        """Anti-drift: a training grade and a real draft-report grade must
        carry the exact same keys, or a training verdict and a draft report
        could describe the same disagreement with different numbers -- the
        drift `taken_block`'s docstring exists to prevent."""
        answer = {"choices": ["p1:RB"], "verdict": "agree", "note": ""}
        record = training.grade_response(scenario, answer)
        table = scenario["state"]["recommendations"]
        direct = taken_block(table, "p1:RB", None)
        assert set(record["graded"][0].keys()) == set(direct.keys())


class TestMergeResponses:
    @pytest.fixture
    def pack(self):
        table = [{"rank": 1, "key": "p1:RB", "name": "P1", "position": "RB", "proj": 100.0, "value": 50.0, "p_best": 1.0}]
        scenarios = [
            {"id": "s1", "round": 1, "pick": 1, "round_bucket": "R1-2", "top_rec_position": "RB",
             "state": {"recommendations": table, "confidence": {}}},
            {"id": "s2", "round": 4, "pick": 20, "round_bucket": "R3-5", "top_rec_position": "RB",
             "state": {"recommendations": table, "confidence": {}}},
        ]
        return {"pack_id": "p1", "scenarios": scenarios}

    def test_merges_known_scenarios(self, pack):
        responses = {"reviewer": "Dad", "answers": {"s1": {"choices": ["p1:RB"], "verdict": "agree", "note": ""}}}
        records, warnings = training.merge_responses(pack, responses)
        assert len(records) == 1
        assert records[0]["scenario_id"] == "s1"
        assert records[0]["reviewer"] == "Dad"
        assert warnings == []

    def test_unknown_scenario_id_is_skipped_with_a_warning(self, pack):
        responses = {"reviewer": "Dad", "answers": {"bogus": {"choices": [], "verdict": "agree", "note": ""}}}
        records, warnings = training.merge_responses(pack, responses)
        assert records == []
        assert len(warnings) == 1
        assert "bogus" in warnings[0]

    def test_partial_pack_only_grades_whats_answered(self, pack):
        responses = {"reviewer": "Dad", "answers": {"s1": {"choices": [], "verdict": "agree", "note": ""}}}
        records, warnings = training.merge_responses(pack, responses)
        assert {r["scenario_id"] for r in records} == {"s1"}


class TestReadWritePack:
    def test_round_trip(self, tmp_path):
        pack = {"pack_id": "abc", "scenarios": []}
        path = training.write_pack(pack, tmp_path / "nested" / "pack.json")
        assert path is not None
        assert training.read_pack(path) == pack

    def test_unwritable_destination_degrades_to_none_not_a_crash(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        assert training.write_pack({"pack_id": "x"}, blocker / "pack.json") is None
