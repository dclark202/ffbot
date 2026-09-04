from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.training_report import main, parse_round_filter, roster_provenance

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


class TestRoundFilter:
    """`--rounds` exists because two packs built with different
    `--rounds-range` are not comparable, and comparing them anyway is the
    easiest mistake this report invites -- review rounds 1 and 2 read as a
    57% -> 17% collapse in agree rate purely because round 2 sampled only
    the middle rounds."""

    def test_expands_a_human_range_to_the_buckets_it_covers(self):
        assert parse_round_filter("R3-9") == {"R3-5", "R6-9"}
        assert parse_round_filter("R1-14") == {"R1-2", "R3-5", "R6-9", "R10+"}
        assert parse_round_filter("R10+") == {"R10+"}

    def test_accepts_an_exact_bucket_label_and_a_comma_list(self):
        assert parse_round_filter("R3-5") == {"R3-5"}
        assert parse_round_filter("R1-2,R10+") == {"R1-2", "R10+"}

    def test_no_filter_is_none_not_every_bucket(self):
        # The caller has to be able to tell "nothing asked for" from "a
        # filter that happens to match everything" -- only the former stays
        # silent in the report header.
        assert parse_round_filter(None) is None
        assert parse_round_filter("") is None

    def test_garbage_is_a_clean_error(self):
        with pytest.raises(ValueError):
            parse_round_filter("rounds 3 to 9")

    def test_restricts_both_the_denominator_and_the_records(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {
                "s1": {"choices": ["p1:RB"], "verdict": "agree"},
                "s2": {"choices": ["p2:WR"], "verdict": "disagree"},
                "s3": {"choices": ["p2:WR"], "verdict": "disagree"},
            },
        )
        rc = main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"), "--rounds", "R3-9",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Only s2 is in R3-5; s1 (R1-2) and s3 (R10+) drop out of BOTH the
        # answered count and the scenario total, or the rate is nonsense.
        assert "1 answered of 1 scenarios" in out
        assert "restricted to rounds: R3-5, R6-9" in out


class TestVerdictIndependentMetrics:
    def test_rank_histogram_and_top3_rate_are_printed(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {
                "s1": {"choices": ["p1:RB"], "verdict": "disagree"},
                "s2": {"choices": ["p2:WR"], "verdict": "disagree"},
            },
        )
        main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        out = capsys.readouterr().out
        # Both picks sit inside a two-row table, so both are within the top 3
        # even though the reviewer pressed "disagree" on both -- which is the
        # entire point of reporting this next to the agree rate.
        assert "reviewer's #1 within the engine's top 3: 2/2 (100%)" in out
        assert "#1:1" in out and "#2:1" in out
        assert "agree rate is self-reported and drifts between rounds" in out


class TestNonePosition:
    """A "none of these" answer names no player, so it has no graded block.
    Without `none_position` it is invisible in every table except the raw
    disagree count -- backwards, since "none of these" on a roster missing a
    mandatory starter is the sharpest signal the tool can collect."""

    def test_none_position_lands_in_the_positional_matrix(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {"s1": {"choices": [], "verdict": "none", "none_position": "TE"}},
        )
        main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        out = capsys.readouterr().out
        assert "Positional bias" in out
        matrix = out.split("Positional bias")[1]
        # s1's engine top rec is RB; the reviewer said TE.
        assert "TE" in matrix
        assert 'includes 1 "none of these" answer(s)' in out

    def test_an_unnamed_none_answer_is_reported_as_uncountable(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json",
            {"s1": {"choices": [], "verdict": "none", "note": "I would take a TE"}},
        )
        main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ])
        out = capsys.readouterr().out
        assert "named neither a player nor a" in out

    def test_older_responses_without_the_field_still_grade(self, tmp_path, pack_path, capsys):
        # Both returned files so far predate `none_position`; they must
        # degrade to the previous behaviour, never error.
        responses_path = _write_responses(
            tmp_path, "resp.json", {"s1": {"choices": ["p1:RB"], "verdict": "agree"}},
        )
        assert main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"),
        ]) == 0


class TestFeedbackFileIsIdempotent:
    """Grading is deterministic and re-run freely; an append-only file made
    every re-run silently double the record count, which would quietly
    corrupt any later pooled analysis across review rounds."""

    def _run(self, tmp_path, pack_path, extra=()):
        responses_path = _write_responses(
            tmp_path, "resp.json", {"s1": {"choices": ["p1:RB"], "verdict": "agree"}},
        )
        return main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"), *extra,
        ])

    def _lines(self, tmp_path):
        path = next((tmp_path / "feedback").glob("*.jsonl"))
        return path.read_text(encoding="utf-8").strip().splitlines()

    def test_regrading_replaces_rather_than_doubles(self, tmp_path, pack_path, capsys):
        self._run(tmp_path, pack_path)
        self._run(tmp_path, pack_path)
        capsys.readouterr()
        assert len(self._lines(tmp_path)) == 1

    def test_append_flag_still_accumulates(self, tmp_path, pack_path, capsys):
        self._run(tmp_path, pack_path)
        self._run(tmp_path, pack_path, extra=["--append"])
        capsys.readouterr()
        assert len(self._lines(tmp_path)) == 2

    def test_reviewer_flag_overrides_a_blank_name_in_the_file(self, tmp_path, pack_path, capsys):
        responses_path = _write_responses(
            tmp_path, "resp.json", {"s1": {"choices": ["p1:RB"], "verdict": "agree"}}, reviewer="",
        )
        main([
            "--pack", str(pack_path), "--responses", str(responses_path),
            "--feedback-dir", str(tmp_path / "feedback"), "--reviewer", "dad",
        ])
        capsys.readouterr()
        assert (tmp_path / "feedback" / "pack-x-dad.jsonl").exists()


class TestRosterProvenance:
    """Which spice level drafted the rosters decides whether a roster
    complaint is about a bot or about the engine itself. Review round 2 was
    generated at the engine's own level and nothing said so."""

    def test_matching_spice_says_the_complaint_is_about_the_engine(self):
        lines = roster_provenance({"generator": {"my_spice": 3}, "config": {"spice_level": 3}})
        assert any("ENGINE" in line for line in lines)

    def test_differing_spice_says_the_complaint_is_about_the_bot(self):
        lines = roster_provenance({"generator": {"my_spice": 2}, "config": {"spice_level": 3}})
        assert any("bot in that seat" in line for line in lines)

    def test_a_pack_predating_the_field_says_nothing(self):
        assert roster_provenance({}) == []
        assert roster_provenance({"generator": {}, "config": {"spice_level": 3}}) == []
