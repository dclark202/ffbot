#!/usr/bin/env python3
"""Mock draft against local bots — no Sleeper, no network, no waiting.

    python scripts/mock_draft.py --slot 4                 # you pick, bots fill in
    python scripts/mock_draft.py --slot 4 --auto          # engine drafts for you too
    python scripts/mock_draft.py --auto --runs 5          # five drafts, five reports

Exists because prototyping against a real Sleeper mock costs a live draft
room and several minutes per run, and the thing this repo most needs when a
recommendation looks wrong is MORE drafts — one is a hypothesis, three that
rhyme is a mechanism worth chasing.

The interactive mode is the same terminal UI `scripts/draft.py` gives you
(same `draft_ui.handle`/`render`, so `*name`, `-name`, `u`, `x`, `p RB`, `s`,
`?name` all work); bots simply take every other seat and pick instantly.
`--auto` hands your seat to the engine too, which turns a whole draft into a
couple of seconds and is the fast way to generate report data in bulk.

Bots draft with `draft.recommend()` at `--bot-spice` (default 1, VOR-chalk),
each from ITS OWN seat: the pick log is re-flagged so `my_roster()` returns
that bot's roster rather than yours. `ffbot/backtest/draft_sim.py` warns
against routing opponents through `recommend()` for exactly the reason this
sidesteps — run against YOUR roster it applies your caps and forced-fill to
them, and every opponent ends up hoarding defenses.

Bots pick from their top `--bot-window` recommendations rather than always
#1, so repeated runs explore different boards instead of replaying one
scripted draft. `--seed` makes a run reproducible.

Writes the same `draft_log.jsonl` and the same `draft/reports/*.json` a real
draft does, so everything downstream (`scripts/draft_report.py`,
`scripts/draft_counterfactual.py`) works on these unchanged.
"""

from __future__ import annotations

import argparse
import random
import sys
import warnings
from dataclasses import replace as dc_replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config, DraftConfig  # noqa: E402
from ffbot.draft import DraftState, recommend, team_slot_at  # noqa: E402
from ffbot.draft_report import DraftReporter  # noqa: E402
from ffbot.draft_ui import UiState, handle, render  # noqa: E402
from scripts.draft import _append_pick_log  # noqa: E402

_CLEAR = "\033[2J\033[H"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--slot", type=int, default=None, help="your draft slot (default: draft.my_slot)")
    p.add_argument("--teams", type=int, default=None, help="override draft.num_teams")
    p.add_argument("--rounds", type=int, default=None, help="override draft.rounds")
    p.add_argument("--bot-spice", type=int, default=1, help="spice level the bots draft at (default: %(default)s)")
    p.add_argument(
        "--bot-window", type=int, default=3,
        help="bots pick randomly among their top N recommendations, so runs differ (default: %(default)s)",
    )
    p.add_argument("--auto", action="store_true", help="let the engine make your picks too")
    p.add_argument("--runs", type=int, default=1, help="how many drafts (only with --auto)")
    p.add_argument("--seed", type=int, default=None, help="reproducible bot behaviour")
    p.add_argument("--log", default="draft/mock_log.jsonl", help="(default: %(default)s)")
    p.add_argument("--quiet", action="store_true", help="--auto: print only the final roster")
    return p.parse_args(argv)


def _bot_view(state: DraftState, slot: int) -> DraftState:
    """`state` as seen from `slot`'s seat.

    `my_roster()` reads each pick's `mine` flag, so a bot's own roster only
    exists if the log is re-flagged for it — without this every bot would
    evaluate candidates against YOUR roster and draft to fill YOUR holes.
    Derives ownership from pick number rather than `slot_for`, which short-
    circuits on the `mine` flag this is in the middle of rewriting.
    """
    picks = [
        dc_replace(p, mine=(team_slot_at(p.number, state.num_teams, state.order) == slot))
        for p in state.picks
    ]
    return dc_replace(state, picks=picks, my_slot=slot)


def _bot_pick(state: DraftState, bot_cfg: Config, slot: int, rng: random.Random, window: int) -> str | None:
    recs = recommend(_bot_view(state, slot), bot_cfg, limit=max(1, window))
    if not recs:
        taken = state.taken_keys()
        pool = [b for b in state.board.players if b.key not in taken and b.adp is not None]
        return min(pool, key=lambda b: b.adp).key if pool else None
    return rng.choice(recs).player.key


def _summary(state: DraftState) -> str:
    counts: dict[str, int] = {}
    for bp in state.my_roster():
        counts[bp.position] = counts.get(bp.position, 0) + 1
    total = sum(bp.points for bp in state.my_roster())
    return f"{counts}  projected {total:.0f}"


def run_draft(args, cfg: Config, board, bot_cfg: Config, rng: random.Random, run_index: int) -> DraftState:
    draft = DraftState(
        board=board, num_teams=cfg.draft.num_teams, my_slot=cfg.draft.my_slot,
        rounds=cfg.draft.rounds, roster_positions=cfg.roster_positions,
        my_picks_override=list(cfg.draft.my_picks), order=cfg.draft.order,
    )
    state = UiState(draft=draft, cfg=cfg)
    state.sync_status = "off"
    state.sync_reason = "local mock draft — bots, not Sleeper"

    draft_id = f"mock-{run_index}" if args.runs > 1 else "mock"
    reporter = DraftReporter(
        cfg=cfg, draft_id=draft_id,
        # Ownership here is exact by construction (we assign every seat
        # ourselves), so neither Sleeper label fits; say what it really is.
        ownership_source="local_mock",
    )
    log_path = Path(args.log)
    total_picks = cfg.draft.num_teams * cfg.draft.rounds
    my_picks = set(draft.my_picks())

    while draft.current_pick() <= total_picks and not state.should_quit:
        pick_no = draft.current_pick()
        if pick_no in my_picks:
            if args.auto:
                recs = recommend(draft, cfg, limit=1)
                if not recs:
                    break
                key = recs[0].player.key
                reporter.capture(draft, key)
                draft.record(key, mine=True)
                _append_pick_log(log_path, key, True)
                if not args.quiet:
                    bp = board.by_key[key]
                    print(f"  R{(pick_no - 1) // cfg.draft.num_teams + 1:<2} pick {pick_no:3d}  YOU  {bp.position:3} {bp.name}")
            else:
                print(_CLEAR + render(state))
                print(f"\n[mock draft — bots at spice {args.bot_spice}] pick {pick_no}, YOUR TURN")
                try:
                    line = input("> ")
                except (EOFError, KeyboardInterrupt):
                    print(f"\nStopped. Log: {log_path}")
                    return draft
                before = len(draft.picks)
                # Capture before `handle` records, so the report holds the
                # table as it stood at the decision -- the whole point of it.
                pending_pick = None
                if line.strip() and not line.strip().startswith(("p", "s", "u", "x", "q", "?", "me ")):
                    pending_pick = True
                state = handle(state, line)
                if len(draft.picks) > before and pending_pick:
                    last = draft.picks[-1]
                    if last.mine and last.key:
                        _append_pick_log(log_path, last.key, True)
            continue

        slot = team_slot_at(pick_no, cfg.draft.num_teams, cfg.draft.order)
        key = _bot_pick(draft, bot_cfg, slot, rng, args.bot_window)
        if key is None:
            break
        draft.record(key, mine=False)
        _append_pick_log(log_path, key, False)

    reporter.flush(draft)
    return draft


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    if args.slot is not None:
        cfg.draft.my_slot = args.slot
    if args.teams is not None:
        cfg.draft.num_teams = args.teams
    if args.rounds is not None:
        cfg.draft.rounds = args.rounds
    if args.runs > 1 and not args.auto:
        print("error: --runs > 1 only makes sense with --auto", file=sys.stderr)
        return 1

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            board = load_board_from_config(cfg)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    # Bots share the board and league shape but draft at their own spice
    # level, and never inherit your `my_picks` keeper overrides.
    try:
        bot_draft = DraftConfig.from_spice_level(args.bot_spice)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    bot_cfg = Config.load(args.config)
    bot_cfg.draft = dc_replace(
        bot_draft,
        num_teams=cfg.draft.num_teams, rounds=cfg.draft.rounds, order=cfg.draft.order,
        position_caps=dict(cfg.draft.position_caps),
        position_targets=dict(cfg.draft.position_targets),
        board_csv=list(cfg.draft.board_csv), my_picks=[],
    )

    rng = random.Random(args.seed)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()  # a mock is a fresh draft, never appended to a stale one

    print(
        f"Mock draft: {cfg.draft.num_teams} teams x {cfg.draft.rounds} rounds, "
        f"you at slot {cfg.draft.my_slot}, bots at spice {args.bot_spice}"
        f"{' (auto)' if args.auto else ''}"
    )
    for i in range(1, args.runs + 1):
        if args.runs > 1:
            print(f"\n--- run {i}/{args.runs} ---")
        draft = run_draft(args, cfg, board, bot_cfg, rng, i)
        print(f"  your roster: {_summary(draft)}")

    print(f"\nlog: {log_path}   reports: draft/reports/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
