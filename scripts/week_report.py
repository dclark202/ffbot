#!/usr/bin/env python3
"""The weekly brief: start/sit, waivers, and streaming, in one report.

    python scripts/week_report.py --week 3
    python scripts/week_report.py --week 3 --proj weekly/wk3_flex.csv --proj weekly/wk3_qb.csv
    python scripts/week_report.py --week 3 --stream K DEF --waivers

Reads `roster.yml` (see roster.example.yml) and, if present,
`weekly/week-NN.yml` for this week's researched status/weather/vegas/notes —
see REFRESH.md and INSEASON.md for where that file comes from. If no fresh
weekly projection CSVs are supplied via `--proj`, falls back to the existing
season-long draft board rescaled to a per-week baseline (`draft/board_csv` in
config.yml), so the report still runs on whatever the last board refresh
produced rather than requiring a brand-new download every week.

This script must not import `yahoo_fantasy_api` or `requests` at module
level — same invariant as scripts/draft.py, for the same reason: the offline
path has to work whether or not Yahoo API access exists yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import roster_source as rs  # noqa: E402
from ffbot import week  # noqa: E402
from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402

_WIDTH = 92


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--roster", default="roster.yml", help="path to roster.yml (default: roster.yml)")
    p.add_argument("--week", type=int, required=True, help="current NFL week")
    p.add_argument("--proj", action="append", default=None, help="weekly FantasyPros CSV (repeatable); falls back to the season board if omitted")
    p.add_argument("--weekly", default=None, help="path to weekly/week-NN.yml (default: derived from --week)")
    p.add_argument("--faab", type=int, default=None, help="remaining FAAB budget, for waiver bid sizing")
    p.add_argument("--stream", nargs="*", default=None, metavar="POS", help="show streaming candidates for these positions, e.g. --stream K DEF")
    p.add_argument("--waivers", action="store_true", help="show ranked waiver-add candidates (needs a draft board — see draft/board_csv in config.yml)")
    p.add_argument("--weeks-in-season", type=int, default=17, help="for season-board fallback scaling (default: 17)")
    p.add_argument("--state", default="weekly/lineup_state.yml", help="remembered lineup slots, for accurate 'no changes needed' reports (default: weekly/lineup_state.yml)")
    p.add_argument("--no-save-state", action="store_true", help="don't persist this run's lineup as next run's baseline (useful for a what-if run)")
    return p.parse_args(argv)


def _default_weekly_path(week_num: int) -> Path:
    return Path("weekly") / f"week-{week_num:02d}.yml"


def load_everything(args: argparse.Namespace):
    cfg = Config.load(args.config)

    weekly_path = Path(args.weekly) if args.weekly else _default_weekly_path(args.week)
    weekly = week.load_weekly_intel(weekly_path)

    board = None
    try:
        board = load_board_from_config(cfg)
    except ValueError:
        pass  # no board configured -- season-board fallback and waivers just won't be available

    fallback_rows = []
    if board is not None:
        weeks_remaining = max(1, args.weeks_in_season - args.week + 1)
        fallback_rows = rs.season_board_rows(board, weeks_remaining)

    csv_paths = args.proj or []
    try:
        players, unmatched = rs.load_roster(csv_paths, args.roster, fallback_rows=fallback_rows)
    except rs.RosterError as exc:
        print(f"{exc}", file=sys.stderr)
        raise SystemExit(1)

    if not csv_paths and not fallback_rows:
        print(
            "No weekly projections and no draft board to fall back on — pass "
            "--proj, or set draft.board_csv in config.yml.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    stadiums = week.load_stadiums()
    return cfg, weekly, board, players, unmatched, stadiums


def render_brief(brief: week.WeekBrief) -> str:
    lines = [f"WEEK {brief.week}", "=" * _WIDTH]

    if brief.alerts:
        lines.append("ALERTS")
        for a in brief.alerts:
            lines.append(f"  ! {a}")
        lines.append("-" * _WIDTH)

    lines.append("LINEUP")
    if brief.lineup.is_noop():
        lines.append("  No changes. Your lineup is already optimal.")
    else:
        for m in brief.lineup.moves:
            lines.append(f"  {m}")
    if brief.lineup.unfilled_slots:
        lines.append(f"  UNFILLED: {', '.join(brief.lineup.unfilled_slots)} — you're short a healthy body here")
    lines.append("-" * _WIDTH)

    lines.append("STARTING LINEUP")
    for slot, p in sorted(brief.lineup.assignments, key=lambda t: t[0]):
        pts = f"{p.projected_points:.1f}" if p.projected_points is not None else "-"
        lines.append(f"  {slot:<6} {p.name:<24} proj {pts}")
    lines.append("-" * _WIDTH)

    if brief.notes:
        lines.append("NOTES")
        for n in brief.notes:
            flag_s = f" [{', '.join(n.flags)}]" if n.flags else ""
            lines.append(f"  {n.name}: {n.note}{flag_s}")
        lines.append("-" * _WIDTH)

    return "\n".join(lines)


def render_streamers(position: str, candidates) -> str:
    lines = [f"STREAMING {position}", "-" * _WIDTH]
    if not candidates:
        lines.append("  (no candidates found)")
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  {i}) {c.name:<20} {c.team:<4} val {c.weekly_value:>6.1f}   {c.reason}")
    return "\n".join(lines)


def render_waivers(candidates, unmatched_roster) -> str:
    lines = ["WAIVERS", "-" * _WIDTH]
    if unmatched_roster:
        lines.append(f"  (skipped {len(unmatched_roster)} rostered player(s) with no board match: "
                      f"{', '.join(unmatched_roster)})")
    if not candidates:
        lines.append("  (no upgrades found)")
    for i, c in enumerate(candidates, start=1):
        drop = f"drop {c.drop_name}" if c.drop_name else c.drop_reason
        lines.append(
            f"  {i}) ADD {c.add_name:<20} {c.position:<4} {c.reason:<32} "
            f"{drop:<24} bid <= {c.max_bid}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg, weekly, board, players, unmatched, stadiums = load_everything(args)

    if unmatched:
        print(f"WARNING: {len(unmatched)} roster name(s) did not resolve:", file=sys.stderr)
        for m in unmatched:
            hint = f" (did you mean {m.suggestion!r}?)" if m.suggestion else ""
            print(f"  {m.query!r}{hint}", file=sys.stderr)
        print("These players are MISSING from every recommendation below.\n", file=sys.stderr)

    # Seed each player's current slot from the last run's output, so the
    # move list reflects real week-over-week changes rather than "everyone
    # moves off the bench" every single time -- see roster_source.py.
    state = rs.load_lineup_state(args.state)
    players = rs.apply_lineup_state(players, state)

    brief = week.build_week_brief(players, cfg.roster_positions, args.week, cfg, weekly, stadiums)
    print(render_brief(brief))

    if not args.no_save_state:
        rs.save_lineup_state(args.state, brief.lineup)

    if args.stream:
        if board is None:
            print("\n(--stream needs a draft board; set draft.board_csv in config.yml)")
        else:
            rostered_names = {p.name for p in players}
            pool = [bp for bp in board.players if bp.name not in rostered_names]
            for pos in args.stream:
                candidates = week.rank_streamers(pool, pos.upper(), weekly, cfg.season)
                print()
                print(render_streamers(pos.upper(), candidates))

    if args.waivers:
        if board is None:
            print("\n(--waivers needs a draft board; set draft.board_csv in config.yml)")
        elif args.faab is None:
            print("\n(--waivers needs --faab <remaining budget> to size bids)")
        else:
            candidates, missing = week.waiver_candidates(
                players, board, cfg.roster_positions, args.faab, cfg
            )
            print()
            print(render_waivers(candidates, missing))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
