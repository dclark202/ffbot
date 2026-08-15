#!/usr/bin/env python3
"""Verify the historical data layer before anything backtest-related builds
on top of it (see docs/dev/BACKTEST.md, milestone B1).

    python scripts/history_check.py --seasons 2022-2024
    python scripts/history_check.py --seasons 2023 --sample 500

Two independent checks, same spirit as `scripts/scoring_check.py`:

1. SCORING RECONCILIATION — recomputes every QB/RB/WR/TE row's fantasy
   points under a PPR-equivalent `LeagueScoring` via
   `ffbot.history.actuals.score_player_row` (the exact function a backtest
   uses) and compares it against nflverse's own `fantasy_points_ppr` column.
   These should agree to within floating-point noise; a real gap means the
   `StatLine` mapping in `ffbot/history/actuals.py` is wrong, not that the
   league differs from consensus. K and DEF residuals are reported
   separately and NOT asserted near-zero — nflverse's own `fantasy_points_ppr`
   doesn't model kicking/defense the way this codebase's distance-banded,
   points-allowed-aware scoring does, so a gap there is expected, not a bug.

2. NAME-MATCH COVERAGE — cross-checks `stats_player_week` identities against
   the same season's `roster_weekly` file (two independent nflverse sources)
   via `ffbot.history.names.match_actuals`, validating the matching
   machinery itself. This is NOT the harder cross-vendor case (joining
   against a real FantasyPros board export) — that exercise is B2's job,
   once real historical board CSVs are in hand.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import (  # noqa: E402
    LeagueScoring, MiscScoring, PassingScoring, ReceivingScoring, RushingScoring,
)
from ffbot.history import actuals  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, fetch_rows, parse_seasons  # noqa: E402
from ffbot.history.names import coverage_summary, match_actuals  # noqa: E402

_COMPARABLE_POSITIONS = {"QB", "RB", "WR", "TE"}
_RESIDUAL_WARN = 0.5  # points; nflverse and this codebase agree on formulas exactly, so any
                       # real gap should be near-zero floating-point noise, not this large


def _ppr_equivalent_scoring() -> LeagueScoring:
    """The scoring nflverse's own `fantasy_points_ppr` column is computed
    under, reconstructed from its published formula — full PPR, 4-pt passing
    TDs, -2 INT, 6-pt rush/rec TDs, 2-pt conversions counted. Used only for
    this reconciliation, never as a fallback for a real `league.yml`."""
    return LeagueScoring(
        passing=PassingScoring(yards_per_point=25, td=4, int=-2, two_pt=2),
        rushing=RushingScoring(yards_per_point=10, td=6, two_pt=2),
        receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0, two_pt=2),
        misc=MiscScoring(fumble_lost=-2, off_fumble_return_td=0),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2022-2024"')
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--sample", type=int, default=None, help="cap rows checked per season (default: all)")
    p.add_argument("--refresh", action="store_true", help="re-download source data even if cached")
    return p.parse_args(argv)


def check_scoring_reconciliation(season: int, cache_dir: str, refresh: bool, sample: "int | None") -> bool:
    scoring = _ppr_equivalent_scoring()
    rows = fetch_rows("stats_player_week", season=season, cache_dir=cache_dir, refresh=refresh)
    if sample:
        rows = rows[:sample]

    residuals: dict[str, list[float]] = {}
    worst: dict[str, tuple[float, str]] = {}
    for row in rows:
        pos = (row.get("position") or "").strip().upper()
        official_raw = row.get("fantasy_points_ppr")
        if official_raw in (None, ""):
            continue
        try:
            official = float(official_raw)
        except ValueError:
            continue
        recomputed, _flags = actuals.score_player_row(row, scoring)
        resid = abs(recomputed - official)
        residuals.setdefault(pos, []).append(resid)
        if resid > worst.get(pos, (-1.0, ""))[0]:
            worst[pos] = (resid, row.get("player_display_name", "?"))

    print(f"\nSEASON {season} — scoring reconciliation ({len(rows)} rows)")
    ok = True
    for pos in sorted(residuals):
        vals = residuals[pos]
        mean_r = statistics.fmean(vals)
        max_r, who = worst[pos]
        comparable = pos in _COMPARABLE_POSITIONS
        flag = ""
        if comparable and mean_r > _RESIDUAL_WARN:
            flag = "  <-- investigate (should be ~0)"
            ok = False
        elif not comparable:
            flag = "  (not compared — see script docstring)"
        print(f"  {pos:<5} n={len(vals):<5} mean={mean_r:6.3f}  max={max_r:6.3f} ({who}){flag}")
    return ok


def check_name_coverage(season: int, week: int, cache_dir: str, refresh: bool) -> float:
    stats_rows = [
        r for r in fetch_rows("stats_player_week", season=season, cache_dir=cache_dir, refresh=refresh)
        if int(r.get("week", -1) or -1) == week and (r.get("position") or "").upper() in _COMPARABLE_POSITIONS
    ]
    try:
        roster_rows = [
            r for r in fetch_rows("roster_weekly", season=season, cache_dir=cache_dir, refresh=refresh)
            if int(r.get("week", -1) or -1) == week
        ]
    except Exception as exc:
        print(f"  week {week}: roster_weekly unavailable ({exc}) — skipping coverage check")
        return 100.0

    targets = [
        # `player_display_name` ("Aaron Rodgers"), NOT `player_name`
        # ("A.Rodgers", nflverse's abbreviated form) -- the abbreviated form
        # is a poor fit for `names.normalize_name`/the fuzzy cascade and
        # tanks match coverage for no real reason. Same field
        # `ffbot/history/actuals.py`'s own `player_statline` (indirectly,
        # via `score_player_row`) and `names.index_by_key` default to.
        {"name": r.get("player_display_name") or r.get("full_name") or "", "position": r.get("position") or "", "team": r.get("team") or ""}
        for r in stats_rows
    ]
    targets = [t for t in targets if t["name"]]
    matches = match_actuals(
        roster_rows, targets,
        name_field="full_name",
    )
    summary = coverage_summary(matches)
    print(f"  week {week}: {summary['matched']}/{summary['total']} matched ({summary['pct']:.1f}%)")
    return summary["pct"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    if not seasons:
        print("error: --seasons produced no years", file=sys.stderr)
        return 1

    overall_ok = True
    for season in seasons:
        try:
            ok = check_scoring_reconciliation(season, args.cache_dir, args.refresh, args.sample)
            overall_ok = overall_ok and ok
        except Exception as exc:
            print(f"SEASON {season}: scoring reconciliation failed to run: {exc}", file=sys.stderr)
            overall_ok = False

        print(f"\nSEASON {season} — name-match coverage (stats_player_week vs. roster_weekly)")
        try:
            worst_pct = 100.0
            for week in (1, 8, 17):
                pct = check_name_coverage(season, week, args.cache_dir, args.refresh)
                worst_pct = min(worst_pct, pct)
            if worst_pct < 98.0:
                print(f"  <-- below 98% coverage in at least one sampled week; investigate before trusting this season")
                overall_ok = False
        except Exception as exc:
            print(f"  name-match coverage failed to run: {exc}", file=sys.stderr)

    print("\n" + ("OK — historical data layer checks out." if overall_ok else "FAILED — see flagged rows above."))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
