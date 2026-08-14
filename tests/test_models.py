from __future__ import annotations

from ffbot.models import bench_slots, equivalent_slot, ir_slots, roster_capacity, starting_slots


def test_starting_slots_excludes_bench_and_ir():
    layout = {"QB": 1, "WR": 2, "BN": 5, "IR": 1}
    slots = starting_slots(layout)
    assert "BN" not in slots
    assert "IR" not in slots
    assert slots.count("QB") == 1
    assert slots.count("WR") == 2


def test_bench_slots():
    assert bench_slots({"QB": 1, "BN": 5}) == 5
    assert bench_slots({"QB": 1}) == 0


def test_ir_slots_sums_every_ir_variant():
    assert ir_slots({"IR": 1, "IR+": 2, "IR-R": 1}) == 4
    assert ir_slots({"QB": 1}) == 0


def test_roster_capacity_is_starters_plus_bench_excluding_ir():
    layout = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 5, "IR": 1}
    # 9 starters + 5 bench = 14, IR excluded.
    assert roster_capacity(layout) == 14


def test_roster_capacity_ignores_ir_size_entirely():
    small_ir = {"QB": 1, "BN": 2, "IR": 1}
    large_ir = {"QB": 1, "BN": 2, "IR": 10}
    assert roster_capacity(small_ir) == roster_capacity(large_ir) == 3


class TestEquivalentSlot:
    def test_already_a_layout_slot_is_identity(self):
        layout = {"QB": 1, "W/R/T": 1, "BN": 5}
        assert equivalent_slot("QB", layout) == "QB"
        assert equivalent_slot("W/R/T", layout) == "W/R/T"

    def test_bench_and_ir_names_pass_through_even_if_absent_from_layout(self):
        layout = {"QB": 1, "BN": 5, "IR": 1}
        assert equivalent_slot("BN", layout) == "BN"
        assert equivalent_slot("IR", layout) == "IR"
        assert equivalent_slot("TAXI", {"QB": 1}) == "TAXI"

    def test_sleeper_flex_maps_onto_yahoo_spelling(self):
        layout = {"QB": 1, "W/R/T": 1, "BN": 5}
        assert equivalent_slot("FLEX", layout) == "W/R/T"

    def test_sleeper_superflex_maps_onto_yahoo_spelling(self):
        layout = {"QB": 1, "Q/W/R/T": 1, "BN": 5}
        assert equivalent_slot("SUPER_FLEX", layout) == "Q/W/R/T"

    def test_identity_when_layout_already_uses_sleeper_spelling(self):
        layout = {"QB": 1, "FLEX": 1, "BN": 5}
        assert equivalent_slot("FLEX", layout) == "FLEX"

    def test_unknown_slot_passes_through(self):
        layout = {"QB": 1, "W/R/T": 1, "BN": 5}
        assert equivalent_slot("MYSTERY", layout) == "MYSTERY"

    def test_no_matching_layout_slot_passes_through(self):
        # FLEX (WR/RB/TE) has no equivalent in a layout with no flex at all.
        layout = {"QB": 1, "WR": 2, "RB": 2, "BN": 5}
        assert equivalent_slot("FLEX", layout) == "FLEX"
