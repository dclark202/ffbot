#!/usr/bin/env python3
"""Replay the weekly lineup optimizer against real NFL history and report
whether it beats a frozen-projection control (see docs/BACKTEST.md,
milestones B2/B3).

    python scripts/backtest_lineup.py --seasons 2023 --source naive --rosters 200 --seed 11
    python scripts/backtest_lineup.py --seasons 2021-2024 --source ecr --rosters 500 --seed 11
    python scripts/backtest_lineup.py --seasons 2023 --source ecr --spice-level 2 --weeks 4-12
    python scripts/backtest_lineup.py --seasons 2023 --source ecr --signals historical_form

For each `(season, week)` in `--seasons x --weeks`, samples `--rosters`
independent synthetic rosters (`ffbot.backtest.rosters`), builds all five
baselines (oracle / control / agent / consensus / random_legal — see
`ffbot.backtest.baselines`), and grades each against real box scores
(`ffbot.history.actuals.week_actuals`). Prints:

  1. Per-baseline mean lineup efficiency (% of oracle captured).
  2. The agent-vs-control paired delta, as a block-bootstrap CONFIDENCE
     INTERVAL (never a bare point estimate) plus the discordant-pair count —
     per docs/BACKTEST.md's pre-registered statistics protocol, an
     underpowered result must read as underpowered, not as a win.
  3. When both engines have data for the requested seasons, the naive-vs-ecr
     projection agreement (Pearson r on overlapping player-weeks) — the open
     question docs/BACKTEST.md raises about whether the two engines actually
     agree where their coverage overlaps (2021-2024).

This script requires the historical data already be cached — run
`scripts/history_fetch.py --seasons ...` first for anything not already
downloaded.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.metrics import (  # noqa: E402
    block_bootstrap_mean_ci,
    discordant_deltas,
    lineup_efficiency,
    lineups_differ,
    paired_deltas,
)
from ffbot.backtest.replay import BASELINE_NAMES, replay  # noqa: E402
from ffbot.config import Config, SeasonConfig  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.openmeteo import open_meteo_game_weather  # noqa: E402
from ffbot.history.projections import ECR_CLEAN_SEASONS, ecr_projections, naive_projections  # noqa: E402
from ffbot.history.signals import combine_providers, historical_form, usage_form  # noqa: E402

GAME_PROVIDERS = {"openmeteo": open_meteo_game_weather}

# Every signal provider callable by --signals, by name. A stats-derived
# proxy measures whether the volatility/upside MECHANISM pays off, not
# whether researched intel is any good -- see ffbot/history/signals.py.
SIGNAL_PROVIDERS = {"historical_form": historical_form, "usage_form": usage_form}


def _build_signal_provider(names: list[str] | None):
    """`--signals a,b` -> `combine_providers(SIGNAL_PROVIDERS[a],
    SIGNAL_PROVIDERS[b])`; `--signals a` -> just `SIGNAL_PROVIDERS[a]`
    (skips the wrapper for the single-provider case); omitted -> `None`,
    same "every signal-dependent dial stays inert" no-op as before this
    existed."""
    if not names:
        return None
    if len(names) == 1:
        return SIGNAL_PROVIDERS[names[0]]
    return combine_providers(*(SIGNAL_PROVIDERS[n] for n in names))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2023" or "2021-2024"')
    p.add_argument("--weeks", default="1-15", help='e.g. "1-15" (default: %(default)s)')
    p.add_argument("--source", choices=["naive", "ecr"], required=True, help="projection engine")
    p.add_argument("--rosters", type=int, default=200, help="sampled rosters per (season, week) (default: %(default)s)")
    p.add_argument("--seed", type=int, default=11, help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--spice-level", type=int, default=None, help="override config.yml's season.spice_level (1-5)")
    p.add_argument("--no-agreement", action="store_true", help="skip the naive-vs-ecr agreement section")
    p.add_argument(
        "--signals", default=None, metavar="NAME[,NAME...]",
        help=f"comma-separated signal provider(s) to merge in (choices: {sorted(SIGNAL_PROVIDERS)}; "
             "see ffbot/history/signals.py); omitted = every signal-dependent spice dial "
             "(volatility/upside_lean/usage) stays structurally inert, same as B3",
    )
    p.add_argument(
        "--game-weather", choices=sorted(GAME_PROVIDERS), default=None,
        help="game-level weather enrichment (see ffbot/history/openmeteo.py); omitted = "
             "games.csv's own wind/roof-derived weather only, same as before this existed",
    )
    return p.parse_args(argv)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Plain-stdlib Pearson correlation — `statistics.correlation` needs
    Python 3.10+; this avoids pinning a minimum version for one report line."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _report_agreement(seasons: list[int], weeks: list[int], cfg: Config, cache_dir: str) -> None:
    overlap = [s for s in seasons if s in ECR_CLEAN_SEASONS]
    if not overlap:
        print("\nNAIVE vs ECR agreement: skipped — no requested season is in ECR's clean window "
              f"({ECR_CLEAN_SEASONS[0]}-{ECR_CLEAN_SEASONS[-1]}).")
        return

    xs: list[float] = []
    ys: list[float] = []
    for season in overlap:
        for week in weeks:
            naive = naive_projections(season, week, cfg, cache_dir=cache_dir)
            try:
                ecr = ecr_projections(season, week, cfg, cache_dir=cache_dir)
            except ValueError:
                continue
            for key, ecr_pts in ecr.items():
                naive_pts = naive.get(key)
                if naive_pts is not None:
                    xs.append(naive_pts)
                    ys.append(ecr_pts)

    r = _pearson(xs, ys)
    print(f"\nNAIVE vs ECR agreement ({', '.join(str(s) for s in overlap)}, n={len(xs)} player-weeks):")
    if r is None:
        print("  not enough overlapping data to compute a correlation")
    else:
        print(f"  Pearson r = {r:.3f}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    weeks = parse_seasons(args.weeks)
    if not seasons or not weeks:
        print("error: --seasons/--weeks produced no values", file=sys.stderr)
        return 1

    cfg = Config.load(args.config)
    if args.spice_level is not None:
        cfg.season = SeasonConfig.from_spice_level(args.spice_level)

    signal_names = [s.strip() for s in args.signals.split(",") if s.strip()] if args.signals else []
    unknown = [n for n in signal_names if n not in SIGNAL_PROVIDERS]
    if unknown:
        print(f"error: unknown --signals {unknown} (choices: {sorted(SIGNAL_PROVIDERS)})", file=sys.stderr)
        return 1
    signal_provider = _build_signal_provider(signal_names)
    game_provider = GAME_PROVIDERS[args.game_weather] if args.game_weather else None
    print(
        f"Replaying {len(seasons)} season(s) x {len(weeks)} week(s), "
        f"{args.rosters} roster(s)/week, source={args.source}, spice_level={cfg.season.spice_level}, "
        f"signals={args.signals or 'none'}, game_weather={args.game_weather or 'none'}"
    )
    result = replay(
        seasons, weeks, cfg, args.source, args.rosters, seed=args.seed,
        cache_dir=args.cache_dir, signal_provider=signal_provider, game_provider=game_provider,
    )
    n = len(result.decisions)
    print(f"{n} (season, week, roster) decisions replayed.\n")
    if n == 0:
        print("nothing to report", file=sys.stderr)
        return 1

    print("Lineup efficiency (mean % of oracle points captured):")
    for name in BASELINE_NAMES:
        if name == "oracle":
            continue
        effs = [lineup_efficiency(d.points[name], d.points["oracle"]) for d in result.decisions]
        print(f"  {name:<14} {statistics.fmean(effs):.3f}")

    deltas = paired_deltas(result.decisions, "agent", "control")
    blocks = [d.block_key for d in result.decisions]
    mean, lo, hi = block_bootstrap_mean_ci(deltas, blocks, seed=args.seed)
    print(f"\nagent vs control, all decisions: mean delta = {mean:+.2f} pts, 95% CI [{lo:+.2f}, {hi:+.2f}]")

    disc = discordant_deltas(result.decisions, result.lineups, "agent", "control")
    disc_blocks = [
        d.block_key
        for d, ls in zip(result.decisions, result.lineups)
        if lineups_differ(ls["agent"], ls["control"])
    ]
    if disc:
        d_mean, d_lo, d_hi = block_bootstrap_mean_ci(disc, disc_blocks, seed=args.seed)
        print(
            f"agent vs control, DISCORDANT ONLY ({len(disc)}/{n} decisions where the spice weights "
            f"actually flipped a start/sit call): mean delta = {d_mean:+.2f} pts, 95% CI [{d_lo:+.2f}, {d_hi:+.2f}]"
        )
    else:
        print("agent vs control: 0 discordant decisions — the spice weights never flipped a call in this sample.")

    if not args.no_agreement:
        _report_agreement(seasons, weeks, cfg, args.cache_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
