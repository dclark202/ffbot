from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board
from ffbot.config import Config, DraftConfig
from ffbot.draft import DraftState
from scripts.mock_draft import _bot_pick, _bot_view

LAYOUT = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "BN": 3}


@pytest.fixture
def board(tmp_path):
    cfg = Config(roster_positions=LAYOUT, draft=DraftConfig(num_teams=4))
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    for pos, base in (("QB", 300.0), ("RB", 290.0), ("WR", 285.0), ("TE", 230.0)):
        for i in range(30):
            lines.append(f"{pos}{i},XXX,{pos},7,{base - 4.0 * i},{i * 4 + 1}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return load_board([path], LAYOUT, 4, cfg), cfg


class TestBotView:
    """A bot must evaluate candidates against ITS OWN roster.

    `ffbot/backtest/draft_sim.py` documents what happens otherwise: run
    against the agent's roster, `recommend()` applies the agent's caps and
    forced-fill to every opponent, and they all end up hoarding defenses.
    """

    def test_reflags_ownership_to_the_given_seat(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        for bp in b.players[:4]:  # picks 1-4, one per seat
            state.record(bp.key, mine=None)

        view = _bot_view(state, 3)
        assert view.my_slot == 3
        # Exactly the pick belonging to seat 3 is flagged mine.
        assert [p.number for p in view.picks if p.mine] == [3]

    def test_each_seat_sees_a_different_roster(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        for bp in b.players[:8]:
            state.record(bp.key, mine=None)

        rosters = {slot: {p.key for p in _bot_view(state, slot).my_roster()} for slot in (1, 2, 3, 4)}
        assert all(rosters.values()), "every seat should own picks after two rounds"
        seen: set[str] = set()
        for keys in rosters.values():
            assert not (keys & seen), "a player was attributed to two seats"
            seen |= keys

    def test_does_not_mutate_the_real_state(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        for bp in b.players[:4]:
            state.record(bp.key, mine=None)
        before = [(p.number, p.mine) for p in state.picks]
        _bot_view(state, 3)
        assert [(p.number, p.mine) for p in state.picks] == before
        assert state.my_slot == 1


class TestBotPick:
    def test_returns_an_available_player(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        key = _bot_pick(state, cfg, 2, random.Random(0), window=3)
        assert key in b.by_key
        assert key not in state.taken_keys()

    def test_never_repeats_a_taken_player_across_a_full_draft(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        rng = random.Random(7)
        for pick in range(1, 4 * 4 + 1):
            slot = (pick - 1) % 4 + 1
            key = _bot_pick(state, cfg, slot, rng, window=3)
            assert key not in state.taken_keys()
            state.record(key, mine=False)
        assert len(state.picks) == 16

    def test_window_of_one_is_deterministic(self, board):
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        a = _bot_pick(state, cfg, 2, random.Random(1), window=1)
        c = _bot_pick(state, cfg, 2, random.Random(999), window=1)
        assert a == c

    def test_a_wider_window_explores(self, board):
        # Runs must not all replay one scripted draft, or extra mocks add
        # nothing over a single one.
        b, cfg = board
        state = DraftState(board=b, num_teams=4, my_slot=1, rounds=4, roster_positions=LAYOUT)
        picks = {_bot_pick(state, cfg, 2, random.Random(s), window=5) for s in range(25)}
        assert len(picks) > 1
