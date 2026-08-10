from __future__ import annotations

import dataclasses
import random

import pytest

from ffbot.backtest.draft_sim import DraftResult, _adp_order, agent_roster, simulate_draft
from ffbot.board import Board, BoardPlayer, _board_key, assign_tiers, derive_replacement
from ffbot.config import Config, DraftConfig

ROSTER_POSITIONS = {"QB": 1, "WR": 1, "RB": 1, "BN": 1}
_POSITIONS = {"QB": 5, "WR": 5, "RB": 5}


def _cfg() -> Config:
    cfg = Config(roster_positions=ROSTER_POSITIONS)
    cfg.draft = DraftConfig(num_teams=3, rounds=4)
    return cfg


def _board(cfg: Config) -> Board:
    rows = []
    for pos, n in _POSITIONS.items():
        for i in range(n):
            rows.append({
                "name": f"{pos} Player {i}", "position": pos, "team": "MIA",
                "points": 200.0 - i * 10.0, "adp": float(1 + i * 5), "adp_stdev": 2.0,
            })
    # An ADP-arbitrage plant: the single best QB by points, but with an ADP
    # deep enough that a pure-ADP draft (12 total picks here) never reaches
    # them, while a VOR/need-aware `recommend()` should still value them
    # highly and grab them early -- this is what gives the two draft modes
    # below a real reason to diverge, not just coincidence.
    rows[0]["adp"] = 500.0
    rows[0]["adp_stdev"] = 50.0
    starters_per_pos, replacement = derive_replacement(rows, ROSTER_POSITIONS, cfg.draft.num_teams, cfg)
    tiers = assign_tiers(rows, cfg)
    players = []
    for row in rows:
        key = _board_key(row["name"], row["position"])
        repl = replacement.get(row["position"], row["points"])
        players.append(BoardPlayer(
            key=key, name=row["name"], position=row["position"], team=row["team"],
            bye_week=None, points=row["points"], adp=row["adp"], adp_stdev=row["adp_stdev"],
            adp_spread=None, yahoo_id=None, tier=tiers.get(key, 1), vor=row["points"] - repl,
            rank=0, points_fp=row["points"], points_source="consensus",
        ))
    players.sort(key=lambda bp: -bp.vor)
    players = [dataclasses.replace(bp, rank=i + 1) for i, bp in enumerate(players)]
    return Board(players=players, by_key={bp.key: bp for bp in players}, replacement=replacement, starters_per_pos=starters_per_pos, tier_last={})


class TestAdpOrder:
    def test_no_jitter_matches_adp_exactly(self):
        cfg = _cfg()
        board = _board(cfg)
        order = _adp_order(board, random.Random(1), adp_noise=0.0)
        by_adp = sorted(board.players, key=lambda bp: bp.adp)
        assert order == [bp.key for bp in by_adp]

    def test_players_without_adp_are_excluded(self):
        cfg = _cfg()
        board = _board(cfg)
        no_adp = dataclasses.replace(board.players[0], adp=None)
        board.players[0] = no_adp
        order = _adp_order(board, random.Random(1), adp_noise=0.0)
        assert no_adp.key not in order

    def test_deterministic_given_seed(self):
        cfg = _cfg()
        board = _board(cfg)
        o1 = _adp_order(board, random.Random(5), adp_noise=1.0)
        o2 = _adp_order(board, random.Random(5), adp_noise=1.0)
        assert o1 == o2

    def test_jitter_can_reorder_close_adps(self):
        cfg = _cfg()
        board = _board(cfg)
        no_jitter = _adp_order(board, random.Random(1), adp_noise=0.0)
        jittered = _adp_order(board, random.Random(1), adp_noise=5.0)
        assert no_jitter != jittered


class TestSimulateDraft:
    def test_every_pick_made_exactly_once(self):
        cfg = _cfg()
        board = _board(cfg)
        result = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=2, seed=11)
        all_keys = [k for roster in result.rosters for k in roster]
        assert len(all_keys) == 12  # 3 teams x 4 rounds
        assert len(set(all_keys)) == 12  # no duplicates

    def test_every_roster_reaches_the_configured_size(self):
        cfg = _cfg()
        board = _board(cfg)
        result = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=1, seed=11)
        assert [len(r) for r in result.rosters] == [4, 4, 4]

    def test_agent_roster_matches_the_slots_own_list(self):
        cfg = _cfg()
        board = _board(cfg)
        result = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=3, seed=11)
        assert agent_roster(result, 3) == result.rosters[2]

    def test_recommend_and_adp_can_draft_different_rosters_same_seed(self):
        cfg = _cfg()
        board = _board(cfg)
        via_recommend = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=2, seed=11, agent_uses_recommend=True)
        via_adp = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=2, seed=11, agent_uses_recommend=False)
        assert set(agent_roster(via_recommend, 2)) != set(agent_roster(via_adp, 2))

    def test_adp_only_mode_is_symmetric_across_slots(self):
        # With agent_uses_recommend=False, the agent slot has no special
        # treatment at all -- every slot drafts by the identical process.
        cfg = _cfg()
        board = _board(cfg)
        result = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=1, seed=11, agent_uses_recommend=False)
        result_slot2 = simulate_draft(board, cfg, num_teams=3, rounds=4, agent_slot=2, seed=11, agent_uses_recommend=False)
        # Changing WHICH slot is "the agent" changes nothing about the pure-
        # ADP draft outcome itself (same seed, same picks, just relabeled).
        assert result.rosters == result_slot2.rosters

    def test_zero_rounds_produces_empty_rosters_not_a_crash(self):
        cfg = _cfg()
        board = _board(cfg)
        result = simulate_draft(board, cfg, num_teams=3, rounds=0, agent_slot=1, seed=11)
        assert result.rosters == [[], [], []]
