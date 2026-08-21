from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.training_report import main

_TABLE_A = [
    {"rank": 1, "key": "p1:RB", "name": "Player One", "position": "RB", "proj": 200.0, "value": 100.0, "p_best": 0.7},
    {"rank": 2, "key": "p2:WR", "name": "Player Two", "position": "WR", "proj": 190.0, "value": 80.0, "p_best": 0.2},
]

_PACK = {
    "pack_id": "pack-x",
    "label": "Test Pack",
    "scenarios": [
        {
            "id": "s1", "round": 1, "pick": 1, "round_bucket": "R1-2", "top_rec_position": "RB",
            "state": {"recommendations": _TABLE_A, "confidence": {"effective_options": 1.2}},
        },
        {
            "id": "s2", "round": 4, "pick": 40, "round_bucket": "R3-5", "top_rec_position": "RB",
            "state": {"recommendations": _TABLE_A, "confidence": {"effective_options": 5.0}},
        },
        {
            "id": "s3", "round": 12, "pick": 120, "round_bucket": "R10+", "top_rec_position": "WR",
            "state": {"recommendations": _TABLE_A, "confidence": {"effective_options": 1.1}},
        },
    ],
}


@pytest.fixture
def pack_path(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(_PACK), encoding="utf-8")
    return path


def _write_responses(tmp_path, name, answers, pack_id="pack-x", reviewer="Dad"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"pack_id": pack_id, "reviewer": reviewer, "answers": answers}), encoding="utf-8",
    )
    return path


class TestEndToEnd:
    def test_agree_and_disagree_summary(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {
                "s1": {"choices": ["p1:RB"], "verdict": "agree", "note": ""},
                "s2": {"choices": ["p2:WR"], "verdict": "disagree", "note": "prefer WR here"},
                "s3": {"choices": [], "verdict": "none", "note": "none of these"},
            },
        )
        rc = main([
            "--pack", str(pack_path),
            "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "3 answered of 3 scenarios" in out
        assert "agree rate: 1/3" in out
        assert "reviewer's #1 == engine's #1: 1/2" in out

        feedback_files = list((tmp_path / "feedback").glob("*.jsonl"))
        assert len(feedback_files) == 1
        lines = feedback_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        records = [json.loads(line) for line in lines]
        assert {r["scenario_id"] for r in records} == {"s1", "s2", "s3"}

    def test_unknown_scenario_warns_but_does_not_fail(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(tmp_path, "resp.json", {"bogus": {"choices": [], "verdict": "agree", "note": ""}})
        rc = main([
            "--pack", str(pack_path),
            "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "bogus" in err

    def test_multiple_reviewers_are_merged(self, tmp_path, pack_path, capsys):
        r1 = _write_responses(tmp_path, "dad.json", {"s1": {"choices": ["p1:RB"], "verdict": "agree", "note": ""}}, reviewer="Dad")
        r2 = _write_responses(tmp_path, "mom.json", {"s2": {"choices": ["p2:WR"], "verdict": "agree", "note": ""}}, reviewer="Mom")
        rc = main([
            "--pack", str(pack_path),
            "--responses", str(r1),
            "--responses", str(r2),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "across 2 reviewer(s)" in out
        feedback_files = sorted((tmp_path / "feedback").glob("*.jsonl"))
        assert len(feedback_files) == 2

    def test_missing_pack_file_is_a_clean_error(self, tmp_path):
        rc = main(["--pack", str(tmp_path / "nope.json"), "--responses", str(tmp_path / "nope2.json")])
        assert rc == 1

    def test_no_answers_at_all_does_not_crash(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(tmp_path, "resp.json", {})
        rc = main([
            "--pack", str(pack_path),
            "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "nothing answered yet" in out


class TestConvictionAndRosterHealth:
    def test_strong_conviction_on_a_flat_table_is_called_out(self, tmp_path, pack_path, capsys):
        # s2's table reports 5.0 effective options -- a toss-up. A reviewer
        # who is CERTAIN there is the pairing the section exists to surface.
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {"s2": {"choices": ["p2:WR"], "verdict": "disagree", "conviction": "strong", "note": ""}},
        )
        rc = main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Conviction vs. the engine's own confidence" in out
        assert "reviewer was CERTAIN and the engine was flat" in out

    def test_roster_health_splits_the_disagreement_rate(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {
                "s1": {"choices": ["p1:RB"], "verdict": "agree", "roster_health": "good", "note": ""},
                "s2": {"choices": ["p2:WR"], "verdict": "disagree", "roster_health": "bad", "note": ""},
            },
        )
        rc = main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Roster health" in out
        assert "poorly built: 1" in out
        assert "good shape" in out

    def test_roster_notes_are_printed_worst_rating_first(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {
                "s1": {"choices": ["p1:RB"], "verdict": "agree",
                       "roster_health": "good", "roster_note": "balanced team", "note": ""},
                "s2": {"choices": ["p2:WR"], "verdict": "disagree",
                       "roster_health": "bad", "roster_note": "zero RBs by round 4", "note": ""},
            },
        )
        rc = main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "What they said about the rosters" in out
        assert "zero RBs by round 4" in out
        assert "balanced team" in out
        # Complaints lead -- a "poorly built" note must sort above a "good shape" one.
        assert out.index("zero RBs by round 4") < out.index("balanced team")

    def test_roster_note_section_absent_when_no_notes_written(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {"s1": {"choices": ["p1:RB"], "verdict": "agree", "roster_health": "good", "note": ""}},
        )
        main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        out = capsys.readouterr().out
        assert "Roster health" in out
        assert "What they said about the rosters" not in out

    def test_sections_are_skipped_entirely_when_unrated(self, tmp_path, pack_path, capsys):
        """A round-1 responses file predates both fields -- it must grade
        cleanly rather than print empty or zeroed sections."""
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {"s1": {"choices": ["p1:RB"], "verdict": "agree", "note": ""}},
        )
        rc = main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Conviction vs." not in out
        assert "Roster health" not in out
