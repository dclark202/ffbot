from __future__ import annotations

import itertools
import random
from collections import defaultdict

import pytest

from ffbot.board import (
    Board,
    BoardPlayer,
    _merge_csv_rows,
    assign_tiers,
    derive_replacement,
    export_rankings,
    read_fantasypros,
    to_player,
)
from ffbot.config import Config, DraftConfig
from ffbot.lineup import optimize
from ffbot.models import Player, slot_accepts, starting_slots

# --- CSV loading -------------------------------------------------------


class TestReadFantasyPros:
    def test_rankings_export_shape(self, tmp_path):
        p = tmp_path / "rankings.csv"
        p.write_text(
            "RK,TIERS,PLAYER NAME,TEAM,POS,BYE WEEK,SOS SEASON,ECR VS. ADP\n"
            "1,1,Justin Jefferson,MIN,WR1,13,3,0\n"
            "2,1,Ja'Marr Chase,CIN,WR2,12,2,1\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert len(rows) == 2
        assert rows[0]["name"] == "Justin Jefferson"
        assert rows[0]["team"] == "MIN"
        assert rows[0]["position"] == "WR"  # rank digit stripped
        assert rows[0]["bye"] == 13
        assert rows[0]["tier"] == 1
        assert rows[0]["rank"] == 1

    def test_adp_export_shape(self, tmp_path):
        p = tmp_path / "adp.csv"
        p.write_text(
            "Rank,Player,Team,Bye,POS,ESPN,Sleeper,NFL,RTSports,FFC,AVG\n"
            "1,Justin Jefferson,MIN,13,WR1,1,2,1,1,1,1.2\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert rows[0]["name"] == "Justin Jefferson"
        assert rows[0]["adp"] == 1.2
        assert rows[0]["bye"] == 13

    def test_projections_export_shape(self, tmp_path):
        p = tmp_path / "proj.csv"
        p.write_text(
            "Player,Team,POS,ATT,YDS,TD,FPTS\n"
            "Christian McCaffrey,SF,RB1,300,1400,12,344.5\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert rows[0]["name"] == "Christian McCaffrey"
        assert rows[0]["points"] == 344.5
        assert rows[0]["position"] == "RB"

    def test_utf8_bom_handled(self, tmp_path):
        p = tmp_path / "bom.csv"
        p.write_bytes(
            b"\xef\xbb\xbfPlayer,Team,POS,FPTS\nJustin Jefferson,MIN,WR,320.0\n"
        )
        rows = read_fantasypros(p)
        assert len(rows) == 1
        assert rows[0]["name"] == "Justin Jefferson"
        # BOM must not leak into the first header/field.
        assert "﻿" not in "".join(str(v) for v in rows[0].values())

    def test_thousands_separator_parsed(self, tmp_path):
        p = tmp_path / "big.csv"
        p.write_text('Player,Team,POS,FPTS\nSome Guy,KC,QB,"1,234.5"\n', encoding="utf-8")
        rows = read_fantasypros(p)
        assert rows[0]["points"] == 1234.5

    def test_missing_bye_is_none(self, tmp_path):
        p = tmp_path / "nobyes.csv"
        p.write_text("Player,Team,POS,BYE,FPTS\nSome Guy,KC,QB,,300.0\n", encoding="utf-8")
        rows = read_fantasypros(p)
        assert rows[0]["bye"] is None

    def test_missing_adp_column_does_not_raise(self, tmp_path):
        p = tmp_path / "noadp.csv"
        p.write_text("Player,Team,POS,FPTS\nSome Guy,KC,QB,300.0\n", encoding="utf-8")
        rows = read_fantasypros(p)
        assert rows[0].get("adp") is None

    def test_unrecognized_header_ignored(self, tmp_path):
        p = tmp_path / "extra.csv"
        p.write_text(
            "Player,Team,POS,FPTS,SomeWeirdColumn\nSome Guy,KC,QB,300.0,xyz\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert "someweirdcolumn" not in rows[0]
        assert rows[0]["points"] == 300.0

    def test_row_without_name_dropped(self, tmp_path):
        p = tmp_path / "blank.csv"
        p.write_text("Player,Team,POS,FPTS\n,KC,QB,300.0\n", encoding="utf-8")
        rows = read_fantasypros(p)
        assert rows == []


class TestMergeCsvRows:
    def test_fills_gaps_across_sources(self):
        projections = [{"name": "Justin Jefferson", "position": "WR", "points": 320.0}]
        adp = [{"name": "Justin Jefferson", "position": "WR", "adp": 4.2, "adp_stdev": 1.1}]
        merged = _merge_csv_rows([projections, adp])
        assert len(merged) == 1
        assert merged[0]["points"] == 320.0
        assert merged[0]["adp"] == 4.2

    def test_first_source_wins_on_conflict(self):
        a = [{"name": "Justin Jefferson", "position": "WR", "points": 320.0}]
        b = [{"name": "Justin Jefferson", "position": "WR", "points": 999.0}]
        merged = _merge_csv_rows([a, b])
        assert merged[0]["points"] == 320.0

    def test_distinct_position_not_merged(self):
        a = [{"name": "Michael Thomas", "position": "WR", "points": 200.0}]
        b = [{"name": "Michael Thomas", "position": "TE", "points": 50.0}]
        merged = _merge_csv_rows([a, b])
        assert len(merged) == 2


# --- Replacement derivation ------------------------------------------------


def _brute_force_position_counts(players: list[Player], slots: list[str]) -> dict[str, int]:
    """Exhaustively find the position counts of the highest-scoring legal
    assignment. Only for tiny inputs — mirrors
    tests/test_lineup.py::TestOptimalityAgainstBruteForce._brute_force_best.
    """
    n = len(slots)
    padded = list(players) + [None] * n
    best_score = -1.0
    best_positions: list[str] = []
    for combo in itertools.permutations(padded, n):
        total = 0.0
        ok = True
        positions: list[str] = []
        for slot, p in zip(slots, combo):
            if p is None:
                continue
            if not slot_accepts(slot, p):
                ok = False
                break
            total += p.projected_points
            positions.append(p.eligible_positions[0])
        if ok and total > best_score:
            best_score = total
            best_positions = positions
    counts: dict[str, int] = defaultdict(int)
    for pos in best_positions:
        counts[pos] += 1
    return dict(counts)


def _rows(players: list[Player]) -> list[dict]:
    return [
        {"name": p.name, "position": p.eligible_positions[0], "points": p.projected_points}
        for p in players
    ]


class TestDeriveReplacementBruteForce:
    def test_flex_split_matches_exhaustive_search(self, cfg):
        # Ambiguous flex demand: two teams, one RB slot, one WR slot, one
        # flex slot each -- the flex split isn't derivable by inspection.
        layout = {"RB": 1, "WR": 1, "W/R/T": 1}
        num_teams = 2
        rng = random.Random(4)
        players = [
            Player(i + 1, f"RB{i}", ["RB"], projected_points=round(rng.uniform(5, 30), 1))
            for i in range(3)
        ] + [
            Player(i + 10, f"WR{i}", ["WR"], projected_points=round(rng.uniform(5, 30), 1))
            for i in range(3)
        ]
        rows = _rows(players)
        starters, _replacement = derive_replacement(rows, layout, num_teams, cfg)

        scaled = {slot: count * num_teams for slot, count in layout.items()}
        slots = starting_slots(scaled)
        want = _brute_force_position_counts(players, slots)

        for pos in ("RB", "WR"):
            assert starters.get(pos, 0) == want.get(pos, 0)

    def test_scaled_layout_fills_all_slots_when_players_sufficient(self, cfg):
        # Every slot in the T-scaled layout should end up filled when the
        # pool has comfortably more players than slots at each position --
        # a basic coverage sanity check on the aggregate optimum.
        layout = {"RB": 1, "WR": 1, "W/R/T": 1}
        num_teams = 3
        rng = random.Random(9)
        players = [
            Player(i + 1, f"RB{i}", ["RB"], projected_points=round(rng.uniform(5, 30), 1))
            for i in range(12)
        ] + [
            Player(i + 100, f"WR{i}", ["WR"], projected_points=round(rng.uniform(5, 30), 1))
            for i in range(12)
        ]
        rows = _rows(players)
        starters, _replacement = derive_replacement(rows, layout, num_teams, cfg)

        scaled = {slot: count * num_teams for slot, count in layout.items()}
        total_slots = len(starting_slots(scaled))
        assert sum(starters.values()) == total_slots


class TestDeriveReplacementTruncationExact:
    def test_truncated_matches_untruncated(self, cfg):
        layout = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1}
        num_teams = 8
        rng = random.Random(20260808)
        pos_pool = (["QB"] * 15 + ["RB"] * 45 + ["WR"] * 55 + ["TE"] * 20 + ["K"] * 12 + ["DEF"] * 12)

        for trial in range(6):
            rng.shuffle(pos_pool)
            players = [
                Player(
                    i + 1,
                    f"P{trial}_{i}",
                    [pos_pool[i]],
                    projected_points=round(rng.uniform(10, 340), 1),
                )
                for i in range(len(pos_pool))
            ]
            rows = _rows(players)

            starters_truncated, _ = derive_replacement(rows, layout, num_teams, cfg)

            scaled = {slot: count * num_teams for slot, count in layout.items()}
            plan = optimize(players, scaled, None, cfg)
            counts: dict[str, int] = defaultdict(int)
            for _slot, p in plan.assignments:
                counts[p.eligible_positions[0]] += 1

            for pos in starters_truncated:
                assert starters_truncated[pos] == counts.get(pos, 0), (
                    f"trial {trial} position {pos}: "
                    f"truncated={starters_truncated[pos]} untruncated={counts.get(pos, 0)}"
                )


class TestSuperflex:
    def test_superflex_increases_qb_starters(self, cfg):
        # Non-overlapping ranges make the flex slot's winner deterministic:
        # every QB outscores every WR, so the extra Q/W/R/T slot must go to
        # the QB pool's second-best player, not whichever position happens
        # to win a coinflip.
        rng = random.Random(3)
        players = [
            Player(i + 1, f"QB{i}", ["QB"], projected_points=round(rng.uniform(280, 320), 1))
            for i in range(6)
        ] + [
            Player(i + 10, f"WR{i}", ["WR"], projected_points=round(rng.uniform(150, 200), 1))
            for i in range(6)
        ]
        rows = _rows(players)

        standard = {"QB": 1, "WR": 1}
        superflex = {"QB": 1, "WR": 1, "Q/W/R/T": 1}

        starters_std, _ = derive_replacement(rows, standard, 1, cfg)
        starters_sf, _ = derive_replacement(rows, superflex, 1, cfg)

        assert starters_sf["QB"] > starters_std["QB"]


# --- Tiering ---------------------------------------------------------------


class TestAssignTiers:
    def test_same_input_same_tiers(self, cfg):
        rows = [
            {"name": "A", "position": "WR", "points": 300.0},
            {"name": "B", "position": "WR", "points": 295.0},
            {"name": "C", "position": "WR", "points": 200.0},
            {"name": "D", "position": "WR", "points": 195.0},
        ]
        t1 = assign_tiers(rows, cfg)
        t2 = assign_tiers(rows, cfg)
        assert t1 == t2

    def test_invariant_under_permutation(self, cfg):
        rows = [
            {"name": "A", "position": "WR", "points": 300.0},
            {"name": "B", "position": "WR", "points": 295.0},
            {"name": "C", "position": "WR", "points": 200.0},
            {"name": "D", "position": "WR", "points": 195.0},
        ]
        shuffled = list(reversed(rows))
        assert assign_tiers(rows, cfg) == assign_tiers(shuffled, cfg)

    def test_breaks_only_at_large_gap(self):
        cfg = Config(draft=DraftConfig(tier_gap_multiplier=2.0, tier_min_gap=1.0))
        rows = [
            {"name": "A", "position": "WR", "points": 100.0},
            {"name": "B", "position": "WR", "points": 98.0},   # gap 2, small
            {"name": "C", "position": "WR", "points": 96.0},   # gap 2, small
            {"name": "D", "position": "WR", "points": 50.0},   # gap 46, huge -> new tier
            {"name": "E", "position": "WR", "points": 48.0},   # gap 2, small
        ]
        tiers = assign_tiers(rows, cfg)
        assert tiers["a:WR"] == tiers["b:WR"] == tiers["c:WR"]
        assert tiers["d:WR"] == tiers["e:WR"]
        assert tiers["d:WR"] > tiers["c:WR"]

    def test_tiers_contiguous_in_rank_order(self, cfg):
        rng = random.Random(1)
        rows = [
            {"name": f"P{i}", "position": "WR", "points": round(rng.uniform(1, 300), 1)}
            for i in range(40)
        ]
        tiers = assign_tiers(rows, cfg)
        ordered = sorted(rows, key=lambda r: -r["points"])
        tier_sequence = [tiers[f"{r['name'].lower()}:WR"] for r in ordered]
        # Non-decreasing when walking players best-to-worst.
        assert tier_sequence == sorted(tier_sequence)


# --- Export ------------------------------------------------------------


def _bp(name, position, points, tier=1, rank=0):
    return BoardPlayer(
        key=f"{name.lower()}:{position}",
        name=name,
        position=position,
        team="",
        bye_week=None,
        points=points,
        adp=None,
        adp_stdev=None,
        yahoo_id=None,
        tier=tier,
        vor=points,
        rank=rank,
    )


class TestExportRankings:
    def test_kicker_deferred_past_threshold(self):
        cfg = Config(draft=DraftConfig(num_teams=2, rounds=4, export_defer_positions=["K", "DEF"]))
        # threshold = num_teams * (rounds - 2) = 2 * 2 = 4
        skill = [_bp(f"WR{i}", "WR", 100 - i) for i in range(6)]
        kickers = [_bp("Kicker1", "K", 50), _bp("Kicker2", "K", 45)]
        board = Board(players=skill[:1] + kickers + skill[1:], by_key={}, replacement={},
                       starters_per_pos={}, tier_last={})
        exported = export_rankings(board, cfg)
        names = [row["name"] for row in exported]
        kicker_positions = [i for i, n in enumerate(names) if n.startswith("Kicker")]
        assert all(i >= 4 for i in kicker_positions)

    def test_export_preserves_all_players(self):
        cfg = Config(draft=DraftConfig(num_teams=2, rounds=4))
        players = [_bp(f"P{i}", "WR", 100 - i) for i in range(5)] + [_bp("K1", "K", 40)]
        board = Board(players=players, by_key={}, replacement={}, starters_per_pos={}, tier_last={})
        exported = export_rankings(board, cfg)
        assert len(exported) == len(players)
        assert {row["name"] for row in exported} == {p.name for p in players}


class TestToPlayer:
    def test_maps_fields(self):
        bp = _bp("Justin Jefferson", "WR", 320.0)
        p = to_player(bp, uid=42)
        assert p.player_id == 42
        assert p.name == "Justin Jefferson"
        assert p.eligible_positions == ["WR"]
        assert p.projected_points == 320.0
        assert p.selected_position == "BN"
