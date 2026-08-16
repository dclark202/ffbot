#!/usr/bin/env python3
"""Was that pick right? Force each option at one pick and replay the rest.

    python scripts/draft_counterfactual.py --pick 38 --slot 11
    python scripts/draft_counterfactual.py --pick 38 --slot 11 --log draft_log.jsonl --top 8

Answers the question a draft report raises but cannot settle on its own.
The report shows what the engine recommended and what you took; this plays
the draft forward from that pick under each alternative and scores the
roster each one ends up with, using `lineup.optimize` -- the same objective
the engine is trying to maximize. So it holds the engine to its own claim
rather than to an opinion.

Opponents keep their real picks throughout. When a counterfactual has taken
a player an opponent wanted, that opponent falls back to best-available by
ADP rather than being skipped: skipping would shorten the draft and drift
every later pick number, which is subtle enough that an earlier version of
this analysis silently reported an identical total for every option.

Two things it cannot tell you, both worth internalizing before acting on a
number it prints.

These are PROJECTED points, not realized ones, so it measures "which pick
best serves the projections you drafted from", not "which pick won you the
season".

More importantly, the result is CHAOTIC at the individual-option level.
Changing one pick changes the next engine pick, which changes the one after
that, and ten picks later two runs that differ only in bookkeeping details
can rank the same option first or last -- observed directly: two
implementations of this analysis agreed that receivers beat running backs
at a particular pick, and disagreed completely about where a tight end
belonged. Trust a direction that survives across POSITIONS and across runs;
do not trust the ordering of two options a few points apart, and never tune
a weight to move one of them.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config, to_player  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.draft import DraftState, recommend  # noqa: E402
from ffbot.lineup import optimize  # noqa: E402
from scripts.draft_report import _read_log  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", default="draft_log.jsonl", help="(default: %(default)s)")
    p.add_argument("--pick", type=int, required=True, help="overall pick number to re-decide")
    p.add_argument("--slot", type=int, default=None, help="your draft slot")
    p.add_argument("--top", type=int, default=8, help="how many options to try (default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--session", type=int, default=-1, help="which draft session in the log (default: last)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    if args.slot is not None:
        cfg.draft.my_slot = args.slot

    segments = _read_log(Path(args.log))
    if not segments:
        print(f"error: no draft sessions found in {args.log}", file=sys.stderr)
        return 1
    segment = segments[args.session]

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            board = load_board_from_config(cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def fresh() -> DraftState:
        return DraftState(
            board=board, num_teams=cfg.draft.num_teams, my_slot=cfg.draft.my_slot,
            rounds=cfg.draft.rounds, roster_positions=cfg.roster_positions,
            my_picks_override=list(cfg.draft.my_picks), order=cfg.draft.order,
        )

    def fallback(state: DraftState) -> str | None:
        pool = [b for b in board.players if b.key not in state.taken_keys() and b.adp is not None]
        return min(pool, key=lambda b: b.adp).key if pool else None

    def play(force_key: str) -> tuple[float, dict[str, int]]:
        state = fresh()
        for entry in segment["picks"]:
            current = state.current_pick()
            mine = entry.get("mine")
            is_mine = mine if mine is not None else (current in state.my_picks())
            if is_mine:
                if current == args.pick:
                    key = force_key
                else:
                    recs = recommend(state, cfg, limit=1)
                    key = recs[0].player.key if recs else fallback(state)
            else:
                key = entry.get("key")
            if key is None or key in state.taken_keys():
                key = fallback(state)
            if key is None:
                break
            state.record(key, mine=is_mine, source="api")

        roster = state.my_roster()
        plan = optimize(
            [to_player(bp, i) for i, bp in enumerate(roster, start=1)],
            cfg.roster_positions, None, cfg,
        )
        counts: dict[str, int] = {}
        for bp in roster:
            counts[bp.position] = counts.get(bp.position, 0) + 1
        return sum(p.projected_points for _slot, p in plan.assignments), counts

    # Rewind to the pick under review and ask what was on the table.
    state = fresh()
    for entry in segment["picks"]:
        if state.current_pick() >= args.pick:
            break
        key = entry.get("key")
        if key is not None and key in state.taken_keys():
            key = fallback(state)
        state.record(key, mine=entry.get("mine"), source="api")

    if state.current_pick() != args.pick:
        print(
            f"error: replay reached pick {state.current_pick()}, not {args.pick} — "
            "is --session/--slot right for this log?",
            file=sys.stderr,
        )
        return 1

    roster_before = [f"{b.position} {b.name}" for b in state.my_roster()]
    next_pick = state.next_my_pick_after(args.pick)
    print(f"pick {args.pick} (round {(args.pick - 1) // cfg.draft.num_teams + 1}), slot {cfg.draft.my_slot}")
    print(f"  roster before: {roster_before or '(empty)'}")
    if next_pick:
        print(f"  next turn: pick {next_pick} ({next_pick - args.pick} picks away)")

    options = [r.player for r in recommend(state, cfg, limit=args.top)]
    if not options:
        print("error: no candidates at that pick", file=sys.stderr)
        return 1

    rows = []
    for bp in options:
        total, counts = play(bp.key)
        rows.append((total, bp, counts))
    rows.sort(key=lambda r: -r[0])

    best = rows[0][0]
    print()
    print(f"  {'forced pick':32} {'lineup pts':>11} {'vs best':>9}  final roster")
    for total, bp, counts in rows:
        print(f"  {bp.position:3} {bp.name[:28]:28} {total:>11.1f} {total - best:>+9.1f}  {counts}")
    print()
    print("Projected points, not realized -- a ~1% spread is inside projection noise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
