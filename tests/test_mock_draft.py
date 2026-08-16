from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board
from ffbot.config import Config, DraftConfig
from ffbot.draft import DraftState
from ffbot.draft_ui import UiState
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


class TestGuiMockDriver:
    """`--mock` swaps the Sleeper feed for local bots. The GUI needs no new
    UI for it: the Draft Log and Opponents panels already render whatever is
    in `DraftState`, so bots simply have to fill it."""

    def _server(self, tmp_path, monkeypatch, board_and_cfg, extra=()):
        import scripts.gui as gui

        b, cfg = board_and_cfg
        state = UiState(
            draft=DraftState(board=b, num_teams=4, my_slot=2, rounds=3, roster_positions=LAYOUT),
            cfg=cfg,
        )
        monkeypatch.setattr(gui, "build_draft_state", lambda args: state)
        monkeypatch.setattr(gui.GuiServer, "server_bind", lambda self: None)
        monkeypatch.setattr(gui.GuiServer, "server_activate", lambda self: None)
        args = gui.parse_args([
            "--mock", "--slot", "2", "--seed", "1", "--teams", "4", "--rounds", "3",
            "--log", str(tmp_path / "log.jsonl"), *extra,
        ])
        return gui, gui.GuiServer(("127.0.0.1", 0), None, args), args

    def test_mock_installs_a_bot_driver_and_suppresses_sleeper(self, tmp_path, monkeypatch, board):
        gui, server, args = self._server(tmp_path, monkeypatch, board)
        assert server.mock_driver is not None
        assert server.sync is None
        # A live poll would fight the bots over the same pick numbers.
        assert args.sync is False
        assert "mock" in server.draft_ui_state.sync_reason.lower()

    def test_bots_fill_every_seat_up_to_my_pick(self, tmp_path, monkeypatch, board):
        gui, server, _args = self._server(tmp_path, monkeypatch, board)
        gui._advance_bots(server)
        draft = server.draft_ui_state.draft
        # my_slot=2, so bots take pick 1 and stop.
        assert draft.current_pick() == 2
        assert len(draft.picks) == 1
        assert draft.picks[0].mine is False

    def test_bots_resume_after_my_pick(self, tmp_path, monkeypatch, board):
        gui, server, _args = self._server(tmp_path, monkeypatch, board)
        gui._advance_bots(server)
        draft = server.draft_ui_state.draft
        pool = [bp for bp in draft.board.players if bp.key not in draft.taken_keys()]
        draft.record(pool[0].key, mine=True)
        gui._advance_bots(server)
        # Snake, 4 teams: my next pick is 7, so bots take 3-6.
        assert draft.current_pick() == 7
        assert sum(1 for p in draft.picks if not p.mine) == 5

    def test_bot_picks_reach_the_draft_log(self, tmp_path, monkeypatch, board):
        gui, server, _args = self._server(tmp_path, monkeypatch, board)
        gui._advance_bots(server)
        logged = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(logged) == 1
        assert json.loads(logged[0])["pick"]["mine"] is False

    def test_no_driver_without_mock(self, tmp_path, monkeypatch, board):
        import scripts.gui as gui

        b, cfg = board
        state = UiState(
            draft=DraftState(board=b, num_teams=4, my_slot=2, rounds=3, roster_positions=LAYOUT),
            cfg=cfg,
        )
        monkeypatch.setattr(gui, "build_draft_state", lambda args: state)
        monkeypatch.setattr(gui, "_build_sync", lambda args, st: None)
        monkeypatch.setattr(gui.GuiServer, "server_bind", lambda self: None)
        monkeypatch.setattr(gui.GuiServer, "server_activate", lambda self: None)
        args = gui.parse_args(["--no-sync", "--log", str(tmp_path / "log.jsonl")])
        server = gui.GuiServer(("127.0.0.1", 0), None, args)
        assert server.mock_driver is None
        gui._advance_bots(server)  # no-op, must not raise
        assert server.draft_ui_state.draft.current_pick() == 1
