from __future__ import annotations

from dataclasses import dataclass

from ffbot.names import (
    defense_key,
    initial_key,
    match_board_to_yahoo,
    normalize_name,
    normalize_position,
    search,
)


class TestNormalizeName:
    def test_suffix_variants_match(self):
        assert normalize_name("Kenneth Walker III") == normalize_name("Kenneth Walker")
        assert normalize_name("Marvin Harrison Jr.") == normalize_name("Marvin Harrison Jr")
        assert normalize_name("Michael Pittman Jr.") == normalize_name("Michael Pittman")
        assert normalize_name("Travis Etienne Jr.") == normalize_name("Travis Etienne")
        assert normalize_name("Deebo Samuel Sr.") == normalize_name("Deebo Samuel")

    def test_punctuation_and_diacritics(self):
        assert normalize_name("Amon-Ra St. Brown") == "amonra st brown"
        assert normalize_name("D'Andre Swift") == normalize_name("DAndre Swift")
        assert normalize_name("J.K. Dobbins") == "jk dobbins"

    def test_bare_v_not_stripped(self):
        # "V" alone must not be treated as a generational suffix.
        assert normalize_name("Devante Parker V") != normalize_name("Devante Parker")

    def test_whitespace_collapsed(self):
        assert normalize_name("  Justin   Jefferson ") == "justin jefferson"

    def test_case_insensitive(self):
        assert normalize_name("JUSTIN JEFFERSON") == normalize_name("justin jefferson")


class TestNormalizePosition:
    def test_defense_aliases(self):
        assert normalize_position("DST") == "DEF"
        assert normalize_position("D/ST") == "DEF"
        assert normalize_position("D-ST") == "DEF"

    def test_rank_digit_stripped(self):
        assert normalize_position("RB1") == "RB"
        assert normalize_position("WR2") == "WR"

    def test_kicker_alias(self):
        assert normalize_position("PK") == "K"


class TestDefenseKey:
    def test_ravens_dst_to_baltimore(self):
        assert defense_key("Ravens D/ST", None) == "BAL"

    def test_commanders_to_washington(self):
        assert defense_key("Commanders D/ST", None) == "WAS"

    def test_la_rams_variants(self):
        assert defense_key("LA Rams", None) == "LAR"
        assert defense_key("Los Angeles Rams", None) == "LAR"

    def test_team_field_preferred(self):
        assert defense_key("Baltimore", "Ravens") == "BAL"

    def test_unknown_returns_none(self):
        assert defense_key("Not A Team", None) is None


class TestInitialKey:
    def test_basic(self):
        assert initial_key("justin jefferson") == "j jefferson"

    def test_single_token_passthrough(self):
        assert initial_key("baltimore") == "baltimore"


@dataclass
class _BoardRow:
    name: str
    position: str
    team: str = ""


def _yahoo(player_id, name, position, team=""):
    return {"player_id": player_id, "name": name, "position": position, "team": team}


class TestMatchBoardToYahoo:
    def test_exact_name_position_team(self):
        board = [{"name": "Justin Jefferson", "position": "WR", "team": "MIN"}]
        yahoo = [_yahoo(1, "Justin Jefferson", "WR", "MIN")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id == 1
        assert result.confidence == "exact"

    def test_suffix_mismatch_resolves_by_position(self):
        board = [{"name": "Kenneth Walker III", "position": "RB", "team": "SEA"}]
        yahoo = [_yahoo(2, "Kenneth Walker", "RB", "SEA")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id == 2

    def test_defense_naming_resolves(self):
        board = [{"name": "Ravens D/ST", "position": "DEF", "team": ""}]
        yahoo = [_yahoo(3, "Baltimore", "DEF", "Ravens")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id == 3

    def test_initial_expansion_josh_joshua(self):
        board = [{"name": "Josh Palmer", "position": "WR", "team": "LAC"}]
        yahoo = [_yahoo(4, "Joshua Palmer", "WR", "LAC")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id == 4
        assert result.confidence == "initial"

    def test_initial_expansion_gabe_gabriel(self):
        board = [{"name": "Gabe Davis", "position": "WR", "team": "BUF"}]
        yahoo = [_yahoo(5, "Gabriel Davis", "WR", "BUF")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id == 5

    def test_hollywood_brown_does_not_silently_match_marquise(self):
        # "Hollywood" is a nickname with zero string overlap with "Marquise" —
        # this must NOT produce a match at all, let alone a wrong one.
        board = [{"name": "Hollywood Brown", "position": "WR", "team": "KC"}]
        yahoo = [_yahoo(6, "Marquise Brown", "WR", "KC")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id is None
        assert result.confidence == "none"

    def test_alias_resolves_hollywood_brown(self):
        board = [{"name": "Hollywood Brown", "position": "WR", "team": "KC"}]
        yahoo = [_yahoo(6, "Marquise Brown", "WR", "KC")]
        aliases = {normalize_name("Hollywood Brown"): "Marquise Brown"}
        [result] = match_board_to_yahoo(board, yahoo, aliases=aliases)
        assert result.matched_id == 6
        assert result.confidence == "exact"

    def test_two_michael_thomas_different_positions_both_resolve(self):
        board = [
            {"name": "Michael Thomas", "position": "WR", "team": "NO"},
            {"name": "Michael Thomas", "position": "CB", "team": "HOU"},
        ]
        yahoo = [
            _yahoo(7, "Michael Thomas", "WR", "NO"),
            _yahoo(8, "Michael Thomas", "CB", "HOU"),
        ]
        results = match_board_to_yahoo(board, yahoo)
        assert results[0].matched_id == 7
        assert results[1].matched_id == 8

    def test_similar_names_under_fuzzy_threshold_do_not_match(self):
        board = [{"name": "Justin Jackson", "position": "RB", "team": "LAC"}]
        yahoo = [_yahoo(9, "Justin Jefferson", "WR", "MIN")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id is None

    def test_unmatched_reports_alternatives(self):
        board = [{"name": "Totally Unknown Player", "position": "WR", "team": "ZZZ"}]
        yahoo = [_yahoo(10, "Someone Else", "WR", "ZZZ")]
        [result] = match_board_to_yahoo(board, yahoo)
        assert result.matched_id is None
        assert result.confidence == "none"


class _Entry:
    def __init__(self, name: str):
        self.name = name
        self.key = normalize_name(name)


class TestSearch:
    def test_initials_find_player(self):
        entries = [_Entry("Justin Jefferson"), _Entry("Ja'Marr Chase")]
        results = search("jj", entries)
        # "jj" matches Jefferson's first-initial+last-name key ("j jefferson")
        # but has no analogous hit against "Ja'Marr Chase".
        assert [e.name for e in results] == ["Justin Jefferson"]

    def test_ceedee_matches_lamb(self):
        entries = [_Entry("CeeDee Lamb"), _Entry("Davante Adams")]
        results = search("ceedee", entries)
        assert results[0].name == "CeeDee Lamb"

    def test_single_letter_prefers_highest_rank_input_order(self):
        # Caller passes entries pre-sorted by board rank; ties break on that
        # order, so the highest-ranked "J" player surfaces first.
        entries = [_Entry("Jerry Jeudy"), _Entry("Justin Jefferson")]
        results = search("j", entries)
        assert results[0].name == "Jerry Jeudy"

    def test_no_match_returns_empty(self):
        entries = [_Entry("Justin Jefferson")]
        assert search("zzz", entries) == []

    def test_last_name_prefix_matches(self):
        entries = [_Entry("Justin Jefferson")]
        results = search("jeff", entries)
        assert results and results[0].name == "Justin Jefferson"


class _CompositeKeyEntry:
    """Stands in for board.BoardPlayer, whose .key is 'name:POSITION' — not
    a clean normalized name. Search must score off .name, not .key."""

    def __init__(self, name: str, position: str):
        self.name = name
        self.key = f"{normalize_name(name)}:{position}"


class TestSearchAgainstCompositeKey:
    def test_last_name_prefix_matches_despite_position_suffix(self):
        entries = [_CompositeKeyEntry("Justin Jefferson", "WR")]
        results = search("jeff", entries)
        assert results and results[0].name == "Justin Jefferson"

    def test_initials_unaffected_by_position_suffix(self):
        entries = [
            _CompositeKeyEntry("Justin Jefferson", "WR"),
            _CompositeKeyEntry("Ja'Marr Chase", "WR"),
        ]
        results = search("jj", entries)
        assert [e.name for e in results] == ["Justin Jefferson"]
