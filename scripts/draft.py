#!/usr/bin/env python3
"""Live snake-draft assistant: keyboard-driven, fully offline.

    python scripts/draft.py --slot 4
    python scripts/draft.py --board my_rankings.csv --slot 4
    python scripts/draft.py --resume          # continue after a crash/restart

Every command you type is appended to the log file (`draft_log.jsonl` by
default) as soon as it's processed. `--resume` replays that log through the
same pure `ffbot.draft_ui.handle()` used interactively, so a restart mid-draft
costs a few seconds, not the draft.

This module must not import `ffbot.sleeper` at module level — the offline
path has to work even with no network or a broken venv. Only `--sync` (see
ffbot/draft_sync.py) touches the network, and it imports lazily.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import draft_store  # noqa: E402
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
    p.add_argument("--slot", type=int, default=None, help="your draft slot (1-indexed); Sleeper randomizes this, so you usually pass it here (or let --sync resolve it from sleeper.roster_id)")
    p.add_argument("--teams", type=int, default=None, help="override draft.num_teams")
    p.add_argument("--rounds", type=int, default=None, help="override draft.rounds")
    p.add_argument("--order", choices=["snake", "linear"], default=None, help="override draft.order")
    p.add_argument("--log", default="draft_log.jsonl", help="command log path (default: draft_log.jsonl)")
    p.add_argument("--resume", action="store_true", help="replay --log before entering the interactive loop")
    p.add_argument(
        "--sync", action=argparse.BooleanOptionalAction, default=True,
        help="poll Sleeper's live draft picks in the background (no auth needed); on by default, pass --no-sync for a fully offline session",
    )
    p.add_argument("--draft-id", default=None, help="Sleeper draft id for --sync (default: resolved from sleeper.league_id's current draft)")
    p.add_argument("--ids-file", default="draft/sleeper_ids.json", help="board-key -> Sleeper player id map from `draft_export.py --reconcile` (default: draft/sleeper_ids.json)")
    return p.parse_args(argv)


def _fetch_kalshi_draft_signal(cfg: Config, board) -> dict[str, float]:
    """Best-effort fetch of the draft-side Kalshi signal. Skipped entirely
    (no network touched at all) when `cfg.draft.kalshi_weight == 0.0` --
    the same "don't even ask" guard `draft.demand_ahead`'s `block_weight`
    check uses. Any failure degrades to an empty dict with a warning,
    never taking down an otherwise-working offline draft session -- same
    contract as `_build_sync`.
    """
    if cfg.draft.kalshi_weight == 0.0:
        return {}
    try:
        from ffbot.markets import kalshi_nfl
        return kalshi_nfl.draft_signal(board)
    except Exception as exc:  # noqa: BLE001 -- a market-data hiccup must never block an offline draft session
        print(f"kalshi_weight: draft signal fetch failed ({exc}). Continuing without it.", file=sys.stderr)
        return {}


def build_state(args: argparse.Namespace) -> UiState:
    cfg = Config.load(args.config)
    if args.slot is not None:
        cfg.draft.my_slot = args.slot
    if args.teams is not None:
        cfg.draft.num_teams = args.teams
    if args.rounds is not None:
        cfg.draft.rounds = args.rounds
    if args.order is not None:
        cfg.draft.order = args.order

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
        order=cfg.draft.order,
    )
    kalshi_scores = _fetch_kalshi_draft_signal(cfg, board)
    return UiState(draft=draft, cfg=cfg, kalshi_scores=kalshi_scores)


def _append_log(log_path: Path, line: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"line": line}) + "\n")


def _append_sync_log(log_path: Path, pick) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"sync": {"number": pick.number, "key": pick.key, "mine": pick.mine}}) + "\n")


def _append_pick_log(log_path: Path, key: str | None, mine: bool | None) -> None:
    """Log an exact-key pick that didn't go through `handle()`'s name search —
    e.g. a GUI row click, where the key is already known and re-running it
    through search on replay could in principle resolve differently."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"pick": {"key": key, "mine": mine}}) + "\n")


def replay_log(state: UiState, log_path: Path) -> UiState:
    """Replay every kind of log entry: typed commands (`handle()`), synced
    picks, and exact-key picks (both bypass `handle()` — matched on board
    key, not a name search, so replaying either as a search string could
    resolve differently than it did live).

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
            elif "pick" in entry:
                try:
                    state.draft.record(entry["pick"]["key"], mine=entry["pick"]["mine"])
                except ValueError:
                    pass  # already applied by a "line" entry earlier in the log
    return state


def handle_local_command(
    state: UiState, line: str, args: argparse.Namespace, log_path: Path
) -> tuple[UiState, bool]:
    """Intercept `reset` / `save <name>` / `load <name>` before they'd reach
    the pure `draft_ui.handle()` — all three touch the filesystem (archiving
    or rebuilding the log), which `handle()` must never do.

    Returns `(new_state, handled)`; `handled=False` means the line wasn't one
    of these and the caller should still route it through `handle()`.
    """
    stripped = line.strip()

    if stripped == "reset":
        state.message = "type 'reset yes' to archive this draft and start a fresh one"
        return state, True

    if stripped == "reset yes":
        archived = draft_store.archive_log(log_path)
        fresh = build_state(args)
        fresh.message = f"draft reset — previous log archived to {archived}" if archived else "draft reset"
        return fresh, True

    if stripped.startswith("save "):
        name = stripped[len("save ") :].strip()
        try:
            dest = draft_store.save_snapshot(log_path, name)
            state.message = f"saved to {dest}"
        except draft_store.DraftStoreError as exc:
            state.message = str(exc)
        return state, True

    if stripped.startswith("load "):
        name = stripped[len("load ") :].strip()
        try:
            snap_path = draft_store.load_snapshot(name)
        except draft_store.DraftStoreError as exc:
            state.message = str(exc)
            return state, True
        archived = draft_store.archive_log(log_path)
        fresh = build_state(args)
        fresh = replay_log(fresh, snap_path)
        # Copy (not move) the snapshot onto the live log so this session's
        # further picks append to a fresh copy, leaving the saved snapshot
        # itself intact for a future load.
        shutil.copyfile(snap_path, log_path)
        note = f" (previous log archived to {archived})" if archived else ""
        fresh.message = f"loaded '{name}'{note}"
        return fresh, True

    return state, False


def run_loop(state: UiState, log_path: Path, args: argparse.Namespace, sync=None) -> None:
    while not state.should_quit:
        if sync is not None:
            for pick in apply_synced_picks(state.draft, sync.drain()):
                _append_sync_log(log_path, pick)
            state.sync_status = sync.status()
            state.sync_unmapped = sync.unmapped_count()

        print(_CLEAR + render(state))
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting. Your draft is saved in", log_path)
            return

        state, handled = handle_local_command(state, line, args, log_path)
        if not handled:
            state = handle(state, line)
            _append_log(log_path, line)
    print("Draft assistant exiting. Log saved to", log_path)


def _build_sync(args: argparse.Namespace, state: UiState):
    """Best-effort live sync setup. Any failure here must not take down an
    otherwise-working offline draft session -- print a warning and return
    None rather than raise.

    Every failure path also sets `state.sync_reason` (a direct mutation,
    same as the `state.draft.my_slot` resolution below) so both front ends
    can show *why* sync stayed off, not just that it did -- `--sync` now
    defaults on for both `scripts/draft.py` and `scripts/gui.py`, so "off"
    with no explanation would otherwise read as a silent mystery rather
    than the deliberate degradation it is.

    Imports are resolved in their OWN try/except, separate from the network
    try/except below: an earlier version imported `SleeperFetchError` INSIDE
    the single try whose `except` clause named it, so any import failure
    raised a bare `NameError` evaluating the except clause itself, escaping
    this function entirely -- exactly the "must not take down an otherwise-
    working session" contract this docstring promises, broken by the one
    thing it exists to guard against. Splitting the imports into their own
    try/except (rather than just hoisting them unguarded above the network
    try) fixes both problems at once: `SleeperFetchError` is always bound by
    the time the second try/except runs, AND a broken import degrades the
    same way a network failure does instead of propagating uncaught.
    """
    ids_path = Path(args.ids_file)
    if not ids_path.exists():
        state.sync_reason = f"{ids_path} not found — run scripts/draft_export.py --reconcile first"
        print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
        return None
    if not state.cfg.sleeper.league_id:
        state.sync_reason = "config.yml has no sleeper.league_id"
        print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
        return None

    try:
        from ffbot.draft_sync import DraftSync
        from ffbot.sleeper.cache import SleeperFetchError
        from ffbot.sleeper.client import SleeperClient
        from ffbot.sleeper_roster import RosterSourceError, resolve_roster_id
    except ImportError as exc:
        state.sync_reason = f"setup failed ({exc})"
        print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
        return None

    try:
        # sleeper_ids.json maps board_key -> sleeper_id; DraftSync needs the
        # inverse (sleeper_id -> board_key) to translate incoming picks.
        raw_ids: dict[str, str] = json.loads(ids_path.read_text(encoding="utf-8"))
        id_map = {sid: key for key, sid in raw_ids.items()}

        client_kwargs = {"cache_dir": state.cfg.sleeper.cache_dir} if state.cfg.sleeper.cache_dir else {}
        client = SleeperClient(**client_kwargs)
        draft_id = args.draft_id
        if not draft_id:
            league = client.league(state.cfg.sleeper.league_id)
            draft_id = league.get("draft_id")
        if not draft_id:
            state.sync_reason = "could not resolve a draft_id from sleeper.league_id (pass --draft-id explicitly)"
            print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
            return None

        # roster_id identifies which picks are "mine" more reliably than a
        # snake-order guess (it survives trades/keepers) -- resolve it from
        # username when config.yml leaves it unset, the same lookup
        # report.py's live roster route already does.
        my_roster_id = state.cfg.sleeper.roster_id
        if my_roster_id is None and state.cfg.sleeper.username:
            try:
                my_roster_id = resolve_roster_id(client, state.cfg.sleeper.league_id, state.cfg.sleeper.username)
            except RosterSourceError as exc:
                print(f"--sync: could not resolve roster_id from username ({exc}). Picks will sync without ownership.", file=sys.stderr)

        # Resolve --slot from Sleeper's own draft-order mapping when the
        # user didn't pass it explicitly -- Sleeper randomizes draft
        # position, so this is real information a hand-typed config.yml
        # default (draft.my_slot) can't have. Explicit --slot always wins.
        if args.slot is None and my_roster_id is not None:
            draft_obj = client.draft(draft_id)
            slot_to_roster_id = draft_obj.get("slot_to_roster_id") or {}
            resolved_slot = next(
                (int(slot) for slot, rid in slot_to_roster_id.items() if rid == my_roster_id),
                None,
            )
            if resolved_slot is not None and resolved_slot != state.draft.my_slot:
                state.draft.my_slot = resolved_slot
                print(f"--sync: resolved your draft slot to {resolved_slot} from sleeper.roster_id.", file=sys.stderr)

        state.sync_reason = ""  # clear any reason a prior --resume attempt may have left
        return DraftSync(
            client,
            draft_id,
            id_map,
            my_roster_id=my_roster_id,
            poll_seconds=state.cfg.draft.sync_poll_seconds,
        )
    except SleeperFetchError as exc:
        state.sync_reason = f"could not reach Sleeper ({exc})"
        print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
        return None
    except Exception as exc:
        state.sync_reason = f"setup failed ({exc})"
        print(f"--sync: {state.sync_reason}. Continuing without sync.", file=sys.stderr)
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
            run_loop(state, log_path, args, sync=sync)
        finally:
            sync.stop()
    else:
        run_loop(state, log_path, args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
