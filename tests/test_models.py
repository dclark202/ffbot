from __future__ import annotations

from ffbot.models import bench_slots, ir_slots, roster_capacity, starting_slots


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
