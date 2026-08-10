from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ffbot.draft_sync import apply_synced_picks, SyncedPick
from ffbot.draft_ui import UiState, handle
from scripts.draft import (  # noqa: E402
    _append_log,
    _append_pick_log,
    _append_sync_log,
    build_state,
    handle_local_command,
    parse_args,
    replay_log,
)

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


def _write_board_csv(tmp_path) -> Path:
    rows = []
    counts = {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}
    n = 0
    for pos, c in counts.items():
        for i in range(c):
            n += 1
            rows.append(f"P{pos}{i},XXX,{pos},{5 + n % 10},{300 - n},{n}")
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS,AVG\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_config(tmp_path, board_csv: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "league_id: \"\"\n"
        "team_key: \"\"\n"
        "roster_positions:\n"
        "  QB: 1\n  WR: 2\n  RB: 2\n  TE: 1\n  W/R/T: 1\n  K: 1\n  DEF: 1\n  BN: 6\n  IR: 1\n"
        "draft:\n"
        "  num_teams: 12\n"
        "  my_slot: 1\n"
        "  rounds: 15\n"
        # as_posix(): a Windows path in a double-quoted YAML scalar makes the
        # "\U" of "C:\Users" a Unicode escape, and the config fails to parse.
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n",
        encoding="utf-8",
    )
    return path


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.config == "config.yml"
        assert args.board is None
        assert args.slot is None
        assert args.resume is False
        assert args.sync is False
        assert args.log == "draft_log.jsonl"

    def test_slot_and_board_overrides(self):
        args = parse_args(["--slot", "4", "--board", "a.csv", "--board", "b.csv"])
        assert args.slot == 4
        assert args.board == ["a.csv", "b.csv"]

    def test_resume_and_sync_flags(self):
        args = parse_args(["--resume", "--sync"])
        assert args.resume is True
        assert args.sync is True

    def test_order_flag(self):
        assert parse_args([]).order is None
        assert parse_args(["--order", "linear"]).order == "linear"
        assert parse_args(["--order", "snake"]).order == "snake"

    def test_invalid_order_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--order", "auction"])


class TestBuildState:
    def test_loads_from_config_board_csv(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])
        state = build_state(args)
        assert state.draft.num_teams == 12
        assert state.draft.my_slot == 1
        assert len(state.draft.board.players) > 0

    def test_cli_board_overrides_config(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path), "--board", str(board_csv), "--slot", "7"])
        state = build_state(args)
        assert state.draft.my_slot == 7

    def test_missing_board_exits(self, tmp_path, monkeypatch):
        config_path = tmp_path / "empty_config.yml"
        config_path.write_text("league_id: \"\"\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])
        with pytest.raises(SystemExit):
            build_state(args)

    def test_teams_and_rounds_overrides(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path), "--teams", "8", "--rounds", "10"])
        state = build_state(args)
        assert state.draft.num_teams == 8
        assert state.draft.rounds == 10

    def test_order_override(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path), "--order", "linear"])
        state = build_state(args)
        assert state.draft.order == "linear"

    def test_default_order_is_snake(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])
        state = build_state(args)
        assert state.draft.order == "snake"


class TestReplayLog:
    def test_replaying_a_prior_quit_does_not_set_should_quit(self, tmp_path, monkeypatch):
        # --resume exists to continue a session. A previously-logged "q" is
        # a historical event, not a live directive -- replaying it must not
        # leave the resumed state pre-quit, or --resume becomes a no-op
        # after every session that ended normally.
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])

        live_state = build_state(args)
        log_path = tmp_path / "draft_log.jsonl"
        bp0 = live_state.draft.board.players[0]
        cmd = bp0.name.split()[0][:6]
        live_state = handle(live_state, cmd)
        _append_log(log_path, cmd)
        live_state = handle(live_state, "q")
        _append_log(log_path, "q")
        assert live_state.should_quit is True

        fresh_state = build_state(args)
        resumed_state = replay_log(fresh_state, log_path)
        assert resumed_state.should_quit is False
        assert resumed_state.draft.current_pick() == 2  # the real pick still replayed

    def test_missing_log_is_noop(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        state = build_state(parse_args(["--config", str(config_path)]))
        result = replay_log(state, tmp_path / "does_not_exist.jsonl")
        assert result.draft.current_pick() == 1

    def test_replay_reproduces_identical_state(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])

        # "Live" run: apply commands and log each one, exactly as run_loop does.
        live_state = build_state(args)
        log_path = tmp_path / "draft_log.jsonl"
        commands = []
        available = list(live_state.draft.board.players)
        for i in range(6):
            bp = available.pop(0)
            commands.append(bp.name.split()[0][:6])
        commands.append("u")  # exercise undo in the replay too
        commands.append(commands[-2])  # re-pick what undo just freed up

        for cmd in commands:
            live_state = handle(live_state, cmd)
            _append_log(log_path, cmd)

        # "Resumed" run: fresh state, replay the log, must match exactly.
        fresh_state = build_state(args)
        resumed_state = replay_log(fresh_state, log_path)

        assert resumed_state.draft.taken_keys() == live_state.draft.taken_keys()
        assert resumed_state.draft.current_pick() == live_state.draft.current_pick()
        assert [bp.key for bp in resumed_state.draft.my_roster()] == [
            bp.key for bp in live_state.draft.my_roster()
        ]

    def test_replay_reproduces_sync_applied_picks(self, tmp_path, monkeypatch):
        # Sync-applied picks bypass handle() entirely (they match on board
        # key, not a name search), so they're logged as a distinct "sync"
        # entry -- replay must reconstruct them too, not just typed commands.
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])

        live_state = build_state(args)
        log_path = tmp_path / "draft_log.jsonl"
        bp0, bp1 = live_state.draft.board.players[:2]

        # One interactive command (forced not-mine, so only the sync-applied
        # pick below should land on my roster), then one sync-applied pick.
        cmd = "-" + bp0.name.split()[0][:6]
        live_state = handle(live_state, cmd)
        _append_log(log_path, cmd)
        for pick in apply_synced_picks(live_state.draft, [SyncedPick(number=2, key=bp1.key, mine=True)]):
            _append_sync_log(log_path, pick)

        fresh_state = build_state(args)
        resumed_state = replay_log(fresh_state, log_path)

        assert resumed_state.draft.taken_keys() == live_state.draft.taken_keys()
        assert resumed_state.draft.current_pick() == live_state.draft.current_pick() == 3
        assert [bp.key for bp in resumed_state.draft.my_roster()] == [bp1.key]

    def test_replay_reproduces_pick_entries(self, tmp_path, monkeypatch):
        # An exact-key pick (e.g. a GUI row click) bypasses handle()'s name
        # search entirely and is logged as its own "pick" entry -- replay
        # must reconstruct it the same way sync-applied picks are.
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])

        live_state = build_state(args)
        log_path = tmp_path / "draft_log.jsonl"
        bp0, bp1 = live_state.draft.board.players[:2]

        live_state.draft.record(bp0.key, mine=False)
        _append_pick_log(log_path, bp0.key, False)
        live_state.draft.record(bp1.key, mine=True)
        _append_pick_log(log_path, bp1.key, True)

        fresh_state = build_state(args)
        resumed_state = replay_log(fresh_state, log_path)

        assert resumed_state.draft.taken_keys() == live_state.draft.taken_keys()
        assert resumed_state.draft.current_pick() == live_state.draft.current_pick() == 3
        assert [bp.key for bp in resumed_state.draft.my_roster()] == [bp1.key]

    def test_replay_skips_blank_lines(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])
        state = build_state(args)
        log_path = tmp_path / "log.jsonl"
        log_path.write_text("\n\n", encoding="utf-8")
        result = replay_log(state, log_path)
        assert result.draft.current_pick() == 1


class TestAppendLog:
    def test_appends_one_json_line_per_call(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _append_log(log_path, "jefferson")
        _append_log(log_path, "*mccaffrey")
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        import json

        assert json.loads(lines[0]) == {"line": "jefferson"}
        assert json.loads(lines[1]) == {"line": "*mccaffrey"}


class TestHandleLocalCommand:
    def _live_state(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        args = parse_args(["--config", str(config_path)])
        return args, build_state(args)

    def test_unrecognized_line_is_not_handled(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        new_state, handled = handle_local_command(state, "jefferson", args, log_path)
        assert handled is False
        assert new_state is state

    def test_reset_alone_asks_for_confirmation_and_does_nothing(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        bp0 = state.draft.board.players[0]
        state.draft.record(bp0.key)
        _append_log(log_path, bp0.name)

        new_state, handled = handle_local_command(state, "reset", args, log_path)
        assert handled is True
        assert "reset yes" in new_state.message
        # Nothing was actually reset -- same DraftState object, pick still there.
        assert new_state is state
        assert new_state.draft.current_pick() == 2
        assert log_path.exists()

    def test_reset_yes_archives_log_and_rebuilds_fresh_state(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        bp0 = state.draft.board.players[0]
        state.draft.record(bp0.key)
        _append_log(log_path, bp0.name)
        assert log_path.exists()

        new_state, handled = handle_local_command(state, "reset yes", args, log_path)
        assert handled is True
        assert new_state.draft.current_pick() == 1  # fresh state, no picks
        assert not log_path.exists()  # archived away
        archives = list(tmp_path.glob("draft_log.*.jsonl"))
        assert len(archives) == 1
        assert "archived" in new_state.message

    def test_reset_yes_with_no_prior_log_still_works(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        assert not log_path.exists()
        new_state, handled = handle_local_command(state, "reset yes", args, log_path)
        assert handled is True
        assert new_state.draft.current_pick() == 1

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        bp0, bp1 = state.draft.board.players[:2]
        state.draft.record(bp0.key, mine=True)
        _append_log(log_path, bp0.name)

        new_state, handled = handle_local_command(state, "save mydraft", args, log_path)
        assert handled is True
        assert "saved" in new_state.message
        assert (tmp_path / "draft" / "saves" / "mydraft.jsonl").exists()

        # Advance further, then load back the saved snapshot -- should
        # discard the extra pick and restore the state at save time.
        state.draft.record(bp1.key, mine=False)
        _append_log(log_path, bp1.name)
        assert state.draft.current_pick() == 3

        new_state, handled = handle_local_command(state, "load mydraft", args, log_path)
        assert handled is True
        assert "loaded" in new_state.message
        assert new_state.draft.current_pick() == 2
        assert [bp.key for bp in new_state.draft.my_roster()] == [bp0.key]
        # The live log now mirrors what was loaded, and the pre-load log was archived.
        assert log_path.exists()
        archives = list(tmp_path.glob("draft_log.*.jsonl"))
        assert len(archives) == 1

    def test_load_unknown_save_reports_error_without_crashing(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        new_state, handled = handle_local_command(state, "load nope", args, log_path)
        assert handled is True
        assert "no save named" in new_state.message
        assert new_state is state

    def test_save_with_no_picks_yet_reports_error(self, tmp_path, monkeypatch):
        args, state = self._live_state(tmp_path, monkeypatch)
        log_path = tmp_path / "draft_log.jsonl"
        assert not log_path.exists()
        new_state, handled = handle_local_command(state, "save mydraft", args, log_path)
        assert handled is True
        assert "nothing to save" in new_state.message
