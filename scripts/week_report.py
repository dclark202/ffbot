#!/usr/bin/env python3
"""The weekly brief: start/sit, waivers, and streaming, in one report.

    python scripts/week_report.py --week 3
    python scripts/week_report.py --week 3 --source sleeper --season 2026
    python scripts/week_report.py --week 3 --proj weekly/wk3_flex.csv --proj weekly/wk3_qb.csv
    python scripts/week_report.py --week 3 --stream K DEF --waivers

Reads `roster.yml` (see roster.example.yml) and, if present,
`weekly/week-NN.yml` for this week's researched status/weather/vegas/notes —
see docs/GUIDE.md for where that file comes from (the /gameday command).

Where the projection NUMBERS themselves come from is a separate question —
see `projection_source:` in config.yml (`--source` overrides it for one
run): `"board"` (the default) rescales the frozen preseason draft board down
to a per-week baseline; `"sleeper"` fetches real, current weekly numbers
free and unauthenticated from api.sleeper.app; `"csv"` is this flag's
original `--proj` route, hand-fed FantasyPros weekly exports. A `"sleeper"`
fetch failure never crashes the report — it falls back to `"board"` for that
run and prints why.

This script must not import `yahoo_fantasy_api` or `requests` at module
level — same invariant as scripts/draft.py, for the same reason: the offline
path has to work whether or not Yahoo API access exists yet.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import gameplan  # noqa: E402
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
    p.add_argument("--source", choices=["board", "sleeper", "csv"], default=None, help="override config.yml's projection_source.source for this run")
    p.add_argument("--season", type=int, default=None, help="NFL season year for a live source (e.g. 2026); default: inferred from today's date")
    p.add_argument("--weekly", default=None, help="path to weekly/week-NN.yml (default: derived from --week)")
    p.add_argument("--priority", type=int, default=None, help="your current rolling waiver priority, 1=best/most valuable (default: fetched live from Sleeper under roster_source: sleeper, else assumes no urgency)")
    p.add_argument("--stream", nargs="*", default=None, metavar="POS", help="show streaming candidates for these positions, e.g. --stream K DEF")
    p.add_argument("--waivers", action="store_true", help="show ranked waiver-add candidates (needs a draft board — see draft/board_csv in config.yml)")
    p.add_argument("--weeks-in-season", type=int, default=17, help="for season-board fallback scaling (default: 17)")
    p.add_argument("--state", default="weekly/lineup_state.yml", help="remembered lineup slots, for accurate 'no changes needed' reports (default: weekly/lineup_state.yml)")
    p.add_argument("--league-rosters", default="league_rosters.yml", help="path to league_rosters.yml (see scripts/import_league_rosters.py); missing file = no exclusion applied")
    p.add_argument("--no-save-state", action="store_true", help="don't persist this run's lineup as next run's baseline (useful for a what-if run)")
    p.add_argument(
        "--refresh", action=argparse.BooleanOptionalAction, default=False,
        help="bypass Sleeper's normal caches for this run -- league state, rosters, players dump, and live projections/conditions all refetch regardless of TTL (default: off; scripts/autorun.py passes this for pre-kickoff/pre-waiver fires)",
    )
    p.add_argument("--out", default=None, help="write the report body to this path, in addition to (or instead of, with --quiet) stdout -- for an unattended/scheduled run (see scripts/autorun.py)")
    p.add_argument("--format", choices=["text", "markdown"], default="text", help="report body format for stdout and --out (default: text, today's exact fixed-width layout)")
    p.add_argument("--quiet", action="store_true", help="suppress the report body on stdout (warnings/alerts still print to stderr); pairs with --out for an unattended run")
    return p.parse_args(argv)


def render_report(sections: list[str], week_num: int, fmt: str) -> str:
    """Join every rendered section into one report body. `"text"` (the
    default) is exactly today's stdout layout — sections separated by a
    blank line, nothing else added. `"markdown"` wraps the identical
    content in a single fenced code block under a `# Week N Report`
    header: a deliberately narrow scope (see this function's docstring in
    the surrounding module for why) rather than a bespoke per-section
    markdown table redesign — every section's fixed-width alignment stays
    exactly as readable as it is in a terminal, just inside a file a
    scheduled run can hand to the GUI's `/reports` page or open directly.
    """
    body = "\n\n".join(sections)
    if fmt == "text":
        return body
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"# Week {week_num} Report\n\nGenerated: {generated}\n\n```\n{body}\n```\n"


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
            season=args.season,
            source_override=args.source,
            refresh=args.refresh,
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


def render_recommended_start_sit(start_sit, unfilled_slots, unmatched_roster) -> str:
    """The post-pickup lineup a human would field AFTER making the ADD/DROP
    section's recommended moves -- distinct from the plain LINEUP section
    above, which stays a read of the CURRENT roster only (see
    `ffbot.gameplan`'s module docstring on why the two can disagree)."""
    lines = ["RECOMMENDED START/SIT  (after the adds below)", "-" * _WIDTH]
    if unmatched_roster:
        lines.append(f"  (skipped {len(unmatched_roster)} rostered player(s) with no board match: "
                      f"{', '.join(unmatched_roster)})")
    if not start_sit:
        lines.append("  No changes beyond your current lineup.")
    for m in start_sit:
        lines.append(f"  {m.text}")
    if unfilled_slots:
        lines.append(f"  UNFILLED: {', '.join(unfilled_slots)}")
    return "\n".join(lines)


def render_claims(claims) -> str:
    lines = ["WAIVER CLAIMS", "-" * _WIDTH]
    if not claims:
        lines.append("  (nothing worth a claim this week)")
    for i, c in enumerate(claims, start=1):
        drop = f"drop {c.drop_name}" if c.drop_name else c.drop_reason
        lines.append(
            f"  {i}) ADD {c.add_name:<20} {c.position:<4} net {c.net:>+6.1f} "
            f"{drop:<24} {c.claim_note}"
        )
        if c.if_clears is not None:
            lines.append(f"       {c.if_clears.text}")
        lines.append(f"       {'; '.join(c.reasons)}")
    return "\n".join(lines)


def render_adddrop(rows, notes) -> str:
    """Streaming and denial are REASONS on an ordinary row now (see
    `ffbot.gameplan`), not their own sections -- a K/DEF need or a "blocks
    <team>" denial motive both just show up here."""
    lines = ["ADD/DROP", "-" * _WIDTH]
    if not rows:
        lines.append("  (no add/drop recommendations this week)")
    for i, r in enumerate(rows, start=1):
        lines.append(f"  {i}) {r.text}")
    for n in notes:
        lines.append(f"  ({n})")
    return "\n".join(lines)


def render_ir_stash(candidates) -> str:
    lines = ["IR STASH  (zero bench cost -- add straight to an open IR slot)", "-" * _WIDTH]
    if not candidates:
        lines.append("  (no researched IR-eligible free agents this week)")
    for i, c in enumerate(candidates, start=1):
        lines.append(f"  {i}) ADD {c.add_name:<20} {c.position:<4} {c.value:>6.1f} season pts   {c.reason}")
    return "\n".join(lines)


@dataclass
class ReportRun:
    """Everything a single `week_report.py` run produced, structured (not
    just the rendered text) — the seam `scripts/autorun.py` builds its
    actionability check on (`Trigger`/`actionable_summary`), since deciding
    "is this worth a notification" needs `WaiverCandidate.claim_note`/`.net`
    as real fields, not text parsed back out of a rendered table.
    `main()` is now just `parse_args` -> `run_report` -> `render_report` ->
    print/`--out`, so CLI output stays byte-identical to before this
    existed.
    """

    week: int
    loaded: LoadedReport
    brief: week.WeekBrief
    streamers: dict[str, list] = field(default_factory=dict)  # {position: [StreamCandidate, ...]}, --stream only
    plan: "gameplan.GamePlan | None" = None
    # `waivers` -- kept for scripts/autorun.py's existing `actionable_summary`
    # contract: every recommended row (`AddDropRec`, `.kind`/.net/.add_name/
    # .drop_name` fields), claims first (autorun only acts on `kind ==
    # "claim"` rows). `denial` stays an always-empty list -- denial is a
    # REASON on an ordinary row now (`AddDropRec.denial_team`), never its
    # own section -- kept only so old callers checking `isinstance(x, list)`
    # don't need a special case.
    waivers: list = field(default_factory=list)
    waiver_missing: list[str] = field(default_factory=list)
    ir_stash: list = field(default_factory=list)
    denial: list = field(default_factory=list)
    sections: list[str] = field(default_factory=list)


def run_report(args: argparse.Namespace) -> ReportRun:
    """Load everything and build every report section, exactly as `main()`
    always has — split out so `scripts/autorun.py` can drive the same
    pipeline and get typed candidate lists back instead of parsing rendered
    text. Operator warnings still print to stderr unconditionally here,
    same as before this function existed; only the final stdout/`--out`
    write moved to `main()`.
    """
    loaded = load_everything(args)
    cfg, weekly, board = loaded.cfg, loaded.weekly, loaded.board
    players, unmatched, stadiums, league_rosters = (
        loaded.players, loaded.unmatched, loaded.stadiums, loaded.league_rosters
    )

    for a in loaded.projection_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.roster_source_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.league_rosters_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.game_conditions_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.standings_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.board_alerts:
        print(f"WARNING: {a}", file=sys.stderr)
    for a in loaded.scoring_alerts:
        print(f"WARNING: {a}", file=sys.stderr)

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
    # Skipped entirely under a live Sleeper lineup baseline (loaded.
    # slots_source == "sleeper"): the real current lineup in the Sleeper
    # app already IS the baseline there, and weekly/lineup_state.yml would
    # only be a stale, second source of truth.
    live_slots = loaded.slots_source == "sleeper"
    if live_slots:
        print("Live Sleeper lineup slots are this run's baseline -- weekly/lineup_state.yml is not used.", file=sys.stderr)
    else:
        state = rs.load_lineup_state(args.state)
        players = rs.apply_lineup_state(players, state)

    # Report BODY sections are collected here rather than printed directly,
    # so the same content can go to stdout, --out, or both, in either
    # --format -- see render_report. Warnings/alerts above (and a couple
    # still below) stay on stderr unconditionally, exactly as before: they
    # are operator noise, not part of the report artifact itself.
    sections: list[str] = []

    brief = week.build_week_brief(
        players, cfg.roster_positions, args.week, cfg, weekly, stadiums,
        board=board, league_rosters=league_rosters,
    )
    run = ReportRun(week=args.week, loaded=loaded, brief=brief, sections=sections)
    sections.append(render_brief(brief))

    if not args.no_save_state and not live_slots:
        rs.save_lineup_state(args.state, brief.lineup)

    if board is not None:
        status = week.build_roster_status(players, cfg.roster_positions, board, cfg)
        sections.append(render_roster_status(status))

    if args.stream:
        if board is None:
            sections.append("(--stream needs a draft board; set draft.board_csv in config.yml)")
        else:
            # normalize_name, not raw string equality — a casing/punctuation
            # difference between roster and board spelling would otherwise
            # let one of your own rostered K/DEF show up as a "streaming
            # candidate," same match key `waiver_candidates` already uses.
            rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
            pool = [bp for bp in board.players if normalize_name(bp.name) not in rostered_names]
            for pos in args.stream:
                candidates = week.rank_streamers(pool, pos.upper(), weekly, cfg.season, week=args.week, stadiums=stadiums)
                run.streamers[pos.upper()] = candidates
                sections.append(render_streamers(pos.upper(), candidates))

    if args.waivers:
        if board is None:
            sections.append("(--waivers needs a draft board; set draft.board_csv in config.yml)")
        else:
            # Explicit --priority always wins; otherwise fall back to the
            # live Sleeper value (roster_source: sleeper only -- see
            # report.load_everything) so an unset flag no longer silently
            # assumes the cheapest, least-urgent priority. `None` still
            # reaches waiver_candidates when neither is available, which
            # keeps that same cheapest-case assumption as the last resort.
            priority = args.priority if args.priority is not None else loaded.waiver_priority
            if args.priority is None and loaded.waiver_priority is not None:
                print(f"(using live waiver priority {loaded.waiver_priority} from Sleeper — pass --priority to override)", file=sys.stderr)
            # A FIXED season length, matching `season_board_rows`'s own
            # fallback-pricing convention (see report.load_everything) --
            # not a shrinking "weeks remaining", which would desync a
            # candidate's board-fallback price from a rostered player's.
            # loaded.ros_board (when live rest-of-season numbers were
            # fetched -- see report.load_everything) carries real ROS points
            # for ros_gain/hold_margin/drop_cost; `board` (the frozen
            # season board) is the fallback, same as before this existed.
            plan = gameplan.build_gameplan(
                loaded, args.week, players, my_priority=priority, weeks_in_season=args.weeks_in_season,
            )
            run.plan = plan
            run.waivers = list(plan.claims) + list(plan.adds)
            run.waiver_missing = plan.missing
            run.ir_stash = plan.ir_stash
            for note in plan.notes:
                print(f"({note})", file=sys.stderr)

            sections.append(render_recommended_start_sit(plan.start_sit, plan.unfilled_slots, plan.missing))
            sections.append(render_claims(plan.claims))
            sections.append(render_adddrop(plan.adds, plan.notes))
            if plan.ir_stash:
                sections.append(render_ir_stash(plan.ir_stash))

    return run


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run = run_report(args)

    report_text = render_report(run.sections, args.week, args.format)
    if not args.quiet:
        print(report_text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_text, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
