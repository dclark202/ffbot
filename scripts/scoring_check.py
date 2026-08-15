#!/usr/bin/env python3
"""Verify league.yml on its own, before anything downstream touches it.

    python scripts/scoring_check.py
    python scripts/scoring_check.py --league league.yml --board draft/proj_flex.csv draft/proj_qb.csv:QB draft/proj_k.csv:K draft/proj_dst.csv:DEF

Prints the parsed rules grouped by stat type, then — if board CSVs are
configured or passed — loads the real board under those rules and reports,
per position, how many players were actually league-scored vs. left on
FantasyPros' consensus points, the self-check residual (should be small; a
large one means the stat-column layout doesn't match this build's
`STAT_LAYOUTS` anymore, not that your league differs from consensus), and
exactly which of your league's rules no export column can express at all.

This is meant to run standalone, before implementing anything that consumes
league-scored points — the scoring config is the foundation every later
change depends on, so it should be verifiable by itself first.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board  # noqa: E402
from ffbot.config import Config, LeagueScoring  # noqa: E402
from ffbot.scoring import unmodeled_rules  # noqa: E402


def _fmt(label: str, value) -> str:
    return f"  {label:<28} {value}"


def print_rules(league: LeagueScoring) -> None:
    print(f"league.yml: {league.name or '(unnamed)'}")
    if league.source:
        print(f"  source: {league.source}")
    print(_fmt("games_per_season", league.games_per_season))
    print(_fmt("regular_season_weeks", league.regular_season_weeks))
    print(_fmt("playoff_teams", league.playoff_teams))
    print(_fmt("lock_eliminated_teams", league.lock_eliminated_teams))

    print("\nPASSING")
    print(_fmt("yards_per_point", league.passing.yards_per_point))
    print(_fmt("td", league.passing.td))
    print(_fmt("int", league.passing.int))
    print(_fmt("two_pt", league.passing.two_pt))

    print("\nRUSHING")
    print(_fmt("yards_per_point", league.rushing.yards_per_point))
    print(_fmt("td", league.rushing.td))

    print("\nRECEIVING")
    print(_fmt("yards_per_point", league.receiving.yards_per_point))
    print(_fmt("td", league.receiving.td))
    print(_fmt("reception (PPR)", league.receiving.reception))
    if league.receiving.reception_by_position:
        print(_fmt("reception overrides", league.receiving.reception_by_position))

    print("\nKICKING")
    if league.kicking.fg_by_distance:
        for band in league.kicking.fg_by_distance:
            print(_fmt(f"FG {int(band.min)}-{int(band.max)}", band.points))
        if league.kicking.fg_distance_mix:
            print(_fmt("distance mix (estimate)", league.kicking.fg_distance_mix))
        else:
            print("  (no fg_distance_mix set — falls back to a flat average across bands)")
    else:
        print(_fmt("fg_made (flat)", league.kicking.fg_made))
    print(_fmt("pat_made", league.kicking.pat_made))

    print("\nDEFENSE")
    print(_fmt("sack", league.defense.sack))
    print(_fmt("interception", league.defense.interception))
    print(_fmt("fumble_recovery", league.defense.fumble_recovery))
    print(_fmt("touchdown", league.defense.touchdown))
    print(_fmt("safety", league.defense.safety))
    if league.defense.points_allowed:
        for t in league.defense.points_allowed:
            print(_fmt(f"points_allowed <= {t.max:g}", t.points))
        print(_fmt("points_allowed_stdev", league.defense.points_allowed_stdev))
    else:
        print("  points_allowed: not scored")

    rules = unmodeled_rules(league)
    print("\nNOT MODELED (no export column can express these — see the note in each)")
    if rules:
        for r in rules:
            print(f"  - {r}")
    else:
        print("  (none — every enabled rule is at least estimable from the exports)")


def print_board_report(cfg: Config) -> None:
    if not cfg.draft.board_csv:
        print("\n(no draft.board_csv configured — pass --board to check against real files)")
        return
    print("\nBOARD CHECK")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            board = load_board(cfg.draft.board_csv, cfg.roster_positions, cfg.draft.num_teams, cfg)
        except ValueError as exc:
            print(f"\n(board check skipped — {exc})")
            return
    for pos, counts in sorted(board.scoring_summary().items()):
        total = counts["league"] + counts["consensus"]
        note = ""
        if cfg.league is not None and counts["league"] == 0 and total > 0:
            note = "  <- entire position stayed on consensus; no matching stat columns found"
        print(f"  {pos:<5} league={counts['league']:<4} consensus={counts['consensus']:<4}{note}")

    if board.scoring_residual:
        print("\n  self-check residual (FantasyPros' own FPTS vs. this codebase's model of")
        print("  FantasyPros' own scoring — small is good; large means STAT_LAYOUTS is stale,")
        print("  not that your league differs from consensus):")
        for pos, resid in sorted(board.scoring_residual.items()):
            flag = "  <-- investigate" if resid > 1.5 else ""
            print(f"    {pos:<5} {resid:.2f} pts{flag}")

    parser_warnings = [w for w in caught if "STAT_LAYOUTS" not in str(w.message) and "league.yml" not in str(w.message)]
    if parser_warnings:
        print("\n  other warnings while loading the board:")
        for w in parser_warnings:
            print(f"    - {w.message}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--league", default=None, help="override config.yml's league_file")
    ap.add_argument("--board", nargs="*", default=None, help="CSV[:POSITION] pairs; overrides config.yml's draft.board_csv")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    if args.league:
        league = LeagueScoring.load(args.league)
        if league is None:
            print(f"error: {args.league} not found", file=sys.stderr)
            return 1
        cfg.league = league
        cfg.league_file = args.league

    if cfg.league is None:
        print(f"No league.yml configured ({cfg.league_file or '(unset)'} not found).")
        print("Every board stays on FantasyPros' own consensus scoring, unchanged.")
        return 0

    if args.board:
        board_csv = []
        for entry in args.board:
            if ":" in entry:
                path, pos = entry.rsplit(":", 1)
                board_csv.append({"path": path, "position": pos})
            else:
                board_csv.append(entry)
        cfg.draft.board_csv = board_csv

    print_rules(cfg.league)
    print_board_report(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
