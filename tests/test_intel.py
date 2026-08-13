from __future__ import annotations

import pytest

from ffbot.board import Board
from ffbot.intel import IntelEntry, IntelError, apply_intel, coverage, load_intel
from tests.conftest import mk_bp


def _board(players) -> Board:
    return Board(
        players=list(players),
        by_key={p.key: p for p in players},
        replacement={},
        starters_per_pos={},
        tier_last={},
    )


def _write(tmp_path, text: str):
    path = tmp_path / "intel.yml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadIntel:
    def test_missing_file_is_empty_not_an_error(self):
        # Intel is optional; its absence must be silent, not a failure.
        assert load_intel("does/not/exist.yml") == {}

    def test_empty_path_is_empty_not_an_error(self):
        # Regression: Path("") resolves to Path("."), the cwd, which
        # .exists() reports True for -- an empty/unset intel_file must
        # degrade the same as a missing one, not try to read the cwd as a
        # file and raise.
        assert load_intel("") == {}

    def test_full_entry(self, tmp_path):
        path = _write(
            tmp_path,
            "players:\n"
            '  "Jaxon Smith-Njigba":\n'
            "    upside: 82\n"
            "    flags: [post-hype, target-share-riser]\n"
            '    note: "took over the slot"\n',
        )
        intel = load_intel(path)
        entry = intel["jaxon smithnjigba"]
        assert entry.upside == 82.0
        assert entry.flags == ("post-hype", "target-share-riser")
        assert entry.note == "took over the slot"

    def test_bare_string_is_shorthand_for_a_note(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "Some Guy": "camp buzz"\n')
        assert load_intel(path)["some guy"].note == "camp buzz"
        assert load_intel(path)["some guy"].upside is None

    def test_block_scalar_note_is_collapsed_to_one_line(self, tmp_path):
        # YAML block scalars keep newlines and indentation; the note lands in a
        # single-line table row, so the whitespace has to be normalized.
        path = _write(
            tmp_path,
            'players:\n  "Some Guy":\n    note: >\n      locked into\n      the slot role\n',
        )
        assert load_intel(path)["some guy"].note == "locked into the slot role"

    def test_key_is_normalized_so_suffixes_match(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "Marvin Harrison Jr.": "note"\n')
        assert "marvin harrison" in load_intel(path)

    def test_out_of_range_upside_rejected(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "X":\n    upside: 140\n')
        with pytest.raises(IntelError):
            load_intel(path)

    def test_non_numeric_upside_rejected(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "X":\n    upside: "very high"\n')
        with pytest.raises(IntelError):
            load_intel(path)

    def test_empty_file_is_empty(self, tmp_path):
        assert load_intel(_write(tmp_path, "")) == {}


class TestApplyIntel:
    def test_merges_onto_matching_player(self):
        bp = mk_bp("Puka Nacua", "WR")
        board = _board([bp])
        merged = apply_intel(
            board, {"puka nacua": IntelEntry("Puka Nacua", upside=88.0, note="note", flags=("x",))}
        )
        out = merged.players[0]
        assert out.upside == 88.0
        assert out.intel_note == "note"
        assert out.intel_flags == ("x",)

    def test_matches_across_name_spelling(self):
        bp = mk_bp("Marvin Harrison", "WR")
        merged = apply_intel(_board([bp]), {"marvin harrison": IntelEntry("Marvin Harrison Jr.", note="n")})
        assert merged.players[0].intel_note == "n"

    def test_empty_intel_returns_the_same_board(self):
        board = _board([mk_bp("A", "WR")])
        assert apply_intel(board, {}) is board

    def test_untouched_players_keep_their_defaults(self):
        players = [mk_bp("Hit", "WR"), mk_bp("Miss", "WR")]
        merged = apply_intel(_board(players), {"hit": IntelEntry("Hit", 50.0, "n")})
        miss = merged.by_key["miss:WR"]
        assert miss.upside is None
        assert miss.intel_note == ""
        assert miss.intel_flags == ()

    def test_unmatched_entry_warns_with_a_suggestion(self):
        board = _board([mk_bp("Puka Nacua", "WR")])
        with pytest.warns(UserWarning, match="Puka Nacuaa"):
            apply_intel(board, {"puka nacuaa": IntelEntry("Puka Nacuaa", note="typo")})

    def test_by_key_is_rebuilt_so_lookups_see_the_intel(self):
        bp = mk_bp("Puka Nacua", "WR")
        merged = apply_intel(_board([bp]), {"puka nacua": IntelEntry("Puka Nacua", note="n")})
        assert merged.by_key["puka nacua:WR"].intel_note == "n"


class TestCoverage:
    def test_measures_against_adp_order_not_board_rank(self):
        # A player our board dislikes but the market takes early is still
        # someone we need an opinion on, so ADP is the right ordering.
        players = [
            mk_bp("Early Adp", "WR", adp=1.0, rank=400, intel_note="known"),
            mk_bp("Late Adp", "WR", adp=300.0, rank=1),
        ]
        covered, total, missing = coverage(_board(players), top_n=1)
        assert (covered, total) == (1, 1)
        assert missing == []

    def test_counts_players_without_intel_as_missing(self):
        players = [
            mk_bp("Has", "WR", adp=1.0, intel_note="n"),
            mk_bp("Lacks", "WR", adp=2.0),
        ]
        covered, total, missing = coverage(_board(players), top_n=10)
        assert (covered, total) == (1, 2)
        assert [bp.name for bp in missing] == ["Lacks"]

    def test_upside_alone_counts_as_covered(self):
        players = [mk_bp("Scored", "WR", adp=1.0, upside=70.0)]
        covered, _, missing = coverage(_board(players), top_n=10)
        assert covered == 1 and missing == []

    def test_players_without_adp_are_out_of_scope(self):
        players = [mk_bp("No Adp", "WR", adp=None)]
        assert coverage(_board(players), top_n=10) == (0, 0, [])


class TestRiskField:
    def test_risk_parses_and_merges(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "X Y":\n    risk: 60\n    note: "suspended"\n')
        entry = load_intel(path)["x y"]
        assert entry.risk == 60.0
        bp = mk_bp("X Y", "WR")
        merged = apply_intel(_board([bp]), {"x y": entry})
        assert merged.players[0].availability_risk == 60.0

    def test_risk_validation_mirrors_upside(self, tmp_path):
        for bad in ("140", '"high"', "-5"):
            path = _write(tmp_path, f'players:\n  "X":\n    risk: {bad}\n')
            with pytest.raises(IntelError):
                load_intel(path)

    def test_absent_risk_stays_none(self, tmp_path):
        path = _write(tmp_path, 'players:\n  "X Y": "just a note"\n')
        entry = load_intel(path)["x y"]
        assert entry.risk is None
        merged = apply_intel(_board([mk_bp("X Y", "WR")]), {"x y": entry})
        assert merged.players[0].availability_risk is None


class TestDiffIntel:
    def test_added_removed_changed(self):
        from ffbot.intel import diff_intel

        old = {
            "gone guy": IntelEntry("Gone Guy", note="was here"),
            "same guy": IntelEntry("Same Guy", upside=60.0, note="steady"),
            "moved guy": IntelEntry("Moved Guy", upside=50.0, risk=None, note="x"),
        }
        new = {
            "same guy": IntelEntry("Same Guy", upside=60.0, note="steady"),
            "moved guy": IntelEntry("Moved Guy", upside=70.0, risk=40.0, note="x"),
            "new guy": IntelEntry("New Guy", upside=80.0, note="fresh"),
        }
        d = diff_intel(old, new)
        assert d["added"] == ["New Guy (upside 80)"]
        assert d["removed"] == ["Gone Guy"]
        assert len(d["changed"]) == 1
        assert "upside 50 -> 70" in d["changed"][0]
        assert "risk - -> 40" in d["changed"][0]
        assert not any("Same Guy" in line for line in d["changed"])

    def test_note_change_reported_without_quoting(self):
        # Notes churn every refresh; the diff says THAT they changed, not the
        # full text, or the report drowns in prose.
        from ffbot.intel import diff_intel

        old = {"g": IntelEntry("G", note="old words")}
        new = {"g": IntelEntry("G", note="new words")}
        assert diff_intel(old, new)["changed"] == ["G: note updated"]

    def test_identical_is_empty(self):
        from ffbot.intel import diff_intel

        entries = {"g": IntelEntry("G", upside=50.0, note="n")}
        assert diff_intel(entries, dict(entries)) == {"added": [], "removed": [], "changed": []}
