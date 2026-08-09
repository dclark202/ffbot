from __future__ import annotations

import itertools
import random
from collections import defaultdict

import pytest

from ffbot.board import (
    Board,
    BoardPlayer,
    _merge_csv_rows,
    apply_league_scoring,
    assign_tiers,
    derive_replacement,
    export_rankings,
    load_board,
    read_fantasypros,
    to_player,
)
from ffbot.config import Config, DraftConfig, LeagueScoring
from ffbot.lineup import optimize
from ffbot.scoring import StatLine
from tests.conftest import mk_bp
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
    return mk_bp(name, position, points, tier=tier, rank=rank)


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


# --- Positional stat reader ------------------------------------------------
#
# The real exports repeat header text within one file (rushing YDS/TDS, then
# receiving YDS/TDS) -- a name-keyed reader (csv.DictReader) silently
# collapses duplicate header names and keeps only the last column's value.
# These pin the positional (index-based) reader against exactly that shape.


class TestStatLayouts:
    def test_flex_layout_separates_rush_from_receiving(self, tmp_path):
        p = tmp_path / "flex.csv"
        p.write_text(
            "Player,Team,POS,ATT,YDS,TDS,REC,YDS,TDS,FL,FPTS\n"
            "Jahmyr Gibbs,DET,RB,274.4,1381.4,13.8,70.9,580.6,4.1,1.1,372.6\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        stats = rows[0]["stats"]
        assert stats.rush_yds == 1381.4
        assert stats.rush_td == 13.8
        assert stats.rec == 70.9
        assert stats.rec_yds == 580.6  # not collapsed onto rush_yds
        assert stats.rec_td == 4.1
        assert stats.fumbles_lost == 1.1

    def test_qb_layout_separates_pass_from_rush(self, tmp_path):
        p = tmp_path / "qb.csv"
        p.write_text(
            "Player,Team,ATT,CMP,YDS,TDS,INTS,ATT,YDS,TDS,FL,FPTS\n"
            "Josh Allen,BUF,491.6,333.0,3814.0,27.4,11.2,118.1,585.2,11.8,4.1,372.2\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p, default_position="QB")
        stats = rows[0]["stats"]
        assert stats.pass_att == 491.6
        assert stats.pass_yds == 3814.0
        assert stats.pass_td == 27.4
        assert stats.pass_int == 11.2
        assert stats.rush_att == 118.1
        assert stats.rush_yds == 585.2  # not collapsed onto pass_yds
        assert stats.rush_td == 11.8

    def test_k_layout(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text(
            "Player,Team,FG,FGA,XPT,FPTS\nBrandon Aubrey,DAL,35.2,39.9,47.0,152.5\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p, default_position="K")
        stats = rows[0]["stats"]
        assert stats.fg_made == 35.2
        assert stats.fg_att == 39.9
        assert stats.pat_made == 47.0

    def test_def_layout(self, tmp_path):
        p = tmp_path / "dst.csv"
        p.write_text(
            "Player,Team,SACK,INT,FR,FF,TD,SAFETY,PA,YDS_AGN,FPTS\n"
            "Houston Texans,,48.8,14.8,11.6,18.3,2.8,1.0,322.0,5061.6,120.4\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p, default_position="DEF")
        stats = rows[0]["stats"]
        assert stats.sack == 48.8
        assert stats.interception == 14.8
        assert stats.fumble_recovery == 11.6
        assert stats.def_td == 2.8
        assert stats.points_allowed_season == 322.0
        assert stats.yards_allowed_season == 5061.6

    def test_short_junk_row_skipped_without_crashing(self, tmp_path):
        p = tmp_path / "flex.csv"
        p.write_text(
            "Player,Team,POS,ATT,YDS,TDS,REC,YDS,TDS,FL,FPTS\n"
            ' , , ,,\n'  # the blank sub-header row FantasyPros ships
            "Jahmyr Gibbs,DET,RB,274.4,1381.4,13.8,70.9,580.6,4.1,1.1,372.6\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert len(rows) == 1
        assert rows[0]["name"] == "Jahmyr Gibbs"

    def test_reordered_header_does_not_match(self, tmp_path):
        # FGA before FG -- not the real export's order. Must not silently
        # misread FGA as fg_made.
        p = tmp_path / "k.csv"
        p.write_text(
            "Player,Team,FGA,FG,XPT,FPTS\nSome Kicker,DAL,39.9,35.2,47.0,152.5\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p, default_position="K")
        assert "stats" not in rows[0]
        assert rows[0]["points"] == 152.5  # FPTS path untouched

    def test_adp_export_has_no_stat_layout(self, tmp_path):
        p = tmp_path / "adp.csv"
        p.write_text(
            "Rank,Player,Team,Bye,POS,ESPN,Sleeper,NFL,RTSports,FFC,AVG\n"
            "1,Justin Jefferson,MIN,13,WR1,1,2,1,1,1,1.2\n",
            encoding="utf-8",
        )
        rows = read_fantasypros(p)
        assert "stats" not in rows[0]


# --- League scoring ---------------------------------------------------------


class TestApplyLeagueScoring:
    def test_none_league_is_exact_noop(self):
        rows = [{"name": "A", "position": "RB", "points": 100.0, "stats": None}]
        apply_league_scoring(rows, None)
        assert rows[0]["points"] == 100.0
        assert rows[0]["points_fp"] == 100.0
        assert rows[0]["points_source"] == "consensus"
        assert rows[0]["points_flags"] == ()

    def test_row_without_stats_keeps_consensus_points(self):
        league = LeagueScoring.from_dict({"passing": {"td": 6}})
        rows = [{"name": "A", "position": "QB", "points": 250.0, "stats": None}]
        apply_league_scoring(rows, league)
        assert rows[0]["points"] == 250.0
        assert rows[0]["points_source"] == "consensus"

    def test_row_with_stats_recomputed(self):
        league = LeagueScoring.from_dict({"defense": {
            "points_allowed": [{"max": 999, "points": -4}, {"max": 10, "points": 10}],
        }})
        stats = StatLine(
            sack=48.8, interception=14.8, fumble_recovery=11.6, def_td=2.8, safety=1.0,
            points_allowed_season=322.0,
        )
        rows = [{"name": "Houston Texans", "position": "DEF", "points": 120.4, "stats": stats}]
        apply_league_scoring(rows, league)
        assert rows[0]["points_source"] == "league"
        assert rows[0]["points_fp"] == 120.4
        # FantasyPros' own export scores points allowed at zero; this league
        # scores it, so the recomputed number must move.
        assert rows[0]["points"] != 120.4


class TestLoadBoardWithoutLeague:
    """cfg.league is None by default -- the board must be bit-identical to
    the board this codebase produced before league scoring existed."""

    def test_bit_identical_board(self, tmp_path):
        p = tmp_path / "flex.csv"
        p.write_text(
            "Player,Team,POS,ATT,YDS,TDS,REC,YDS,TDS,FL,FPTS\n"
            "Jahmyr Gibbs,DET,RB,274.4,1381.4,13.8,70.9,580.6,4.1,1.1,372.6\n"
            "Bijan Robinson,ATL,RB,270.0,1200.0,10.0,50.0,400.0,2.0,1.0,300.0\n",
            encoding="utf-8",
        )
        cfg = Config(roster_positions={"RB": 1, "BN": 1})
        board = load_board([str(p)], cfg.roster_positions, num_teams=1, cfg=cfg)
        gibbs = board.by_key["jahmyr gibbs:RB"]
        assert gibbs.points == 372.6
        assert gibbs.points_fp == 372.6
        assert gibbs.points_source == "consensus"
        assert gibbs.points_flags == ()


class TestLoadBoardWithLeague:
    def test_reception_zero_reorders_rb_vs_wr(self, tmp_path):
        p = tmp_path / "flex.csv"
        p.write_text(
            "Player,Team,POS,ATT,YDS,TDS,REC,YDS,TDS,FL,FPTS\n"
            # A receiving-heavy WR that only leads under PPR.
            "Slot Guy,KC,WR,0,0,0,100,900,6,0,190.0\n"
            # A rushing-heavy RB with fewer catches.
            "Grinder,SF,RB,300,1300,10,20,150,1,0,185.0\n",
            encoding="utf-8",
        )
        cfg = Config(roster_positions={"W/R/T": 2, "BN": 1})
        cfg.league = LeagueScoring.from_dict({"receiving": {"reception": 0.0}})
        board = load_board([str(p)], cfg.roster_positions, num_teams=1, cfg=cfg)
        slot_guy = board.by_key["slot guy:WR"]
        grinder = board.by_key["grinder:RB"]
        # Under standard (no PPR), Grinder's rush/rec yardage+TD haul out-scores
        # Slot Guy's now-unpaid 100 receptions -- the ordering flips.
        assert grinder.points > slot_guy.points
        assert slot_guy.points_source == "league"
        assert slot_guy.points_flags == ()

    def test_scoring_summary_and_residual(self, tmp_path):
        p = tmp_path / "dst.csv"
        p.write_text(
            "Player,Team,SACK,INT,FR,FF,TD,SAFETY,PA,YDS_AGN,FPTS\n"
            "Houston Texans,,48.8,14.8,11.6,18.3,2.8,1.0,322.0,5061.6,120.4\n",
            encoding="utf-8",
        )
        cfg = Config(roster_positions={"DEF": 1, "BN": 1})
        cfg.league = LeagueScoring.from_dict({"defense": {
            "points_allowed": [
                {"max": 0, "points": 10}, {"max": 6, "points": 7}, {"max": 13, "points": 4},
                {"max": 20, "points": 1}, {"max": 27, "points": 0}, {"max": 34, "points": -1},
                {"max": 999, "points": -4},
            ],
        }})
        board = load_board([{"path": str(p), "position": "DEF"}], cfg.roster_positions, num_teams=1, cfg=cfg)
        summary = board.scoring_summary()
        assert summary["DEF"]["league"] == 1
        assert summary["DEF"]["consensus"] == 0
        # Small residual: this stat line, scored under FantasyPros' own
        # default rules, should reproduce FantasyPros' own FPTS closely.
        assert board.scoring_residual["DEF"] < 1.5
