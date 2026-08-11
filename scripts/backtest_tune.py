#!/usr/bin/env python3
"""Sweep `SeasonConfig` weight grids over the weekly lineup replayer, with a
train/test season split enforced — the tuning HARNESS for B5.

    python scripts/backtest_tune.py --train 2021-2022 --test 2023-2024 --grid vegas_weight=0,0.1,0.2,0.3
    python scripts/backtest_tune.py --train 2021-2023 --test 2024 \\
        --grid vegas_weight=0.1,0.2 --grid weather_weight=0,0.2
    python scripts/backtest_tune.py --train 2021-2022 --test 2023-2024 \\
        --grid volatility_weight=0,0.2,0.4 --signals historical_form
    python scripts/backtest_tune.py --train 2021-2023 --test 2024 --train-only \\
        --grid spice_level=1,2,3,4,5 --signals historical_form,usage_form

Every combination in the (cartesian-product) `--grid` is replayed on BOTH
`--train` and `--test` seasons, agent vs. control, and printed side by side —
both the "all decisions" delta and the "discordant only" delta (the pairs the
swept weights actually flipped; see docs/BACKTEST.md's statistics protocol,
rule 4). This tool refuses (raises) if `--train`/`--test` share a season — the
train/test split the statistics protocol calls for and
`scripts/backtest_lineup.py`'s own sweeps have never enforced.

Each grid cell starts from `--config`'s own `SeasonConfig` (its
`spice_level` preset plus any `config.yml`/`config.local.yml` hand
overrides) and overlays ONLY the fields named in `--grid` on top — every
dial not swept stays at its configured production value, not at the bare
dataclass zero. This deliberately differs from `scripts/backtest_lineup.py
--spice-level`, which replaces the whole `SeasonConfig`; here the question
is "what does changing dial X do to the CURRENT preset," not "what does X
do starting from an all-zero no-op agent."

ONE EXCEPTION: sweeping `spice_level` itself special-cases to
`SeasonConfig.from_spice_level(level, **rest_of_the_combo)` rather than the
overlay above — `spice_level` doesn't have a value of its own to overlay,
it REDEFINES every derived dial, so overlaying it onto the base config's
already-resolved numbers would be a no-op on everything but the field's own
bookkeeping. This is what makes `--grid spice_level=1,2,3,4,5` a real ladder
comparison.

`--signals` must be passed to sweep `volatility_weight`/`upside_lean_weight`/
`usage_weight` (or any future signal-backed dial) at all — omitted,
`ffbot.backtest.replay`'s default `signal_provider=None` means those dials
stay structurally inert (every value in the grid scores identically), the
same dead-dial trap `ffbot/history/signals.py` was written to fix elsewhere.

`--train-only` skips the test replay entirely — halves cost while exploring,
and structurally prevents a held-out season from being looked at more than
once, rather than merely asking nicely not to.

`--out PATH` writes every cell (both columns, both metrics, the full CI) as
JSON — B6's sweeps never persisted raw output, so every number from that
milestone survives only as hand-copied prose in docs/BACKTEST.md. Don't
repeat that for B5.

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
import json
import sys
from dataclasses import fields, replace as dc_replace
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.metrics import (  # noqa: E402
    block_bootstrap_mean_ci,
    discordant_deltas,
    lineups_differ,
    paired_deltas,
)
from ffbot.backtest.replay import ReplayResult, replay  # noqa: E402
from ffbot.config import Config, ConfigError, SeasonConfig  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.openmeteo import open_meteo_game_weather  # noqa: E402
from ffbot.history.signals import (  # noqa: E402
    combine_providers,
    historical_form,
    scoring_form,
    usage_divergence,
    usage_form,
)

# Same registry convention as scripts/backtest_lineup.py's SIGNAL_PROVIDERS.
SIGNAL_PROVIDERS = {
    "historical_form": historical_form,
    "usage_form": usage_form,
    "scoring_form": scoring_form,
    "usage_divergence": usage_divergence,
}
GAME_PROVIDERS = {"openmeteo": open_meteo_game_weather}

# Dials that read nothing without a SignalProvider supplying the underlying
# WeeklyPlayerIntel field — sweeping any of these without --signals is a
# silent no-op, not a real experiment. See ffbot/history/signals.py.
SIGNAL_DEPENDENT_FIELDS = {
    "volatility_weight",
    "upside_lean_weight",
    "usage_weight",
    "momentum_weight",
    "divergence_weight",
}


def _build_signal_provider(names: list[str]):
    """Same combinator as scripts/backtest_lineup.py's — see there."""
    if not names:
        return None
    if len(names) == 1:
        return SIGNAL_PROVIDERS[names[0]]
    return combine_providers(*(SIGNAL_PROVIDERS[n] for n in names))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", required=True, help='seasons to tune on, e.g. "2021-2022"')
    p.add_argument("--test", required=True, help='held-out seasons to report on, e.g. "2023-2024"')
    p.add_argument("--weeks", default="1-15", help="(default: %(default)s)")
    p.add_argument(
        "--grid", action="append", required=True, metavar="KEY=V1,V2,...",
        help="a SeasonConfig field and its sweep values; repeatable for a cartesian-product grid "
             "(spice_level is special-cased — see module docstring)",
    )
    p.add_argument("--source", choices=["naive", "ecr"], default="ecr", help="(default: %(default)s)")
    p.add_argument("--rosters", type=int, default=200, help="sampled rosters per (season, week) (default: %(default)s)")
    p.add_argument("--seed", type=int, default=11, help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument(
        "--signals", default=None, metavar="NAME[,NAME...]",
        help=f"comma-separated signal provider(s) to merge in (choices: {sorted(SIGNAL_PROVIDERS)}; "
             "see ffbot/history/signals.py); required for a sweep of any of "
             f"{sorted(SIGNAL_DEPENDENT_FIELDS)} to mean anything — omitted, those dials are "
             "structurally inert and every grid value scores identically",
    )
    p.add_argument(
        "--game-weather", choices=sorted(GAME_PROVIDERS), default=None,
        help="game-level weather enrichment (see ffbot/history/openmeteo.py); required for a "
             "game_script_weight sweep against RICHER weather to mean anything beyond games.csv's "
             "own wind/roof coverage",
    )
    p.add_argument(
        "--train-only", action="store_true",
        help="skip the --test replay entirely — halves cost while exploring and structurally "
             "prevents a held-out season from being looked at more than once",
    )
    p.add_argument(
        "--out", default=None, metavar="PATH",
        help="write every cell's full result (both columns, all + discordant, CI) as JSON",
    )
    return p.parse_args(argv)


def _apply_overrides(base: SeasonConfig, overrides: dict[str, float]) -> SeasonConfig:
    """`base` (the config's own preset) with only `overrides`'s fields
    changed — every other dial stays at its configured value. `spice_level`
    is special-cased to `from_spice_level` (see module docstring); every
    other key turns an unknown `--grid` field into a `ConfigError` naming
    the valid fields, same contract as `ffbot.config._construct`."""
    if "spice_level" in overrides:
        level = int(overrides["spice_level"])
        rest = {k: v for k, v in overrides.items() if k != "spice_level"}
        try:
            return SeasonConfig.from_spice_level(level, **rest)
        except (ValueError, TypeError) as exc:
            valid = ", ".join(f.name for f in fields(SeasonConfig))
            raise ConfigError(f"--grid: {exc}. Valid keys: {valid}") from exc
    try:
        return dc_replace(base, **overrides)
    except TypeError as exc:
        valid = ", ".join(f.name for f in fields(SeasonConfig))
        raise ConfigError(f"--grid: {exc}. Valid keys: {valid}") from exc


def _parse_grid(specs: list[str]) -> list[dict[str, float]]:
    """`["vegas_weight=0,0.1", "weather_weight=0,0.2"]` -> every combination
    as a `{field: value}` override dict, via `itertools.product` over each
    field's own value list."""
    keys: list[str] = []
    value_lists: list[list[float]] = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"--grid entry {spec!r} must be KEY=V1,V2,...")
        key, raw_values = spec.split("=", 1)
        keys.append(key.strip())
        value_lists.append([float(v) for v in raw_values.split(",") if v.strip() != ""])

    combos = []
    for values in itertools.product(*value_lists):
        combos.append(dict(zip(keys, values)))
    return combos


def _cell_stats(r: ReplayResult, seed: int) -> dict:
    """Both the "all decisions" and "discordant only" agent-vs-control
    block-bootstrap deltas for one replay — see docs/BACKTEST.md's
    statistics protocol, rule 4, on why discordant-only is the metric that
    actually isolates what the swept weight did."""
    if not r.decisions:
        return {
            "all": {"mean": 0.0, "lo": 0.0, "hi": 0.0, "n": 0},
            "discordant": {"mean": 0.0, "lo": 0.0, "hi": 0.0, "n": 0},
        }
    all_deltas = paired_deltas(r.decisions, "agent", "control")
    all_blocks = [d.block_key for d in r.decisions]
    a_mean, a_lo, a_hi = block_bootstrap_mean_ci(all_deltas, all_blocks, seed=seed)

    disc_deltas = discordant_deltas(r.decisions, r.lineups, "agent", "control")
    disc_blocks = [
        d.block_key for d, ls in zip(r.decisions, r.lineups) if lineups_differ(ls["agent"], ls["control"])
    ]
    d_mean, d_lo, d_hi = block_bootstrap_mean_ci(disc_deltas, disc_blocks, seed=seed)

    return {
        "all": {"mean": a_mean, "lo": a_lo, "hi": a_hi, "n": len(r.decisions)},
        "discordant": {"mean": d_mean, "lo": d_lo, "hi": d_hi, "n": len(disc_deltas)},
    }


def _replay_stats(
    seasons: list[int], weeks: list[int], cfg: Config, source: str, rosters: int, seed: int,
    cache_dir: str, signal_provider=None, game_provider=None,
) -> dict:
    r = replay(
        seasons, weeks, cfg, source, rosters, seed=seed, cache_dir=cache_dir,
        signal_provider=signal_provider, game_provider=game_provider,
    )
    return _cell_stats(r, seed)


def _fmt(stats: dict) -> str:
    a, d = stats["all"], stats["discordant"]
    flip_pct = f"{100.0 * d['n'] / a['n']:.1f}%" if a["n"] else "n/a"
    return (
        f"all: {a['mean']:+.3f} [{a['lo']:+.2f}, {a['hi']:+.2f}] (n={a['n']})   "
        f"discordant: {d['mean']:+.3f} [{d['lo']:+.2f}, {d['hi']:+.2f}] (n={d['n']}, {flip_pct} flipped)"
    )


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

    signal_names = [s.strip() for s in args.signals.split(",") if s.strip()] if args.signals else []
    unknown = [n for n in signal_names if n not in SIGNAL_PROVIDERS]
    if unknown:
        print(f"error: unknown --signals {unknown} (choices: {sorted(SIGNAL_PROVIDERS)})", file=sys.stderr)
        return 1

    swept_fields = sorted({k for c in combos for k in c})
    signal_dependent = set(swept_fields) & SIGNAL_DEPENDENT_FIELDS
    # spice_level moves every derived dial including the signal-backed
    # ones, so it needs --signals too, same reasoning as any of them alone.
    if "spice_level" in swept_fields:
        signal_dependent = signal_dependent | {"spice_level"}
    if signal_dependent and not signal_names:
        print(
            f"error: sweeping {sorted(signal_dependent)} without --signals is a dead-dial sweep — "
            "every grid value would score identically (see ffbot/history/signals.py). "
            f"Pass --signals {{{','.join(sorted(SIGNAL_PROVIDERS))}}}.",
            file=sys.stderr,
        )
        return 1
    signal_provider = _build_signal_provider(signal_names)
    game_provider = GAME_PROVIDERS[args.game_weather] if args.game_weather else None

    base_cfg = Config.load(args.config)
    try:
        for overrides in combos:
            _apply_overrides(base_cfg.season, overrides)  # validate every combo up front
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"{len(combos)} grid cell(s); train={train_seasons} test={test_seasons if not args.train_only else '(skipped, --train-only)'}; "
        f"base spice_level={base_cfg.season.spice_level}; signals={args.signals or 'none'}; "
        f"game_weather={args.game_weather or 'none'}\n"
    )

    out_rows: list[dict] = []
    for overrides in combos:
        cfg = Config.load(args.config)
        cfg.season = _apply_overrides(cfg.season, overrides)

        def _fmt_value(v: float) -> str:
            return str(int(v)) if v == int(v) else str(v)

        label = ", ".join(f"{k}={_fmt_value(overrides[k])}" for k in swept_fields if k in overrides)
        train_stats = _replay_stats(
            train_seasons, weeks, cfg, args.source, args.rosters, args.seed, args.cache_dir,
            signal_provider, game_provider,
        )
        print(f"{label}")
        print(f"  train  {_fmt(train_stats)}")

        test_stats: Optional[dict] = None
        if not args.train_only:
            test_stats = _replay_stats(
                test_seasons, weeks, cfg, args.source, args.rosters, args.seed, args.cache_dir,
                signal_provider, game_provider,
            )
            print(f"  test   {_fmt(test_stats)}")
        print()

        out_rows.append({"overrides": overrides, "train": train_stats, "test": test_stats})

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "train_seasons": train_seasons,
                    "test_seasons": None if args.train_only else test_seasons,
                    "weeks": weeks,
                    "source": args.source,
                    "rosters": args.rosters,
                    "seed": args.seed,
                    "signals": signal_names,
                    "game_weather": args.game_weather,
                    "cells": out_rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {out_path}")

    print(
        "Reminder: picking the best-looking TEST cell above and reporting it as a validated "
        "result is the leakage the train/test split exists to prevent. Pick using the TRAIN "
        "column only, then report that one cell's TEST result, once."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
