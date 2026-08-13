from __future__ import annotations

from ffbot.models import SLOT_ELIGIBILITY, STATUS_IR_ELIGIBLE, STATUS_OUT, STATUS_QUESTIONABLE
from ffbot.sleeper.models import is_defense_id, normalize_injury_status


class TestSleeperSlotsRegisteredInModels:
    """Sleeper's flex names are added directly to models.SLOT_ELIGIBILITY
    (see that module) rather than translated at this boundary -- these tests
    guard that they're actually present, since ffbot/sleeper never mutates
    that dict itself."""

    def test_flex_variants_present(self):
        assert SLOT_ELIGIBILITY["FLEX"] == frozenset({"WR", "RB", "TE"})
        assert SLOT_ELIGIBILITY["SUPER_FLEX"] == frozenset({"QB", "WR", "RB", "TE"})
        assert SLOT_ELIGIBILITY["WRRB_FLEX"] == frozenset({"WR", "RB"})
        assert SLOT_ELIGIBILITY["REC_FLEX"] == frozenset({"WR", "TE"})

    def test_yahoo_variants_still_present(self):
        # Additive, not a replacement -- demo/2025/ and every existing
        # fixture depend on this.
        assert "W/R/T" in SLOT_ELIGIBILITY
        assert "Q/W/R/T" in SLOT_ELIGIBILITY


class TestNormalizeInjuryStatus:
    def test_empty_and_none_are_healthy(self):
        assert normalize_injury_status(None) == ""
        assert normalize_injury_status("") == ""

    def test_questionable(self):
        assert normalize_injury_status("Questionable") == "Q"
        assert "Q" in STATUS_QUESTIONABLE

    def test_doubtful(self):
        assert normalize_injury_status("Doubtful") == "D"
        assert "D" in STATUS_OUT

    def test_out(self):
        assert normalize_injury_status("Out") == "O"
        assert "O" in STATUS_OUT

    def test_ir(self):
        assert normalize_injury_status("IR") == "IR"
        assert "IR" in STATUS_OUT
        assert "IR" in STATUS_IR_ELIGIBLE

    def test_pup(self):
        assert normalize_injury_status("PUP") == "PUP"

    def test_suspended_short_and_long_spelling(self):
        assert normalize_injury_status("Sus") == "SUSP"
        assert normalize_injury_status("Suspended") == "SUSP"

    def test_na(self):
        assert normalize_injury_status("NA") == "NA"

    def test_case_insensitive(self):
        assert normalize_injury_status("questionable") == "Q"
        assert normalize_injury_status("OUT") == "O"

    def test_dnr_maps_conservatively_to_doubtful(self):
        # A documented judgment call, not a verified fact -- see the
        # docstring in ffbot/sleeper/models.py for the reasoning.
        assert normalize_injury_status("DNR") == "D"

    def test_unrecognized_value_passes_through_rather_than_silently_healthy(self):
        assert normalize_injury_status("SomeNewStatus") == "SomeNewStatus"


class TestIsDefenseId:
    def test_team_abbreviation_is_a_defense(self):
        assert is_defense_id("KC") is True
        assert is_defense_id("SF") is True

    def test_numeric_id_is_not_a_defense(self):
        assert is_defense_id("4046") is False

    def test_lowercase_is_not_matched(self):
        # Real Sleeper defense ids are uppercase; a real player id is never
        # alphabetic at all, so this branch mostly guards against surprises.
        assert is_defense_id("kc") is False
