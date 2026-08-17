#!/usr/bin/env python3
"""Generate a training pack: synthetic draft situations for a human reviewer
who has no stake in the outcome and no view of the engine's own reasoning.

    python scripts/make_training_pack.py --count 30 --drafts 6 --seed 7 \
        --out training/packs/dad-01.json --export training/dad-01.html --label "Round 1"

Every tuning conversation this repo has had started from a sample of one --
a single mock draft where a recommendation looked wrong (see the
single-draft-evidence memory: 0-for-3 on changes that measured
neutral-to-negative once actually backtested). This is a second, cheap
source of judgment at volume: run several full mock drafts with bots in
EVERY seat (including "mine", at a different spice level than the tool's
own config, so a partial roster isn't just a replay of the engine's own
choices), freeze the recommendation table at each of my picks the exact
way the live draft room would show it, and hand a stratified sample of
those situations to a reviewer as a standalone HTML page
(`ffbot/training_export.py`) they can open with no server and no Python.

Nothing here changes a recommendation, a weight, or the engine -- this only
reads the board through the ordinary config path
(`board.load_board_from_config`) and reuses `scripts/mock_draft.py`'s own
bot-picking (`_bot_pick`). A pack is a hypothesis-generation tool; see
docs/dev/TRAINING.md for the rule that any finding still has to clear
`scripts/backtest_draft.py` before a weight moves.
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import training  # noqa: E402
from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config, DraftConfig  # noqa: E402
from ffbot.draft import DraftState  # noqa: E402
from ffbot.draft_ui import UiState  # noqa: E402
from ffbot.training_export import write_standalone  # noqa: E402
from scripts.mock_draft import _bot_pick  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--board", action="append", default=None, help="FantasyPros CSV path (repeatable); overrides draft.board_csv")
    p.add_argument("--count", type=int, default=30, help="scenarios in the pack (default: %(default)s)")
    p.add_argument("--drafts", type=int, default=6, help="simulated drafts to draw candidates from (default: %(default)s)")
    p.add_argument("--rounds-range", default="1-14", help="MIN-MAX: only picks in this round range become candidates (default: %(default)s)")
    p.add_argument("--teams", type=int, default=None, help="override draft.num_teams")
    p.add_argument("--slot", type=int, default=None, help="fix my draft slot across every simulated draft (default: vary it)")
    p.add_argument("--seed", type=int, default=None, help="base seed -- draft i uses seed+i (default: unseeded)")
    p.add_argument("--bot-spice", type=int, default=1, help="spice level the opponent bots draft at (default: %(default)s)")
    p.add_argument("--my-spice", type=int, default=2, help="spice level the BOT PLAYING MY SEAT drafts at -- deliberately not the config's own level, so situations aren't a replay of the engine's own choices (default: %(default)s)")
    p.add_argument("--bot-window", type=int, default=3, help="every bot picks among its top N recs, so runs differ (default: %(default)s)")
    p.add_argument("--blind", action="store_true", help="pack tells the reviewer page to hide Val/Δ/P/Why until they rank a player")
    p.add_argument("--label", default="", help="human-readable label shown at the top of the reviewer page")
    p.add_argument("--pack-id", default=None, help="default: derived from --out's filename")
    p.add_argument("--out", default="training/packs/pack.json", help="pack JSON path (default: %(default)s)")
    p.add_argument("--export", default=None, help="also write a standalone reviewer HTML file here")
    return p.parse_args(argv)


def _parse_range(spec: str) -> tuple[int, int]:
    try:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
    except ValueError:
        raise SystemExit(f"error: --rounds-range must be MIN-MAX (got {spec!r})")
    if lo < 1 or hi < lo:
        raise SystemExit(f"error: --rounds-range must be an increasing range >= 1 (got {spec!r})")
    return lo, hi


def _build_seat_cfg(base_cfg: Config, spice: int, num_teams: int, rounds: int, order: str) -> Config:
    """A bot-driving `Config` at `spice`, sharing league shape with
    `base_cfg` but never inheriting keeper overrides (`my_picks`) -- the
    same construction `scripts/mock_draft.py`'s `main()` uses for its own
    opponent bots, reused here for both opponents and my own seat.

    `dataclasses.replace(base_cfg)` with no field overrides is a shallow
    copy -- cheaper than re-parsing config.yml the way `mock_draft.py`'s
    `Config.load(args.config)` does, and safe here because only `.draft`
    (itself replaced wholesale, not mutated) differs per seat.
    """
    seat_draft = DraftConfig.from_spice_level(spice)
    cfg = dataclasses.replace(base_cfg)
    cfg.draft = dataclasses.replace(
        seat_draft,
        num_teams=num_teams, rounds=rounds, order=order,
        position_caps=dict(base_cfg.draft.position_caps),
        position_targets=dict(base_cfg.draft.position_targets),
        board_csv=list(base_cfg.draft.board_csv),
        my_picks=[],
    )
    return cfg


def simulate_one_draft(
    draft_index: int,
    cfg: Config,
    board,
    opponent_cfg: Config,
    my_seat_cfg: Config,
    my_slot: int,
    rng: random.Random,
    window: int,
    round_range: tuple[int, int],
) -> list[dict]:
    """Walk one full mock draft forward, every seat a bot, and return one
    candidate scenario per pick landing in my seat within `round_range`.

    The recommendation table captured at each candidate is computed under
    `cfg` -- the real, tool-configured engine -- via `UiState(draft, cfg)`;
    only the CHOICE of who my bot actually takes uses `my_seat_cfg`. That
    split is the whole point: a reviewer sees the actual engine's advice,
    against a roster that isn't simply that same advice played out.
    """
    draft = DraftState(
        board=board, num_teams=cfg.draft.num_teams, my_slot=my_slot,
        rounds=cfg.draft.rounds, roster_positions=cfg.roster_positions,
        order=cfg.draft.order,
    )
    ui_state = UiState(draft=draft, cfg=cfg)
    ui_state.sync_status = "off"
    ui_state.sync_reason = "synthetic training draft -- bots, not Sleeper"

    lo, hi = round_range
    candidates: list[dict] = []
    total_picks = draft.num_teams * draft.rounds
    my_picks = set(draft.my_picks())
    scenario_seq = 0

    while draft.current_pick() <= total_picks:
        pick_no = draft.current_pick()
        if pick_no in my_picks:
            round_ = (pick_no - 1) // draft.num_teams + 1
            if lo <= round_ <= hi:
                scenario_seq += 1
                candidates.append(
                    training.build_scenario(
                        ui_state,
                        scenario_id=f"d{draft_index}-p{pick_no}",
                        source_draft=f"d{draft_index}",
                    )
                )
            key = _bot_pick(draft, my_seat_cfg, my_slot, rng, window)
            if key is None:
                break
            draft.record(key, mine=True)
        else:
            from ffbot.draft import team_slot_at

            slot = team_slot_at(pick_no, draft.num_teams, draft.order)
            key = _bot_pick(draft, opponent_cfg, slot, rng, window)
            if key is None:
                break
            draft.record(key, mine=False)

    return candidates


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    round_range = _parse_range(args.rounds_range)

    cfg = Config.load(args.config)
    if args.teams is not None:
        cfg.draft.num_teams = args.teams

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            board = load_board_from_config(cfg, args.board)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    opponent_cfg = _build_seat_cfg(cfg, args.bot_spice, cfg.draft.num_teams, cfg.draft.rounds, cfg.draft.order)
    my_seat_cfg = _build_seat_cfg(cfg, args.my_spice, cfg.draft.num_teams, cfg.draft.rounds, cfg.draft.order)

    rng = random.Random(args.seed)
    all_candidates: list[dict] = []
    for i in range(1, args.drafts + 1):
        slot = args.slot if args.slot is not None else ((i - 1) % cfg.draft.num_teams) + 1
        draft_seed = None if args.seed is None else args.seed + i
        draft_rng = random.Random(draft_seed) if draft_seed is not None else rng
        candidates = simulate_one_draft(
            i, cfg, board, opponent_cfg, my_seat_cfg, slot, draft_rng, args.bot_window, round_range,
        )
        print(f"  draft {i}: slot {slot}, {len(candidates)} candidate picks in rounds {round_range[0]}-{round_range[1]}")
        all_candidates.extend(candidates)

    if not all_candidates:
        print("error: no candidate scenarios were generated -- widen --rounds-range or check --drafts", file=sys.stderr)
        return 1

    chosen = training.stratify(all_candidates, args.count, seed=args.seed)
    bucket_counts: dict[str, int] = {}
    for c in chosen:
        bucket_counts[c["round_bucket"]] = bucket_counts.get(c["round_bucket"], 0) + 1
    print(f"selected {len(chosen)} of {len(all_candidates)} candidates: {bucket_counts}")

    pack_id = args.pack_id or Path(args.out).stem
    pack = training.build_pack(
        chosen, cfg, board,
        pack_id=pack_id,
        label=args.label,
        blind=args.blind,
        generator={
            "drafts": args.drafts,
            "bot_spice": args.bot_spice,
            "my_spice": args.my_spice,
            "bot_window": args.bot_window,
            "rounds_range": list(round_range),
            "seed": args.seed,
        },
    )

    out_path = training.write_pack(pack, args.out)
    if out_path is None:
        print(f"error: could not write {args.out}", file=sys.stderr)
        return 1
    print(f"pack: {out_path}")

    if args.export:
        try:
            export_path = write_standalone(pack, args.export)
        except OSError as exc:
            print(f"error: could not write {args.export} ({exc})", file=sys.stderr)
            return 1
        size_kb = export_path.stat().st_size / 1024
        print(f"export: {export_path} ({size_kb:.0f} KB) -- open directly in a browser, no server needed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
