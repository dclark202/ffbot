#!/usr/bin/env python3
"""Unattended trigger brain for the weekly report.

Runs on a schedule (Windows Task Scheduler, every ~15 min is the
recommended interval) and decides whether anything is due right now: a
per-kickoff-slot pre-game check (~80 min before each DISTINCT kickoff time
this week's games use -- Thursday night, Sunday early/late, Sunday night,
Monday night are typically five separate slots, not one), a waiver-claims
check the evening before this league's weekly waiver run, and a free-agent
check the morning after it (both slots in config.yml's `autorun:` block;
`--waiver-weekday`/`--waiver-hour` override the first). Each check has a
purpose and its message is shaped by it -- see `notification_for`. Each
trigger that fires runs the exact same scripts/week_report.py pipeline, with
--no-save-state so an unattended what-if-shaped check can never poison
next week's real lineup baseline, and writes reports/YYYY-wNN-<trigger>.md.

    python scripts/autorun.py --dry-run                # print this week's trigger schedule, fire nothing
    python scripts/autorun.py                           # check now, fire anything due, exit
    python scripts/autorun.py --waivers --priority 6     # forwarded to week_report.py for every fired run

One-shot by design, not a long-running daemon -- Task Scheduler owns the
polling interval; every invocation does a single check-and-maybe-fire and
exits immediately either way. Idempotent via a small JSON state file
(default data/autorun_state.json) keyed on a stable trigger id, so a
15-minute poll never double-fires the same trigger, and a window missed
entirely (machine asleep/off) fires LATE within a grace period rather than
never -- see _is_due. Past the grace period a missed trigger is simply
skipped, never fired: a start/sit brief for a game that already kicked off
helps nobody.

This module must not import ffbot.sleeper/ffbot.markets at module level --
same offline-importability invariant as scripts/draft.py and
scripts/week_report.py; every network call here goes through
ffbot.live.schedule or scripts/week_report.py's own already-lazy imports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import projections  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.console import make_streams_safe  # noqa: E402
from ffbot.live.schedule import (  # noqa: E402
    ScheduleError,
    _eastern_is_dst,  # noqa: F401 -- re-exported; tests pin the DST rule here
    current_week,
    eastern_to_utc,
    this_week_games,
)
from scripts import week_report  # noqa: E402

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# How long past a kickoff-tied trigger's due time it's still worth firing --
# a small buffer for "the poll landed a few minutes late," not a real grace
# window: past kickoff itself a start/sit check is moot regardless.
_KICKOFF_GRACE_MINUTES = 30.0

# The pre-waiver check has no hard cutoff the way kickoff does -- a
# half-day grace covers a machine that was briefly asleep/off without
# firing a bizarrely stale report the next day.
_WAIVER_GRACE_MINUTES = 12 * 60.0


@dataclass(frozen=True)
class Trigger:
    id: str  # stable across polls -- the state-file key
    due_at: datetime
    grace_minutes: float
    label: str  # human-readable, for --dry-run output
    # Pre-kickoff triggers only (None for the pre-waiver check). `kickoff` is
    # the schedule's own US-Eastern time -- what `games` carries and what `id`
    # is keyed on -- and `local_kickoff` is the same moment on this machine's
    # clock, which is what `due_at` and every human-facing time use.
    kickoff: datetime | None = None
    local_kickoff: datetime | None = None
    kind: str = ""  # "kickoff" | "waiver" | "post_waiver" | "research" | "grade" -- set by build_triggers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--chdir", default=None, metavar="DIR",
        help="chdir here before doing anything else -- every path this script and "
             "week_report.py resolve (config.yml, roster.yml, weekly/, reports/, "
             "data/autorun_state.json) is CWD-relative. A scheduled task has no "
             "notion of 'start in' the way a terminal does, so scripts/"
             "schedule_autorun.py always passes this, pointed at the repo root.",
    )
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--roster", default="roster.yml", help="path to roster.yml")
    p.add_argument("--season", type=int, default=None, help="override the inferred current NFL season")
    p.add_argument("--week", type=int, default=None, help="override the inferred current NFL week")
    p.add_argument("--lead-minutes", type=float, default=80.0, help="how long before each kickoff slot the pre-game check starts (default: 80 -- just after NFL inactives post at 90 minutes; with research on, a research pass runs first and the message still lands about an hour before kickoff)")
    p.add_argument("--waiver-weekday", choices=list(_WEEKDAYS), default=None, help="weekday the waiver-claims check fires on -- the evening before this league's weekly waiver run (default: config.yml's autorun.waiver_weekday, tue)")
    p.add_argument("--waiver-hour", type=int, default=None, help="local hour 0-23 the waiver-claims check fires at (default: config.yml's autorun.waiver_hour, 20 = 8pm)")
    p.add_argument("--state-file", default="data/autorun_state.json", help="idempotency state (default: data/autorun_state.json)")
    p.add_argument("--reports-dir", default="reports", help="where fired reports are written (default: reports/)")
    p.add_argument(
        "--refresh-league-rosters", action="store_true",
        help="also refresh league_rosters.yml via import_league_rosters.py --live before a fired run (needs sleeper.league_id) -- "
             "unnecessary under config.yml's league_rosters_source: sleeper, which already fetches live on every fired run; "
             "still the freshness mechanism for the file route",
    )
    p.add_argument("--league-rosters", default="league_rosters.yml", help="path passed through to week_report.py / import_league_rosters.py")
    p.add_argument("--dry-run", action="store_true", help="print this week's trigger schedule and exit -- fires nothing, writes no state")
    # Forwarded straight to week_report.py for every trigger that fires.
    p.add_argument("--stream", nargs="*", default=None, metavar="POS", help="positions to scan for a streaming upgrade (default: config.yml's season.stream_positions)")
    p.add_argument("--waivers", action=argparse.BooleanOptionalAction, default=True, help="show ranked waiver-add candidates (default: on -- pass --no-waivers to skip)")
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--weeks-in-season", type=int, default=17)
    return p.parse_args(argv)


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt/unreadable state file degrades to "nothing fired yet", never a crash


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _this_calendar_week_at(now: datetime, weekday: int, hour: int) -> datetime:
    """The occurrence of `weekday`/`hour` in `now`'s own Mon-Sun calendar
    week -- computed from that week's Monday forward, so it resolves
    correctly whether `weekday` falls before OR after `now` within the
    same week (a plain "most recent past occurrence" calculation would
    wrongly jump back a full week when `now` is earlier in the week than
    `weekday` -- e.g. Monday looking for Tuesday must return TOMORROW, not
    last week's Tuesday). A run mid-week compares against THIS week's
    waiver check either way, whether that moment is already past (now
    within/past its grace period) or still ahead.
    """
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return (monday + timedelta(days=weekday)).replace(hour=hour, minute=0, second=0, microsecond=0)


def eastern_to_local(kickoff: datetime) -> datetime:
    """A naive US-Eastern kickoff as a naive time on THIS machine's clock --
    the same clock `datetime.now()` reads in `main`."""
    return eastern_to_utc(kickoff).astimezone().replace(tzinfo=None)


def _clock(t: datetime) -> str:
    """`19:35` -- 24-hour, the manager's call (2026-09-16).

    Every clock a person reads here goes through this one function, so a
    trigger label and the "Checked" stamp beside it can never disagree about
    what kind of clock they are on -- which they did while labels were
    `%H:%M` and this was `%I:%M%p`.
    """
    return t.strftime("%H:%M")


def build_triggers(
    games: dict, now: datetime, lead_minutes: float, waiver_weekday: str, waiver_hour: int,
    to_local: Callable[[datetime], datetime] | None = None,
    research_weekday: str | None = None,
    research_hour: int = 17,
    grade_weekday: str | None = None,
    grade_hour: int = 8,
    post_waiver_weekday: str | None = None,
    post_waiver_hour: int = 7,
) -> list[Trigger]:
    """Every trigger for the current week: one per DISTINCT kickoff time
    (several games routinely share a slot -- one trigger covers all of
    them), one waiver-claims check, and -- when `post_waiver_weekday` is
    given -- one free-agent check the morning after the weekly run.
    `Trigger.id` must stay stable across polls within the same week; it's
    what the state file's idempotency keys on.
    """
    triggers: list[Trigger] = []

    distinct_kickoffs = sorted({g.kickoff for g in games.values() if g.kickoff is not None})
    for kickoff in distinct_kickoffs:
        # `games` carries the schedule's US-Eastern kickoff, but `now` is this
        # machine's local clock. Comparing the two directly shifted every
        # check by the machine's distance from Eastern -- the old "2h" lead
        # actually ran 1h out in Central time, and would have fired AT
        # kickoff in Pacific. `main` passes `eastern_to_local`; None
        # (identity) keeps this function pure for tests.
        local = to_local(kickoff) if to_local is not None else kickoff
        triggers.append(
            Trigger(
                # Keyed on the schedule's own time, NOT the local one, so a
                # state file written before this conversion existed still
                # matches -- re-keying would re-fire checks that already ran.
                id=f"pre_kickoff_{kickoff.isoformat()}",
                due_at=local - timedelta(minutes=lead_minutes),
                grace_minutes=lead_minutes + _KICKOFF_GRACE_MINUTES,
                label=f"pre-kickoff ({local:%a} {_clock(local)})".lower(),
                kickoff=kickoff,
                local_kickoff=local,
                kind="kickoff",
            )
        )

    waiver_due = _this_calendar_week_at(now, _WEEKDAYS[waiver_weekday], waiver_hour)
    triggers.append(
        Trigger(
            id=f"pre_waiver_{waiver_due.date().isoformat()}",
            due_at=waiver_due,
            grace_minutes=_WAIVER_GRACE_MINUTES,
            label=f"check ({waiver_weekday} {waiver_hour:02d}:00)",
            kind="waiver",
        )
    )

    # The free-agent check the morning after the weekly run: what happened
    # to the claims, and who is worth a free pickup now. Built only when
    # `autorun.post_waiver_enabled`; a typo'd weekday skips it loudly.
    if post_waiver_weekday is not None:
        post_index = _WEEKDAYS.get(post_waiver_weekday)
        if post_index is None:
            print(
                f"autorun: autorun.post_waiver_weekday {post_waiver_weekday!r} is not one of "
                f"{sorted(_WEEKDAYS)} -- the free-agent check is skipped",
                file=sys.stderr,
            )
        else:
            post_due = _this_calendar_week_at(now, post_index, post_waiver_hour)
            triggers.append(
                Trigger(
                    id=f"post_waiver_{post_due.date().isoformat()}",
                    due_at=post_due,
                    grace_minutes=_WAIVER_GRACE_MINUTES,
                    label=f"check ({post_waiver_weekday} {post_waiver_hour:02d}:00)",
                    kind="post_waiver",
                )
            )

    # The research-only pass after Friday's final injury designations. Built
    # only when research is on; a typo'd weekday skips the pass loudly rather
    # than taking down every poll of the week.
    if research_weekday is not None:
        weekday_index = _WEEKDAYS.get(research_weekday)
        if weekday_index is None:
            print(
                f"autorun: research.injury_report_weekday {research_weekday!r} is not one of "
                f"{sorted(_WEEKDAYS)} -- the injury-report research pass is skipped",
                file=sys.stderr,
            )
        else:
            research_due = _this_calendar_week_at(now, weekday_index, research_hour)
            triggers.append(
                Trigger(
                    id=f"research_{research_due.date().isoformat()}",
                    due_at=research_due,
                    grace_minutes=_WAIVER_GRACE_MINUTES,
                    label=f"research ({research_weekday} {research_hour:02d}:00)",
                    kind="research",
                )
            )

    # The projection grade (scripts/grade_week.py): after Monday night's game,
    # before the waiver check. Built only when grade.enabled; a typo'd weekday
    # skips it loudly, same as research.
    if grade_weekday is not None:
        grade_index = _WEEKDAYS.get(grade_weekday)
        if grade_index is None:
            print(
                f"autorun: grade.weekday {grade_weekday!r} is not one of "
                f"{sorted(_WEEKDAYS)} -- the projection grade is skipped",
                file=sys.stderr,
            )
        else:
            grade_due = _this_calendar_week_at(now, grade_index, grade_hour)
            triggers.append(
                Trigger(
                    id=f"grade_{grade_due.date().isoformat()}",
                    due_at=grade_due,
                    grace_minutes=_WAIVER_GRACE_MINUTES,
                    label=f"grade ({grade_weekday} {grade_hour:02d}:00)",
                    kind="grade",
                )
            )

    return triggers


def _is_due(trigger: Trigger, now: datetime, fired: set[str]) -> bool:
    if trigger.id in fired:
        return False
    if now < trigger.due_at:
        return False
    if now > trigger.due_at + timedelta(minutes=trigger.grace_minutes):
        return False  # missed the window entirely -- firing this late helps nobody
    return True


def _fire(
    trigger: Trigger, args: argparse.Namespace, cfg: Config, season: int, week_num: int,
    games: dict | None = None, skip_research: bool = False,
) -> "tuple[bool, week_report.ReportRun | None, research.ResearchResult | None]":
    """Run `week_report.run_report`'s exact pipeline for `trigger`, writing
    reports/YYYY-wNN-<trigger-suffix>.md, and hand back the structured
    `ReportRun` too (for `actionable_summary`/notifications below) instead
    of only the rendered text `week_report.main()` used to produce.
    `--refresh` is always passed -- the whole point of a pre-kickoff/
    pre-waiver check firing at a specific moment is that it wants the
    latest information right then, not whatever happened to be cached from
    an earlier poll this week.

    With `research.enabled`, a research pass runs between a live report and a
    second report that reads it (see `_run_research`). `skip_research` is set
    when this trigger already paid for its research on an earlier poll.

    Returns `(True, run, research)` on success; `(False, None, research)` on
    failure, so the caller does NOT mark the trigger fired -- an unattended
    run that failed should be retried on the next poll, within the trigger's
    own grace window, not silently treated as done. `research` is None when no
    pass ran.
    """
    reports_dir = Path(args.reports_dir)
    # `trigger.id` embeds an ISO kickoff timestamp (colons and all) for a
    # pre-kickoff trigger -- fine as a state-file dict key, but ":" is not
    # a legal Windows filename character. Sanitized here, only for the
    # filename; `trigger.id` itself (the idempotency key) is untouched.
    trigger_suffix = re.sub(r'[<>:"/\\|?*]', "-", trigger.id)
    out_path = reports_dir / f"{datetime.now():%Y}-w{week_num:02d}-{trigger_suffix}.md"

    stream_positions = args.stream if args.stream is not None else cfg.season.stream_positions

    week_report_argv = [
        "--config", args.config,
        "--roster", args.roster,
        "--week", str(week_num),
        "--season", str(season),
        "--weeks-in-season", str(args.weeks_in_season),
        "--league-rosters", args.league_rosters,
        "--no-save-state",
        "--refresh",
        # The structured counterpart to `out_path`'s markdown. Reuses the
        # SAME sanitized suffix, so a check's rendered report and its
        # recommendation log are named for the same trigger and are trivial
        # to line up afterwards. `trigger.id` itself stays untouched -- it is
        # the idempotency key, and `trigger_suffix` exists precisely because
        # a pre-kickoff id embeds an ISO timestamp whose colons are illegal
        # in a Windows filename.
        "--week-log-source", trigger_suffix,
    ]
    if stream_positions:
        week_report_argv += ["--stream", *stream_positions]
    if args.waivers:
        week_report_argv.append("--waivers")
    if args.priority is not None:
        week_report_argv += ["--priority", str(args.priority)]

    print(f"autorun: firing {trigger.label!r} ({trigger.id}) -> {out_path}", file=sys.stderr)
    research_result = None
    try:
        run = week_report.run_report(week_report.parse_args(week_report_argv))
        if cfg.research.enabled and not skip_research:
            research_result = _run_research(trigger, run, cfg, season, week_num, games or {})
            if research_result.ok:
                # Re-read with the fresh research. The first report just
                # refreshed every live feed and they are still cached, so this
                # costs a re-read, not a second round of fetches.
                rerun_argv = [a for a in week_report_argv if a != "--refresh"]
                run = week_report.run_report(week_report.parse_args(rerun_argv))
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
        print(f"autorun: {trigger.id} exited {rc}", file=sys.stderr)
        return False, None, research_result
    except Exception as exc:  # noqa: BLE001 -- one bad trigger must never take down the whole poll
        print(f"autorun: {trigger.id} failed ({exc})", file=sys.stderr)
        return False, None, research_result

    if research_result is not None:
        run.sections.insert(min(1, len(run.sections)), research_section(research_result))
    report_text = week_report.render_report(run.sections, week_num, "markdown")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    return True, run, research_result


def _fire_grade(
    trigger: Trigger, cfg: Config, season: int, week_num: int,
) -> "tuple[bool, tuple[str, str] | None]":
    """Run the projection grade (`scripts/grade_week.scheduled_grade`) -- no
    weekly report, no research. `(False, None)` when nothing has finished yet
    or it failed, so the next poll retries inside the grace window."""
    from scripts import grade_week

    print(f"autorun: firing {trigger.label!r} ({trigger.id})", file=sys.stderr)
    try:
        result = grade_week.scheduled_grade(season, week_num, cfg)
    except Exception as exc:  # noqa: BLE001 -- one bad trigger must never take down the whole poll
        print(f"autorun: {trigger.id} failed ({exc})", file=sys.stderr)
        return False, None
    for alert in result.alerts:
        print(f"autorun: grade: {alert}", file=sys.stderr)
    if result.week is None:
        return False, None
    return True, result.message


# Prefix on the result of a slot pass that had nothing to research. Not a
# failure, so a notification must never call it one.
_NOT_NEEDED = "not needed"
_MAX_CANDIDATES = 12


def _run_research(
    trigger: Trigger, run: "week_report.ReportRun", cfg: Config, season: int, week_num: int, games: dict,
) -> "research.ResearchResult":
    """One research pass for `trigger`, built from the live report `run`. A
    pre-kickoff pass with nothing on the slot's teams is skipped outright:
    none of your players or candidates play, so it would spend Claude usage
    researching games you have no stake in."""
    from ffbot import research
    from ffbot.report import default_weekly_path

    ctx = research_context(trigger, run, season, week_num, games)
    if ctx.mode == "slot" and not ctx.roster and not ctx.candidates:
        when = _clock(trigger.local_kickoff or trigger.kickoff)
        return research.ResearchResult(
            ok=False, alerts=[f"{_NOT_NEEDED} -- none of your players or candidates play at {when}"],
        )
    week_path = default_weekly_path(week_num)
    print(f"autorun: researching ({ctx.mode}) -> {week_path}", file=sys.stderr)
    return research.run_research(ctx, cfg.research, week_path, repo_root=Path("."))


def research_context(
    trigger: Trigger, run: "week_report.ReportRun", season: int, week_num: int, games: dict,
) -> "research.ResearchContext":
    """What the research run is told, from a live report. A pre-kickoff pass
    is scoped to the teams playing at that kickoff; every other pass covers
    the whole roster. Candidates are the plan's claims and adds plus the top
    streamers, so research reaches the players the waiver side is weighing."""
    from ffbot import research
    from ffbot.models import BENCH, IR_SLOTS

    slot = trigger.kind == "kickoff" and trigger.kickoff is not None
    slot_teams = tuple(sorted(t for t, g in games.items() if slot and g.kickoff == trigger.kickoff))

    def in_scope(team: str) -> bool:
        return (team in slot_teams) if slot else True

    roster: list[str] = []
    loaded = getattr(run, "loaded", None)
    for p in getattr(loaded, "players", None) or []:
        if not in_scope(p.team):
            continue
        pos = p.eligible_positions[0] if p.eligible_positions else ""
        if p.selected_position in IR_SLOTS:
            where = "IR"
        elif not p.selected_position or p.selected_position == BENCH:
            where = "bench"
        else:
            where = "starter"
        roster.append(f"{p.name} ({pos}, {p.team}, {where}, Sleeper status: {p.status or '-'})")

    seen: set[str] = set()
    candidates: list[str] = []

    def add(name: str, position: str, team: str, status: str = "") -> None:
        if not name or name in seen or not in_scope(team):
            return
        seen.add(name)
        fields = [f for f in (position, team, status) if f]
        candidates.append(f"{name} ({', '.join(fields)})")

    plan = getattr(run, "plan", None)
    for row in list(getattr(plan, "claims", None) or []) + list(getattr(plan, "adds", None) or []):
        # A row's `backups` are candidates the waiver side is weighing too.
        for r in (row, *getattr(row, "backups", ())):
            avail = getattr(r, "availability", None)
            add(
                r.add_name, getattr(r, "position", ""), getattr(r, "add_team", ""),
                avail.label() if avail is not None else "",
            )
    for rows in (getattr(run, "streamers", None) or {}).values():
        for c in rows[:3]:
            add(c.name, c.position, c.team)

    return research.ResearchContext(
        season=season,
        week=week_num,
        mode="slot" if slot else "full",
        kickoff_et=trigger.kickoff.isoformat(timespec="minutes") if slot else "",
        slot_teams=slot_teams,
        roster=tuple(roster),
        candidates=tuple(candidates[:_MAX_CANDIDATES]),
    )


def research_line(result: "research.ResearchResult") -> str:
    """One notification line saying whether research ran and what it found.
    An override is named, because it is a status that will beat Sleeper's."""
    if result.ok:
        if result.overrides:
            line = (
                f"Research: updated -- {len(result.overrides)} official status(es): "
                + "; ".join(result.overrides[:3])
            )
        else:
            line = "Research: updated -- no official status changes"
        if result.downgraded:
            line += f"; {len(result.downgraded)} unverified kept as notes"
        return line
    first = result.alerts[0] if result.alerts else "research did not complete"
    if first.startswith(_NOT_NEEDED):
        return f"Research: {first}"
    return f"Research FAILED: {first}"


def research_section(result: "research.ResearchResult") -> str:
    """The report file's own record of a research pass."""
    lines = ["RESEARCH", "-" * 60, f"  {research_line(result)}"]
    lines.extend(f"  status override: {o}" for o in result.overrides)
    lines.extend(f"  kept as a note (no official source): {d}" for d in result.downgraded)
    lines.extend(f"  {a}" for a in result.alerts[1:])
    if result.transcript:
        lines.append("  summary:")
        lines.extend(f"    {ln}" for ln in result.transcript.splitlines()[:12])
    return "\n".join(lines)


def actionable_summary(run: "week_report.ReportRun", min_waiver_net: float, cfg=None) -> list[str]:
    """What is worth waking a human up for. Empty means "stay quiet".

    A lineup move ALWAYS counts -- the tool wants a change made in the
    Sleeper app right now. A waiver row only counts when it is typed
    `kind == "claim"` (never `"add"`: HOLD-PRIORITY economics say "don't
    spend anything on this yet") AND its `net` clears `min_waiver_net`. A
    week where nothing clears the bar stays quiet rather than buzzing a
    phone for a marginal row.

    Returns the rendered SECTIONS, so `notification_for` can hand the same
    list to the transport whether or not there is anything else to add.
    """
    sections: dict[str, list[str]] = {}
    claims, adds = [], []
    for c in run.waivers:
        if c.net < min_waiver_net:
            continue
        if c.kind == "claim":
            claims.extend(move_line(run, c, "CLAIM"))
        elif c.kind == "add" and getattr(c, "availability", None) is not None:
            adds.extend(move_line(run, c, "ADD"))
    sections["WAIVER CLAIM"] = claims
    sections["ADD/DROP"] = adds

    if run.plan is not None and run.plan.start_sit:
        sections["START/SIT"] = start_sit_lines(run)
    elif getattr(getattr(run, "brief", None), "lineup", None) is not None and run.brief.lineup.moves:
        sections["START/SIT"] = _lineup_fallback_lines(run)

    if cfg is not None:
        sections["MONITOR"] = monitor_lines(run, cfg, pre_run=False)

    body = render_sections(sections)
    return [body] if body else []


def _backups_suffix(row) -> str:
    """` -- backups: GB, SF` when a row carries an ordered fallback (the other
    candidates for the same slot, or the other claims spending the same
    drop); empty otherwise."""
    backups = getattr(row, "backups", None) or ()
    if not backups:
        return ""
    return " -- backups: " + ", ".join(b.add_name for b in backups)


def availability_line(run: "week_report.ReportRun") -> str | None:
    """Whether this check knew who is a free agent and who is on waivers."""
    loaded = getattr(run, "loaded", None)
    if loaded is None or getattr(loaded, "availability_source", "off") == "off":
        return None
    if loaded.availability is not None:
        return loaded.availability.summary()
    return "Free-agent/waiver status UNKNOWN -- every add was priced as a waiver claim"


# --- The two waiver-cycle checks' messages ------------------------------------
#
# A check has a PURPOSE, and its message is shaped by it. The 2026-09-15
# waiver-claims check pushed the same lineup-first body a pre-kickoff check
# does: three lineup moves days before any game, then three defenses typed
# as free agents. The manager's call: Tuesday is claims (each with its
# ordered fallback) and what to leave for free agency, with no lineup lines
# at all; Wednesday is what happened at the run and who is worth a free
# pickup now; and a quiet check says so rather than sending nothing.


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _priority_line(run: "week_report.ReportRun", cfg) -> str | None:
    """`Rolling priority: you're 7th of 12` -- what a claim would spend."""
    priority = getattr(getattr(run, "loaded", None), "waiver_priority", None)
    if priority is None:
        return None
    num_teams = getattr(getattr(cfg, "draft", None), "num_teams", 0) or 0
    return f"Rolling priority: you're {_ordinal(int(priority))}" + (f" of {num_teams}" if num_teams else "")


def _gain_text(row) -> str:
    d = getattr(row, "decision", None)
    if d is None:
        return ""
    return f"{d.week_gain:+.1f} this wk, {d.ros_gain_per_week:+.1f}/wk ROS"


def _clears_text(row) -> str:
    clears = getattr(getattr(row, "availability", None), "clears_at", None)
    if clears is None:
        return ""
    from ffbot.availability import local_clock

    return f"clears {local_clock(clears)}"


def _who(row) -> str:
    return f"{getattr(row, 'position', '') or ''} {row.add_name}".strip()


def claim_line(row) -> str:
    """`CLAIM DEF Kansas City Chiefs (drop Detroit Lions) -- +3.6 this wk,
    -0.0/wk ROS; clears Wed 3:05AM; if it clears: start at DEF over Detroit
    Lions (+3.6 this week) -- backups: Green Bay Packers, San Francisco 49ers`"""
    if_clears = getattr(getattr(row, "if_clears", None), "text", "") or ""
    parts = [t for t in (_gain_text(row), _clears_text(row), if_clears) if t]
    body = f" -- {'; '.join(parts)}" if parts else ""
    return f"CLAIM {_who(row)} (drop {row.drop_name or '-'}){body}{_backups_suffix(row)}"


def _seat_note(run: "week_report.ReportRun", name: str) -> str:
    """`starts at DEF over Detroit Lions` when the plan seats a free-agent
    add -- the row carries its own consequence, so no lineup section is
    needed to say it."""
    for line in getattr(getattr(run, "plan", None), "start_sit", None) or []:
        if getattr(line, "kind", "") == "add_start" and getattr(line, "start_name", "") == name:
            slot = getattr(line, "slot_display", "") or getattr(line, "slot", "")
            over = getattr(line, "bench_name", "")
            return f"starts at {slot}" + (f" over {over}" if over else "")
    return ""


def add_line(run: "week_report.ReportRun", row) -> str:
    """`ADD (free agent) DEF Green Bay Packers (drop Detroit Lions) -- +3.7
    this wk, -0.0/wk ROS; starts at DEF over Detroit Lions -- backups: ...`"""
    parts = [t for t in (_gain_text(row), _seat_note(run, row.add_name)) if t]
    body = f" -- {'; '.join(parts)}" if parts else ""
    return f"ADD (free agent) {_who(row)} (drop {row.drop_name or '-'}){body}{_backups_suffix(row)}"


def _wait_line(rows) -> str | None:
    """`Wait for free agency (clears Wed 3:05AM): DEF Green Bay Packers (+3.7
    this wk), ...` -- the best rows on waivers that are NOT worth priority:
    the free-agent check's list, previewed."""
    waits = [r for r in rows if getattr(r, "kind", "") == "wait"][:3]
    if not waits:
        return None
    clears = next((t for t in (_clears_text(r) for r in waits) if t), "")
    items = []
    for r in waits:
        d = getattr(r, "decision", None)
        items.append(_who(r) + (f" ({d.week_gain:+.1f} this wk)" if d is not None else ""))
    return "Wait for free agency" + (f" ({clears})" if clears else "") + ": " + ", ".join(items)


# How many MONITOR rows reach a phone. Two is a glance; more is a list.
_SPECULATIVE_PUSH_LIMIT = 2


# --- The push body: instructions, not sentences ----------------------------
#
# This is the surface the manager actually reads (CLAUDE.md, "the push IS the
# product"). The first version wrote each recommendation as a prose clause
# with its full derivation attached, which on a phone produced lines like
# "WR Caleb Douglas (MIA) -- 8.5 this wk vs your Tyjae Spears 7.6; rostered
# in 21.9% -> 50.7% of leagues; 3,062,888 leagues added him in 48h" and drew
# the verdict "I have no idea what this means" (2026-09-16).
#
# The rules that replaced it, all from that feedback:
#
#   * LABELLED SECTIONS, in the order the work gets done: WAIVER CLAIM,
#     ADD/DROP, START/SIT, then MONITOR.
#   * ONE LINE PER TRANSACTION, not per component. An add, the drop that
#     pays for it and the lineup move it causes are a single thing you do in
#     the Sleeper app, so they read as one instruction -- "ADD x DROP y
#     START at RB" -- and the lineup half is NOT repeated under START/SIT.
#     `_consequence_names` is what keeps that promise.
#   * VERB-LED, capitalised: ADD, DROP, CLAIM, START, SIT, MOVE. The line is
#     a thing to do, not a finding to interpret.
#   * The DERIVATION stays out. Ownership percentages, league-add counts,
#     clear times, if-it-clears consequences and backup rationale all live in
#     the report file and the GUI. The push gets points and, at most, a
#     three-word reason.
#   * Nothing that is true every week earns a line. The availability
#     preamble ("free agency open since Wed 2:08AM...") was identical on
#     every run, so it was read once and thereafter only pushed the real
#     content further down the screen.
#
# MONITOR is specifically the NEAR MISSES -- players who came close to a bar
# and could clear it next week (the manager's definition, 2026-09-16). Three
# kinds qualify: the best row that did not clear `notify.min_waiver_net`, a
# row refused by the noise floor, and a speculative row (below your worst
# rostered player, but the league is moving on him). It is explicitly not a
# list of everything the scan rejected.
_SECTION_ORDER = ("WAIVER CLAIM", "ADD/DROP", "START/SIT", "MONITOR")


def _pts(value) -> str:
    return f"{value:+.1f}" if value is not None else ""


def _row_gain(row):
    d = getattr(row, "decision", None)
    return getattr(d, "week_gain", None) if d is not None else None


def _short_who(row) -> str:
    pos = getattr(row, "position", "") or ""
    return f"{pos} {row.add_name}".strip()


def _alt_line(row) -> str | None:
    """`alt: Green Bay, San Francisco` -- the backups for a move only ONE of
    which can be executed. Named, never explained."""
    names = [b.add_name for b in (getattr(row, "backups", ()) or ())][:2]
    return ("    alt: " + ", ".join(names)) if names else None


def _seat_clause(run: "week_report.ReportRun", name: str) -> str:
    """`START at RB over D'Andre Swift` when the plan seats this add.

    This is the blending: the lineup consequence rides on the add's own
    line, because in the Sleeper app it is the same visit.
    """
    for line in getattr(getattr(run, "plan", None), "start_sit", None) or []:
        if getattr(line, "kind", "") == "add_start" and getattr(line, "start_name", "") == name:
            slot = getattr(line, "slot_display", "") or getattr(line, "slot", "")
            over = getattr(line, "bench_name", "")
            return f"START at {slot}" + (f" over {over}" if over else "")
    return ""


def _consequence_names(run: "week_report.ReportRun") -> set:
    """Players whose lineup line is already carried by an add/claim line.

    START/SIT must not repeat them -- that is what made the old body read as
    two unrelated instructions for one move.
    """
    return {
        getattr(line, "start_name", "")
        for line in (getattr(getattr(run, "plan", None), "start_sit", None) or [])
        if getattr(line, "kind", "") == "add_start"
    }


def move_line(run: "week_report.ReportRun", row, verb: str) -> list[str]:
    """`CLAIM DEF Kansas City Chiefs  DROP Detroit Lions  START at DEF  +3.5`"""
    parts = [f"{verb} {_short_who(row)}"]
    if getattr(row, "drop_name", ""):
        parts.append(f"DROP {row.drop_name}")
    seat = _seat_clause(run, row.add_name)
    if seat:
        parts.append(seat)
    gain = _pts(_row_gain(row))
    line = "  " + "  ".join(parts) + (f"  {gain}" if gain else "")
    out = [line]
    alt = _alt_line(row)
    if alt:
        out.append(alt)
    return out


def start_sit_lines(run: "week_report.ReportRun", limit: int = 4) -> list[str]:
    """Pure lineup work -- the moves NOT already carried by an add's line.

    Built from `gameplan.SwapLine`'s typed fields rather than reformatting
    its rendered `.text`, which carries the reason clause this section drops.
    """
    plan = getattr(run, "plan", None)
    lines = list(getattr(plan, "start_sit", None) or []) if plan is not None else []
    already = _consequence_names(run)
    out: list[str] = []
    shown = 0
    for line in lines:
        kind = getattr(line, "kind", "")
        if kind == "add_start":
            continue  # blended onto the add's own line
        start = getattr(line, "start_name", "")
        if start and start in already:
            continue
        if shown >= limit:
            out.append(f"    +{len(lines) - shown} more")
            break
        bench = getattr(line, "bench_name", "")
        delta = _delta_of(line)
        if kind == "slot_shift":
            frm = getattr(line, "from_slot_display", "") or getattr(line, "from_slot", "")
            to = getattr(line, "slot_display", "") or getattr(line, "slot", "")
            out.append(f"  MOVE {start}  {frm} -> {to}".rstrip())
        elif start and bench:
            out.append(f"  START {start}  SIT {bench}  {_pts(delta)}".rstrip())
        elif start:
            slot = getattr(line, "slot_display", "") or getattr(line, "slot", "")
            out.append(f"  START {start}  at {slot}  {_pts(delta)}".rstrip())
        elif bench:
            out.append(f"  SIT {bench}  {_pts(delta)}".rstrip())
        else:
            continue
        shown += 1
    return out


def _delta_of(line):
    """This-week points the swap is worth, from the typed metrics rather
    than parsed out of the rendered text."""
    start = getattr(line, "start_proj", None)
    bench = getattr(line, "bench_proj", None)
    if start is None or bench is None:
        return None
    return start - bench


def _lineup_fallback_lines(run: "week_report.ReportRun", limit: int = 4) -> list[str]:
    """A board-less run has no `plan`, only the optimizer's raw moves."""
    moves = list(getattr(getattr(run, "brief", None), "lineup", None).moves or [])
    out = [f"  {m}" for m in moves[:limit]]
    if len(moves) > limit:
        out.append(f"    +{len(moves) - limit} more")
    return out


# Below these, a demand signal is not worth the width it takes on a phone:
# a one-point ownership drift and a handful of leagues are noise, and
# printing them implies an interest nobody has expressed.
_DEMAND_MIN_PCT_OWNED = 5.0
_DEMAND_MIN_LEAGUES = 50_000.0

_MISSED_CUTOFF = {
    "gain<=0": "below your bench",
    "noise_floor": "inside noise floor",
    "pool_truncation": "outside the scan",
}


def _demand_short(signals) -> str:
    """The single strongest piece of evidence, or "" when none is strong
    enough to earn a line.

    A share is a scale a human already holds; a raw count of Sleeper leagues
    is not, so it is divided down and only shown when it is large. A rival
    claim always wins -- twelve managers who share your wire beat a million
    who do not.
    """
    for d in signals:
        if d.unit == "claims":
            return d.text
    for d in signals:
        if d.unit == "pct_owned" and d.value >= _DEMAND_MIN_PCT_OWNED:
            return f"+{d.value:.0f}% owned"
    for d in signals:
        if d.unit == "leagues" and d.value >= _DEMAND_MIN_LEAGUES:
            return f"{d.value / 1000:.0f}k leagues adding"
    return ""


def monitor_lines(run: "week_report.ReportRun", cfg, pre_run: bool = True) -> list[str]:
    """Near misses: close to a bar this week, could clear it next week.

    Deliberately short and deliberately NOT a dump of everything the scan
    rejected -- that count lives in the report's own notes. Three sources,
    best first: the row that just missed `notify.min_waiver_net`, then
    speculative rows the league is moving on.
    """
    out: list[str] = []
    min_net = cfg.notify.min_waiver_net
    rows = list(getattr(run, "waivers", None) or [])
    near = [
        r for r in rows
        if getattr(r, "kind", "") in ("claim", "add") and 0.0 < r.net < min_net
    ]
    for r in near[:1]:
        out.append(f"  {_short_who(r)}  {_pts(_row_gain(r))}  under the {min_net:.1f} bar")

    for c in list(getattr(run, "speculative", []) or [])[:_SPECULATIVE_PUSH_LIMIT]:
        signals = [d for d in c.demand if not (pre_run and d.is_retrospective)]
        if not signals:
            # His ONLY evidence is a processed waiver claim, which does not
            # exist yet on a Tuesday. Showing him at all -- even under a
            # cutoff label -- would leak knowledge of a run that has not
            # happened, so he is dropped rather than relabelled.
            continue
        # Otherwise fall back to naming the CUTOFF he missed when no signal
        # is strong enough to be worth the width -- that is the section's
        # own definition, and it beats a one-point ownership drift.
        why = _demand_short(signals) or _MISSED_CUTOFF.get(getattr(c, "filtered_by", ""), "")
        if not why:
            continue
        vs = f" vs {c.drop_name}" if c.drop_name else ""
        out.append(f"  {_short_who(c)}  {_pts(c.week_delta)}{vs}  {why}")
    return out


def render_sections(sections: dict, tail=None) -> str:
    """`{"WAIVER CLAIM": [...], ...}` -> the push body.

    An empty section is omitted rather than rendered as a header with
    nothing under it: a heading that says "nothing here" costs the same
    screen space as one that says something.
    """
    blocks = []
    for name in _SECTION_ORDER:
        lines = sections.get(name) or []
        if lines:
            blocks.append("\n".join([name, *lines]))
    body = "\n\n".join(blocks)
    for extra in tail or []:
        if extra:
            body += ("\n\n" if body else "") + extra
    return body


def waiver_summary(run: "week_report.ReportRun", cfg) -> list[str]:
    """The Tuesday claims check.

    Deliberately NO start/sit section: the run is the night before waivers
    process, nothing has moved yet, and a lineup line here is noise you
    cannot act on -- the standing rule that each check's message is shaped
    by its purpose.
    """
    min_net = cfg.notify.min_waiver_net
    claims, adds = [], []
    for c in run.waivers:
        if c.net < min_net:
            continue
        if getattr(c, "kind", "") == "claim":
            claims.extend(move_line(run, c, "CLAIM"))
        elif getattr(c, "kind", "") == "add" and getattr(c, "availability", None) is not None:
            adds.extend(move_line(run, c, "ADD"))
    if not claims and not adds:
        return []

    sections = {"WAIVER CLAIM": claims, "ADD/DROP": adds,
                "MONITOR": monitor_lines(run, cfg, pre_run=True)}
    tail = [t for t in (_priority_line(run, cfg), _wait_line(run.waivers)) if t]
    body = render_sections(sections, tail=tail)
    return [body] if body else []


def _claim_outcome_lines(run: "week_report.ReportRun", say_none: bool = False) -> list[str]:
    """What Sleeper did with your claims at the run (`LoadedReport.claim_outcomes`).

    `say_none` is accepted and ignored. An explicit "No claim of yours was
    processed at the run." was removed on 2026-09-16 as unreadable: if you
    put no claims in, being told none processed answers a question you did
    not ask and reads as though something failed. A claim that DID process
    is still reported -- that one is news.
    """
    return [o.text() for o in (getattr(getattr(run, "loaded", None), "claim_outcomes", None) or [])]


def post_waiver_summary(run: "week_report.ReportRun", cfg) -> list[str]:
    """The Wednesday free-agent check: what the run did with your claims,
    then who to pick up. The rival-claim demand signal is knowable here and
    not on Tuesday, so MONITOR carries it (`pre_run=False`)."""
    min_net = cfg.notify.min_waiver_net
    adds = []
    for c in run.waivers:
        if getattr(c, "kind", "") == "add" and getattr(c, "availability", None) is not None and c.net >= min_net:
            adds.extend(move_line(run, c, "ADD"))
    outcomes = _claim_outcome_lines(run)
    if not adds and not outcomes:
        # MONITOR alone must never make a check "actionable" -- it is
        # information, not work. With nothing to do, the heartbeat carries
        # it instead, the same rule that stops a speculative row from ever
        # triggering a notification of its own.
        return []
    monitor = monitor_lines(run, cfg, pre_run=False)

    sections = {"ADD/DROP": adds, "MONITOR": monitor}
    head = "\n".join(["WAIVER RESULTS", *(f"  {o}" for o in outcomes)]) if outcomes else ""
    body = render_sections(sections)
    if head:
        body = head + ("\n\n" + body if body else "")
    return [body] if body else []


def _quiet_message(
    run: "week_report.ReportRun", trigger: Trigger, headline: str, lines: list[str],
    research: "research.ResearchResult | None", checked_at: datetime | None = None,
) -> tuple[str, str]:
    """A waiver-cycle check with nothing to push still says what it looked
    at -- same reasoning as `heartbeat_message`: the absence of the message
    must be the failure signal."""
    body = [*lines]
    # The availability preamble is gone: it said the same thing on every run
    # ("free agency open since Wed 2:08AM..."), so it was read once and
    # thereafter only pushed the real content down the screen. Research
    # status and feed health stay -- on a check with nothing to do, they are
    # the proof it ran at all.
    if research is not None:
        body.append(research_line(research))
    # Feed health is an ALARM, not a status line. An all-clear on every run
    # is read once and then ignored, which is exactly how a real degradation
    # gets missed; the report file records the full picture either way.
    health = _data_health(run)
    if health and not health.startswith("Live data: every"):
        body.append(health)
    body.append(f"Checked {_clock(checked_at or datetime.now())}")
    # A blank line before the housekeeping tail, so a MONITOR section above
    # it does not appear to continue into "Live data: ..." and "Checked ...".
    cut = len(lines)
    if cut and len(body) > cut:
        body = [*body[:cut], "", *body[cut:]]
    return f"ffbot W{run.week}: {trigger.label} -- {headline}", "\n".join(body)


def waiver_heartbeat(
    run: "week_report.ReportRun", trigger: Trigger, cfg,
    research: "research.ResearchResult | None" = None, checked_at: datetime | None = None,
) -> tuple[str, str]:
    lines = ["No claim worth your priority tonight."]
    prio = _priority_line(run, cfg)
    if prio:
        lines.append(prio)
    wait = _wait_line(run.waivers)
    if wait:
        lines.append(wait)
    if getattr(getattr(cfg, "autorun", None), "post_waiver_enabled", False):
        lines.append("The free-agent check after the run will say who to pick up.")
    monitor = monitor_lines(run, cfg, pre_run=True)
    if monitor:
        if lines:
            lines.append("")  # never open the body with a blank line
        lines.extend(["MONITOR", *monitor])
    else:
        closest = _closest_call(run, cfg.notify.min_waiver_net)
        if closest:
            lines.append(closest)
    return _quiet_message(run, trigger, "nothing worth a claim", lines, research, checked_at)


def post_waiver_heartbeat(
    run: "week_report.ReportRun", trigger: Trigger, cfg,
    research: "research.ResearchResult | None" = None, checked_at: datetime | None = None,
) -> tuple[str, str]:
    # No "Nothing worth a free-agent add." line: the title already says
    # "nothing worth adding", and repeating it in the body pushed the real
    # content down for no information.
    lines = _claim_outcome_lines(run, say_none=True)
    monitor = monitor_lines(run, cfg, pre_run=False)
    if monitor:
        if lines:
            lines.append("")  # never open the body with a blank line
        lines.extend(["MONITOR", *monitor])
    else:
        closest = _closest_call(run, cfg.notify.min_waiver_net)
        if closest:
            lines.append(closest)
    return _quiet_message(run, trigger, "nothing worth adding", lines, research, checked_at)


def notification_for(
    run: "week_report.ReportRun", trigger: Trigger, cfg: Config, games: dict,
    research: "research.ResearchResult | None" = None,
) -> "tuple[str, str] | None":
    """`(title, body)` to push for a completed check, or None to stay quiet.

    Pure, and shaped by `trigger.kind`. A pre-kickoff check sends its
    lineup-first `actionable_summary`, or an all-clear (`heartbeat_message`)
    when `cfg.notify.heartbeat` is on. The waiver-claims check sends
    `waiver_summary` -- claims with their fallback, never a lineup line --
    or, when nothing clears the bar, says so (`waiver_heartbeat`); the
    free-agent check sends `post_waiver_summary` or its own all-clear.
    Whenever a research pass ran, the message says how it went, and a pass
    that FAILED is reported even from a check that would otherwise stay
    quiet: a broken login would otherwise leave every check silently running
    on live data alone. Anything else stays quiet.
    """
    research_text = research_line(research) if research is not None else ""
    failed = research_text.startswith("Research FAILED")
    title = f"ffbot W{run.week}: {trigger.label}"
    if trigger.kind == "research":
        if research is None or not (failed or cfg.notify.heartbeat):
            return None
        body = [research_text]
        body.extend(f"Status override: {o}" for o in research.overrides)
        body.extend(f"Kept as a note (no official source): {d}" for d in research.downgraded)
        return title, "\n".join(body)

    def with_tail(lines: list[str]) -> str:
        # The availability preamble used to go here too; see `_quiet_message`.
        if research_text:
            lines = [*lines, research_text]
        return "\n".join(lines)

    if trigger.kind in ("waiver", "post_waiver"):
        body = waiver_summary(run, cfg) if trigger.kind == "waiver" else post_waiver_summary(run, cfg)
        if body:
            return title, with_tail(body)
        if cfg.notify.heartbeat:
            quiet = waiver_heartbeat if trigger.kind == "waiver" else post_waiver_heartbeat
            return quiet(run, trigger, cfg, research=research)
        if failed:
            return title, research_text
        return None
    summary = actionable_summary(run, cfg.notify.min_waiver_net, cfg)
    if summary:
        return title, with_tail(summary)
    if cfg.notify.heartbeat and trigger.kickoff is not None:
        return heartbeat_message(run, trigger, games, cfg.notify.min_waiver_net, research=research)
    if failed:
        return title, research_text
    return None


# The seams whose silent fallback would make an all-clear a lie.
_LIVE_SEAMS = ("projection", "roster", "slots", "league_rosters", "availability")


def heartbeat_message(
    run: "week_report.ReportRun", trigger: Trigger, games: dict, min_waiver_net: float,
    checked_at: datetime | None = None,
    research: "research.ResearchResult | None" = None,
) -> tuple[str, str]:
    """The pre-kickoff all-clear: a check that found nothing to change still
    says so, with enough detail to prove it actually looked.

    A quiet check used to produce no output at all, which made "ran and found
    nothing" indistinguishable from "never ran" -- on 2026-09-10 the 18:47
    check ran correctly and, forty minutes from kickoff, there was no way to
    tell. So every line is evidence rather than reassurance: which starters
    lock at this kickoff (from the live schedule), the lineup's projected
    total (it moves with live projections), the closest call the plan looked
    at and declined, and whether every live feed answered or fell back. With
    this in place, the ABSENCE of the message is the failure signal.
    """
    local = trigger.local_kickoff or trigger.kickoff
    when = _clock(local)
    title = f"ffbot W{run.week}: all clear for {local:%a} {when} kickoff"
    lines = ["No lineup changes. Nothing worth a waiver claim."]

    plan = getattr(run, "plan", None)
    lineup = plan.current_plan if plan is not None else run.brief.lineup
    assignments = list(getattr(lineup, "assignments", None) or [])
    locking = [
        f"{p.name} ({slot})" for slot, p in assignments
        if (g := games.get(p.team)) is not None and g.kickoff == trigger.kickoff
    ]
    if locking:
        lines.append(f"Locking at {when}: " + ", ".join(locking))
    else:
        lines.append(f"None of your starters play at {when}.")
    if assignments:
        total = sum(p.projected_points or 0.0 for _, p in assignments)
        lines.append(f"Projected lineup: {total:.1f} pts")

    closest = _closest_call(run, min_waiver_net)
    if closest:
        lines.append(closest)
    avail_text = availability_line(run)
    if avail_text:
        lines.append(avail_text)
    if research is not None:
        lines.append(research_line(research))
    health = _data_health(run)
    if health:
        lines.append(health)
    lines.append(f"Checked {_clock(checked_at or datetime.now())}")
    return title, "\n".join(lines)


def _closest_call(run: "week_report.ReportRun", min_waiver_net: float) -> str | None:
    """The best row the plan looked at and did NOT push -- proof the waiver
    side ran, and the one thing worth a glance if you disagree with it.
    `run.waivers` is claims first, then adds, each best-first."""
    rows = list(getattr(run, "waivers", None) or [])
    if not rows:
        return None
    r = rows[0]
    pos = getattr(r, "position", "")
    swap = (f"{pos} " if pos else "") + r.add_name + (f" for {r.drop_name}" if r.drop_name else "")
    decision = getattr(r, "decision", None)
    gain = f", {decision.week_gain:+.1f} pts this week" if decision is not None else ""
    note = getattr(r, "claim_note", "") or ""
    if r.kind == "claim":
        why = f"claim worth {r.net:+.1f}, under your {min_waiver_net:g}-point notify bar"
    elif r.kind == "add" and getattr(r, "availability", None) is not None:
        why = f"free-agent add worth {r.net:+.1f}, under your {min_waiver_net:g}-point notify bar"
    elif note.startswith("WAIT FOR FREE AGENCY"):
        why = "wait for free agency"
    elif note.startswith("HOLD PRIORITY"):
        why = "not worth a waiver claim"
    else:
        why = "an ordinary add, not a claim"
    return f"Closest call: {swap}{gain} ({why})"


def _data_health(run: "week_report.ReportRun") -> str | None:
    """Whether every live Sleeper feed actually answered this run. Reads
    `week_log.live_sources` -- the same summary the week log records -- and
    deliberately not the alert lists, which carry a permanent scoring note
    even on a perfectly healthy run."""
    loaded = getattr(run, "loaded", None)
    if loaded is None:
        return None
    from ffbot import week_log

    sources = week_log.live_sources(loaded)
    fell_back = [
        f"{k}={sources.get(k)}" for k in _LIVE_SEAMS
        if sources.get(k) != "sleeper" and not (k == "availability" and sources.get(k) in (None, "off"))
    ]
    if not fell_back:
        return "Live data: every Sleeper feed answered."
    return "NOT fully live: " + ", ".join(fell_back) + " -- check the report."


def _refresh_league_rosters(args: argparse.Namespace) -> None:
    """Best-effort `import_league_rosters.py --live` refresh, gated by
    `--refresh-league-rosters`. Any failure prints a warning and continues
    -- a stale league_rosters.yml degrades denial/free-agent-pool accuracy,
    it never crashes an otherwise-working scheduled run."""
    from scripts import import_league_rosters

    try:
        rc = import_league_rosters.main([
            "--live", "--config", args.config, "--out", args.league_rosters,
        ])
        if rc != 0:
            print(f"autorun: league_rosters.yml refresh exited {rc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- a stale roster file is better than a crashed poll
        print(f"autorun: league_rosters.yml refresh failed ({exc})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # A legacy Windows console is cp1252; this repo's prose is not.
    # Without this the whole run completes and then dies on print.
    make_streams_safe()
    args = parse_args(argv)
    if args.chdir:
        os.chdir(args.chdir)
    cfg = Config.load(args.config)
    now = datetime.now()

    season = args.season if args.season is not None else projections.current_nfl_season()
    try:
        week_num = args.week if args.week is not None else current_week(season)
        games = this_week_games(season, week_num)
    except ScheduleError as exc:
        print(f"autorun: schedule fetch failed ({exc}) -- cannot determine this week's triggers", file=sys.stderr)
        return 1

    # The waiver-claims check's slot comes from config.yml's `autorun:`
    # block; the command-line flags (what the registered task passes) win
    # when given. A typo'd config weekday falls back to the dataclass
    # default, loudly.
    waiver_weekday = args.waiver_weekday or cfg.autorun.waiver_weekday
    if waiver_weekday not in _WEEKDAYS:
        print(
            f"autorun: autorun.waiver_weekday {waiver_weekday!r} is not one of {sorted(_WEEKDAYS)} "
            "-- using tue",
            file=sys.stderr,
        )
        waiver_weekday = "tue"
    waiver_hour = args.waiver_hour if args.waiver_hour is not None else cfg.autorun.waiver_hour
    triggers = build_triggers(
        games, now, args.lead_minutes, waiver_weekday, waiver_hour,
        to_local=eastern_to_local,
        research_weekday=cfg.research.injury_report_weekday if cfg.research.enabled else None,
        research_hour=cfg.research.injury_report_hour,
        grade_weekday=cfg.grade.weekday if cfg.grade.enabled else None,
        grade_hour=cfg.grade.hour,
        post_waiver_weekday=cfg.autorun.post_waiver_weekday if cfg.autorun.post_waiver_enabled else None,
        post_waiver_hour=cfg.autorun.post_waiver_hour,
    )

    if args.dry_run:
        print(f"season {season}, week {week_num} -- {len(triggers)} trigger(s):")
        for t in sorted(triggers, key=lambda t: t.due_at):
            window = f"{t.due_at:%a %Y-%m-%d %H:%M} .. +{t.grace_minutes:.0f}min"
            print(f"  {t.id:<40} due {window}   {t.label}")
        notify_note = (
            f" (min_waiver_net={cfg.notify.min_waiver_net}, heartbeat={cfg.notify.heartbeat})"
            if cfg.notify.channel != "off" else ""
        )
        print(f"notify: channel={cfg.notify.channel!r}{notify_note}")
        if cfg.research.enabled:
            from ffbot import research

            cli = research.resolve_claude(cfg.research)
            print(f"research: on (Claude Code CLI: {cli or 'NOT FOUND -- set research.claude_path'})")
        else:
            print("research: off")
        return 0

    state_path = Path(args.state_file)
    state = _load_state(state_path)
    fired: set[str] = set(state.get(f"{season}-w{week_num:02d}", []))

    due = [t for t in triggers if _is_due(t, now, fired)]
    if not due:
        return 0

    if args.refresh_league_rosters:
        _refresh_league_rosters(args)

    week_key = f"{season}-w{week_num:02d}"
    for trigger in sorted(due, key=lambda t: t.due_at):
        if trigger.kind == "grade":
            ok, message = _fire_grade(trigger, cfg, season, week_num)
            if not ok:
                continue
            fired.add(trigger.id)
            state[week_key] = sorted(fired)
            _save_state(state_path, state)
            if message is not None and cfg.notify.channel != "off":
                from ffbot import notify

                for alert in notify.send(cfg.notify, *message):
                    print(f"autorun: notify: {alert}", file=sys.stderr)
            continue

        # The free-agent check never researches: Tuesday's full pass already
        # covers the week (and its failure was reported Tuesday).
        research_key = f"research:{trigger.id}"
        ok, run, research_result = _fire(
            trigger, args, cfg, season, week_num, games=games,
            skip_research=research_key in fired or trigger.kind == "post_waiver",
        )
        if research_result is not None:
            # Recorded even when the report after it failed: the retry on the
            # next poll re-runs the report, not another paid research pass.
            fired.add(research_key)
            state[week_key] = sorted(fired)
            _save_state(state_path, state)
        if not ok:
            continue
        fired.add(trigger.id)
        state[week_key] = sorted(fired)
        _save_state(state_path, state)  # persisted per-trigger, not batched -- a mid-run crash still keeps earlier successes recorded

        # A successful fire's own failure to NOTIFY never un-marks it as
        # fired -- the report itself is the artifact of record; a missed
        # push is a lesser problem than re-running (and re-notifying for)
        # an already-completed check.
        if run is not None and cfg.notify.channel != "off":
            message = notification_for(run, trigger, cfg, games, research=research_result)
            if message is not None:
                from ffbot import notify

                title, body = message
                for alert in notify.send(cfg.notify, title, body):
                    print(f"autorun: notify: {alert}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
