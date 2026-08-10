#!/usr/bin/env python3
"""Sweep `SeasonConfig` weight grids over the weekly lineup replayer, with a
train/test season split enforced — the tuning HARNESS for B5, built now but
NOT run to a conclusion here (see docs/BACKTEST.md's milestones: re-deriving
`SPICE_PRESETS` is deliberately out of scope for this pass).

    python scripts/backtest_tune.py --train 2021-2022 --test 2023-2024 --grid vegas_weight=0,0.1,0.2,0.3
    python scripts/backtest_tune.py --train 2021-2023 --test 2024 \\
        --grid vegas_weight=0.1,0.2 --grid weather_weight=0,0.2

Every combination in the (cartesian-product) `--grid` is replayed on BOTH
`--train` and `--test` seasons, agent vs. control, and printed side by side.
This tool refuses (raises) if `--train`/`--test` share a season — the
train/test split docs/BACKTEST.md's statistics protocol calls for and
`scripts/backtest_lineup.py`'s own sweeps have never enforced.

IMPORTANT — printing every cell's TEST column is an exploration aid, not a
validated result. The statistics protocol's actual discipline is: pick a
cell using the TRAIN column ONLY, then report that one cell's TEST result,
once. Choosing whichever cell's TEST column looks best after seeing all of
them is exactly the leakage the split exists to prevent — this script does
not (and should not) make that selection for you.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.metrics import block_bootstrap_mean_ci, paired_deltas  # noqa: E402
from ffbot.backtest.replay import replay  # noqa: E402
from ffbot.config import Config, SeasonConfig  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", required=True, help='seasons to tune on, e.g. "2021-2022"')
    p.add_argument("--test", required=True, help='held-out seasons to report on, e.g. "2023-2024"')
    p.add_argument("--weeks", default="1-15", help="(default: %(default)s)")
    p.add_argument(
        "--grid", action="append", required=True, metavar="KEY=V1,V2,...",
        help="a SeasonConfig field and its sweep values; repeatable for a cartesian-product grid",
    )
    p.add_argument("--source", choices=["naive", "ecr"], default="ecr", help="(default: %(default)s)")
    p.add_argument("--rosters", type=int, default=200, help="sampled rosters per (season, week) (default: %(default)s)")
    p.add_argument("--seed", type=int, default=11, help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    return p.parse_args(argv)


def _parse_grid(specs: list[str]) -> list[dict[str, float]]:
    """`["vegas_weight=0,0.1", "weather_weight=0,0.2"]` -> every combination
    as a `{field: value}` override dict, via `itertools.product` over each
    field's own value list."""
    fields: list[str] = []
    value_lists: list[list[float]] = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"--grid entry {spec!r} must be KEY=V1,V2,...")
        key, raw_values = spec.split("=", 1)
        fields.append(key.strip())
        value_lists.append([float(v) for v in raw_values.split(",") if v.strip() != ""])

    combos = []
    for values in itertools.product(*value_lists):
        combos.append(dict(zip(fields, values)))
    return combos


def _mean_delta(seasons: list[int], weeks: list[int], cfg: Config, source: str, rosters: int, seed: int, cache_dir: str):
    r = replay(seasons, weeks, cfg, source, rosters, seed=seed, cache_dir=cache_dir)
    if not r.decisions:
        return 0.0, 0.0, 0.0, 0
    deltas = paired_deltas(r.decisions, "agent", "control")
    blocks = [d.block_key for d in r.decisions]
    mean, lo, hi = block_bootstrap_mean_ci(deltas, blocks, seed=seed)
    return mean, lo, hi, len(r.decisions)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    train_seasons = parse_seasons(args.train)
    test_seasons = parse_seasons(args.test)
    weeks = parse_seasons(args.weeks)

    overlap = set(train_seasons) & set(test_seasons)
    if overlap:
        print(f"error: --train and --test share season(s) {sorted(overlap)} — refusing "
              "(a tuning result reported on the season it was chosen against is not a real result)",
              file=sys.stderr)
        return 1
    if not train_seasons or not test_seasons or not weeks:
        print("error: --train/--test/--weeks produced no values", file=sys.stderr)
        return 1

    try:
        combos = _parse_grid(args.grid)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not combos:
        print("error: --grid produced no combinations", file=sys.stderr)
        return 1

    base_cfg = Config.load(args.config)
    print(f"{len(combos)} grid cell(s); train={train_seasons} test={test_seasons}\n")
    header = ", ".join(sorted({k for c in combos for k in c}))
    print(f"{header:<40} {'train delta':>12} {'train CI':>18} {'test delta':>12} {'test CI':>18}")

    for overrides in combos:
        cfg = Config.load(args.config)
        cfg.season = SeasonConfig(**overrides)

        tm, tlo, thi, tn = _mean_delta(train_seasons, weeks, cfg, args.source, args.rosters, args.seed, args.cache_dir)
        em, elo, ehi, en = _mean_delta(test_seasons, weeks, cfg, args.source, args.rosters, args.seed, args.cache_dir)

        label = ", ".join(f"{k}={v}" for k, v in overrides.items())
        print(
            f"{label:<40} {tm:>+12.3f} {'[' + format(tlo, '+.2f') + ', ' + format(thi, '+.2f') + ']':>18} "
            f"{em:>+12.3f} {'[' + format(elo, '+.2f') + ', ' + format(ehi, '+.2f') + ']':>18}"
        )

    print(
        "\nReminder: picking the best-looking TEST cell above and reporting it as a validated "
        "result is the leakage the train/test split exists to prevent. Pick using the TRAIN "
        "column only, then report that one cell's TEST result, once."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
