#!/usr/bin/env python3
"""The weekly brief: start/sit, waivers, and streaming, in one report.

    python scripts/week_report.py --week 3
    python scripts/week_report.py --week 3 --proj weekly/wk3_flex.csv --proj weekly/wk3_qb.csv
    python scripts/week_report.py --week 3 --stream K DEF --waivers

Reads `roster.yml` (see roster.example.yml) and, if present,
`weekly/week-NN.yml` for this week's researched status/weather/vegas/notes —
see docs/DRAFT.md and docs/INSEASON.md for where that file comes from. If no fresh
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

from ffbot import denial  # noqa: E402
from ffbot import policy  # noqa: E402
from ffbot import roster_source as rs  # noqa: E402
from ffbot import week  # noqa: E402
from ffbot.names import normalize_name  # noqa: E402
from ffbot.report import LoadedReport, ReportError  # noqa: E402
from ffbot.report import load_everything as _load_everything  # noqa: E402

_WIDTH = 92


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--roster", default="roster.yml", help="path to roster.yml (default: roster.yml)")
    p.add_argument("--week", type=int, required=True, help="current NFL week")
    p.add_argument("--proj", action="append", default=None, help="weekly FantasyPros CSV (repeatable); falls back to the season board if omitted")
    p.add_argument("--weekly", default=None, help="path to weekly/week-NN.yml (default: derived from --week)")
    p.add_argument("--faab", type=int, default=None, help="remaining FAAB budget, for waiver bid sizing (FAAB leagues only; see league.yml waiver_type)")
    p.add_argument("--priority", type=int, default=None, help="your current rolling waiver priority, 1=best/most valuable (rolling-priority leagues only; unknown assumes no urgency)")
    p.add_argument("--stream", nargs="*", default=None, metavar="POS", help="show streaming candidates for these positions, e.g. --stream K DEF")
    p.add_argument("--waivers", action="store_true", help="show ranked waiver-add candidates (needs a draft board — see draft/board_csv in config.yml)")
    p.add_argument("--weeks-in-season", type=int, default=17, help="for season-board fallback scaling (default: 17)")
    p.add_argument("--state", default="weekly/lineup_state.yml", help="remembered lineup slots, for accurate 'no changes needed' reports (default: weekly/lineup_state.yml)")
    p.add_argument("--league-rosters", default="league_rosters.yml", help="path to league_rosters.yml (see scripts/import_league_rosters.py); missing file = no exclusion applied")
    p.add_argument("--no-save-state", action="store_true", help="don't persist this run's lineup as next run's baseline (useful for a what-if run)")
    return p.parse_args(argv)


def load_everything(args: argparse.Namespace) -> LoadedReport:
    """Thin wrapper around `ffbot.report.load_everything`: the CLI's only
    addition is turning a `ReportError` into the `SystemExit` this script has
    always used — the GUI (`ffbot/webapi.py`) calls the shared function
    directly and handles `ReportError` as a catchable error instead."""
    try:
        return _load_everything(
            config_path=args.config,
            roster_path=args.roster,
            week_num=args.week,
            proj_csv_paths=args.proj,
            weekly_path=args.weekly,
            weeks_in_season=args.weeks_in_season,
            league_rosters_path=args.league_rosters,
        )
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


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


def render_roster_status(status: week.RosterStatus) -> str:
    space = status.space
    lines = [
        f"ROSTER  {space.occupied}/{space.capacity} filled, {space.open_spots} open, "
        f"IR {space.ir_parked} used",
        "-" * _WIDTH,
    ]
    if status.missing:
        lines.append(f"  (skipped {len(status.missing)} rostered player(s) with no board match: "
                      f"{', '.join(status.missing)})")
    core_sorted = sorted(status.core, key=lambda c: -c.hold_margin)
    stream_sorted = sorted(status.stream, key=lambda c: -c.hold_margin)
    lines.append(f"  CORE ({len(core_sorted)})   " + ", ".join(c.name for c in core_sorted))
    if stream_sorted:
        stream_s = ", ".join(f"{c.name} ({c.hold_margin:+.1f})" for c in stream_sorted)
        lines.append(f"  STREAM ({len(stream_sorted)}) " + stream_s)
    return "\n".join(lines)


def render_streamers(position: str, candidates) -> str:
    lines = [f"STREAMING {position}", "-" * _WIDTH]
    if not candidates:
        lines.append("  (no candidates found)")
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  {i}) {c.name:<20} {c.team:<4} val {c.weekly_value:>6.1f}   {c.reason}")
    return "\n".join(lines)


def render_waivers(candidates, unmatched_roster, waiver_type: str) -> str:
    lines = ["WAIVERS", "-" * _WIDTH]
    if unmatched_roster:
        lines.append(f"  (skipped {len(unmatched_roster)} rostered player(s) with no board match: "
                      f"{', '.join(unmatched_roster)})")
    if not candidates:
        lines.append("  (no upgrades found)")
    for i, c in enumerate(candidates, start=1):
        drop = f"drop {c.drop_name}" if c.drop_name else c.drop_reason
        cost = f"bid <= {c.max_bid}" if waiver_type != "rolling" else c.claim_note
        lines.append(
            f"  {i}) ADD {c.add_name:<20} {c.position:<4} net {c.net:>+6.1f} "
            f"{drop:<24} {cost}"
        )
        lines.append(f"       {c.reason}")
    return "\n".join(lines)


def render_ir_stash(candidates) -> str:
    lines = ["IR STASH  (zero bench cost -- add straight to an open IR slot)", "-" * _WIDTH]
    if not candidates:
        lines.append("  (no researched IR-eligible free agents this week)")
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  {i}) ADD {c.add_name:<20} {c.position:<4} {c.value:>6.1f} season pts   {c.reason}")
    return "\n".join(lines)


def render_denial(candidates) -> str:
    lines = [
        "DENIAL HOLDS  (would not start these yourself -- flagged, not a normal add)",
        "-" * _WIDTH,
    ]
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  {i}) ADD {c.add_name:<20} {c.position:<4} denial {c.denial_value:>+6.1f}   {c.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    loaded = load_everything(args)
    cfg, weekly, board = loaded.cfg, loaded.weekly, loaded.board
    players, unmatched, stadiums, league_rosters = (
        loaded.players, loaded.unmatched, loaded.stadiums, loaded.league_rosters
    )
    if league_rosters.teams:
        print(
            f"League rosters loaded: {len(league_rosters.teams)} teams "
            f"({len(league_rosters.rostered_names())} players) — free-agent pool excludes them.",
            file=sys.stderr,
        )
        if league_rosters.unmatched:
            print(
                f"  {len(league_rosters.unmatched)} unmatched from that import — "
                "see league_rosters.yml's 'unmatched:' list.",
                file=sys.stderr,
            )

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

    if board is not None:
        status = week.build_roster_status(players, cfg.roster_positions, board, cfg)
        print()
        print(render_roster_status(status))

    if args.stream:
        if board is None:
            print("\n(--stream needs a draft board; set draft.board_csv in config.yml)")
        else:
            # normalize_name, not raw string equality — a casing/punctuation
            # difference between roster and board spelling would otherwise
            # let one of your own rostered K/DEF show up as a "streaming
            # candidate," same match key `waiver_candidates` already uses.
            rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
            pool = [bp for bp in board.players if normalize_name(bp.name) not in rostered_names]
            for pos in args.stream:
                candidates = week.rank_streamers(pool, pos.upper(), weekly, cfg.season, week=args.week)
                print()
                print(render_streamers(pos.upper(), candidates))

    if args.waivers:
        waiver_type = cfg.league.waiver_type if cfg.league is not None else "faab"
        if board is None:
            print("\n(--waivers needs a draft board; set draft.board_csv in config.yml)")
        elif waiver_type != "rolling" and args.faab is None:
            print("\n(--waivers needs --faab <remaining budget> to size bids under a FAAB league)")
        else:
            if waiver_type == "rolling" and args.faab is not None:
                print("\n(this league uses rolling waiver priority, not FAAB — --faab is ignored; use --priority)", file=sys.stderr)
            weeks_remaining = max(1, args.weeks_in_season - args.week + 1)
            candidates, missing = week.waiver_candidates(
                players, board, cfg.roster_positions, cfg,
                remaining_faab=args.faab or 0, my_priority=args.priority,
                weeks_remaining=weeks_remaining, league_rosters=league_rosters,
                week=args.week,
            )
            print()
            print(render_waivers(candidates, missing, waiver_type))

            ir_candidates = week.ir_stash_candidates(
                players, board, cfg.roster_positions, weekly, cfg, league_rosters=league_rosters
            )
            if ir_candidates:
                print()
                print(render_ir_stash(ir_candidates))

            if cfg.season.denial_weight != 0.0 and league_rosters.teams:
                roster_keys, _ = week.roster_board_keys(players, board)
                rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
                streaming_floor = week.best_streaming_baseline(roster_keys, board, cfg)
                denial_list = denial.denial_candidates(
                    roster_keys, board, cfg.roster_positions, cfg, league_rosters,
                    rostered_names, streaming_floor,
                )
                if denial_list:
                    if waiver_type == "rolling":
                        verdict = policy.can_deny_claim(args.priority or cfg.draft.num_teams, cfg)
                        if not verdict.allowed:
                            denial_list = []
                            print(f"\n(denial holds suppressed: {verdict.reason})", file=sys.stderr)
                    if denial_list:
                        print()
                        print(render_denial(denial_list))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
