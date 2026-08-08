#!/usr/bin/env python3
"""Live snake-draft assistant: keyboard-driven, fully offline.

    python scripts/draft.py --slot 4
    python scripts/draft.py --board my_rankings.csv --slot 4
    python scripts/draft.py --resume          # continue after a crash/restart

Every command you type is appended to the log file (`draft_log.jsonl` by
default) as soon as it's processed. `--resume` replays that log through the
same pure `ffbot.draft_ui.handle()` used interactively, so a restart mid-draft
costs a few seconds, not the draft.

This module must not import `yahoo_fantasy_api` or `requests` at module
level — the offline path has to work even if the Yahoo API was never
approved, or the venv is broken. Only `--sync` (see ffbot/draft_sync.py)
touches the network, and it imports lazily.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.draft import DraftState  # noqa: E402
from ffbot.draft_sync import apply_synced_picks  # noqa: E402  (no yahoo_fantasy_api/requests import in this module)
from ffbot.draft_ui import UiState, handle, render  # noqa: E402

_CLEAR = "\033[2J\033[H"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml (default: config.yml)")
    p.add_argument("--board", action="append", default=None, help="FantasyPros CSV path (repeatable); overrides config.yml's draft.board_csv")
    p.add_argument("--slot", type=int, default=None, help="your draft slot (1-indexed); Yahoo randomizes this, so you usually pass it here")
    p.add_argument("--teams", type=int, default=None, help="override draft.num_teams")
    p.add_argument("--rounds", type=int, default=None, help="override draft.rounds")
    p.add_argument("--log", default="draft_log.jsonl", help="command log path (default: draft_log.jsonl)")
    p.add_argument("--resume", action="store_true", help="replay --log before entering the interactive loop")
    p.add_argument("--sync", action="store_true", help="poll Yahoo's live draft results in the background (requires API access)")
    p.add_argument("--ids-file", default="draft/yahoo_ids.json", help="board-key -> Yahoo player id map from `draft_export.py --yahoo-players` (default: draft/yahoo_ids.json)")
    return p.parse_args(argv)


def build_state(args: argparse.Namespace) -> UiState:
    cfg = Config.load(args.config)
    if args.slot is not None:
        cfg.draft.my_slot = args.slot
    if args.teams is not None:
        cfg.draft.num_teams = args.teams
    if args.rounds is not None:
        cfg.draft.rounds = args.rounds

    try:
        board = load_board_from_config(cfg, args.board)
    except ValueError as exc:
        print(
            f"{exc}. Pass --board path/to/rankings.csv, or set draft.board_csv "
            "in config.yml.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    draft = DraftState(
        board=board,
        num_teams=cfg.draft.num_teams,
        my_slot=cfg.draft.my_slot,
        rounds=cfg.draft.rounds,
        roster_positions=cfg.roster_positions,
        my_picks_override=list(cfg.draft.my_picks),
    )
    return UiState(draft=draft, cfg=cfg)


def _append_log(log_path: Path, line: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"line": line}) + "\n")


def _append_sync_log(log_path: Path, pick) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"sync": {"number": pick.number, "key": pick.key, "mine": pick.mine}}) + "\n")


def replay_log(state: UiState, log_path: Path) -> UiState:
    """Replay both kinds of log entry: typed commands (`handle()`) and
    sync-applied picks (recorded directly, bypassing `handle()` — sync
    matches on board key, not on a name search, so replaying it as a search
    string could resolve differently than it did live).

    A logged "q" is a historical event, not a live directive: --resume
    exists to continue a session, so replaying a prior quit must reconstruct
    the draft state up to that point without re-firing the quit itself --
    otherwise resuming after any session that ended normally would
    immediately exit again before the user can type anything.
    """
    if not log_path.exists():
        return state
    with open(log_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            entry = json.loads(raw)
            if "line" in entry:
                state = handle(state, entry["line"])
                state.should_quit = False
            elif "sync" in entry:
                try:
                    state.draft.record(entry["sync"]["key"], mine=entry["sync"]["mine"], source="api")
                except ValueError:
                    pass  # already applied by a "line" entry earlier in the log
    return state


def run_loop(state: UiState, log_path: Path, sync=None) -> None:
    while not state.should_quit:
        if sync is not None:
            for pick in apply_synced_picks(state.draft, sync.drain()):
                _append_sync_log(log_path, pick)
            state.sync_status = sync.status()

        print(_CLEAR + render(state))
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting. Your draft is saved in", log_path)
            return
        state = handle(state, line)
        _append_log(log_path, line)
    print("Draft assistant exiting. Log saved to", log_path)


def _build_sync(args: argparse.Namespace, state: UiState):
    """Best-effort live sync setup. Any failure here must not take down an
    otherwise-working offline draft session -- print a warning and return
    None rather than raise."""
    ids_path = Path(args.ids_file)
    if not ids_path.exists():
        print(
            f"--sync: {ids_path} not found (run scripts/draft_export.py "
            "--yahoo-players first). Continuing without sync.",
            file=sys.stderr,
        )
        return None
    if not state.cfg.league_id:
        print("--sync: config.yml has no league_id. Continuing without sync.", file=sys.stderr)
        return None

    try:
        import yahoo_fantasy_api as yfa

        from ffbot.auth import YahooSession, env_file_persister
        from ffbot.draft_sync import DraftSync

        # yahoo_ids.json maps board_key -> yahoo_id; DraftSync needs the
        # inverse (yahoo_id -> board_key) to translate incoming picks.
        raw_ids: dict[str, int] = json.loads(ids_path.read_text(encoding="utf-8"))
        id_map = {yid: key for key, yid in raw_ids.items()}
        sc = YahooSession.from_env(on_refresh=env_file_persister(".env"))
        league = yfa.Game(sc, "nfl").to_league(state.cfg.league_id)
        return DraftSync(
            league,
            id_map,
            my_team_key=state.cfg.team_key or None,
            poll_seconds=state.cfg.draft.sync_poll_seconds,
        )
    except Exception as exc:
        print(f"--sync: setup failed ({exc}). Continuing without sync.", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = build_state(args)

    log_path = Path(args.log)
    if args.resume:
        state = replay_log(state, log_path)

    sync = _build_sync(args, state) if args.sync else None
    if sync is not None:
        sync.start()
        state.sync_status = "live"
        try:
            run_loop(state, log_path, sync=sync)
        finally:
            sync.stop()
    else:
        run_loop(state, log_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
