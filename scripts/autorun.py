#!/usr/bin/env python3
"""Unattended trigger brain for the weekly report.

Runs on a schedule (Windows Task Scheduler, every ~15 min is the
recommended interval) and decides whether anything is due right now: a
per-kickoff-slot pre-game check (~2h before each DISTINCT kickoff time
this week's games use -- Thursday night, Sunday early/late, Sunday night,
Monday night are typically five separate slots, not one) and a
pre-waiver check (a configurable weekday/hour ahead of this league's
rolling-priority waiver processing). Each trigger
that fires runs the exact same scripts/week_report.py pipeline, with
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
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import projections  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.live.schedule import ScheduleError, current_week, this_week_games  # noqa: E402
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--roster", default="roster.yml", help="path to roster.yml")
    p.add_argument("--season", type=int, default=None, help="override the inferred current NFL season")
    p.add_argument("--week", type=int, default=None, help="override the inferred current NFL week")
    p.add_argument("--lead-minutes", type=float, default=120.0, help="how long before each kickoff slot to fire the pre-game check (default: 120 = 2h)")
    p.add_argument("--waiver-weekday", choices=list(_WEEKDAYS), default="tue", help="weekday the pre-waiver check fires on -- ADJUST to match this league's real waiver-processing day (default: tue)")
    p.add_argument("--waiver-hour", type=int, default=20, help="local hour 0-23 the pre-waiver check fires at (default: 20 = 8pm)")
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


def build_triggers(
    games: dict, now: datetime, lead_minutes: float, waiver_weekday: str, waiver_hour: int,
) -> list[Trigger]:
    """Every trigger for the current week: one per DISTINCT kickoff time
    (several games routinely share a slot -- one trigger covers all of
    them) plus one pre-waiver check. `Trigger.id` must stay stable across
    polls within the same week; it's what the state file's idempotency
    keys on.
    """
    triggers: list[Trigger] = []

    distinct_kickoffs = sorted({g.kickoff for g in games.values() if g.kickoff is not None})
    for kickoff in distinct_kickoffs:
        due_at = kickoff - timedelta(minutes=lead_minutes)
        triggers.append(
            Trigger(
                id=f"pre_kickoff_{kickoff.isoformat()}",
                due_at=due_at,
                grace_minutes=lead_minutes + _KICKOFF_GRACE_MINUTES,
                label=f"pre-kickoff check for {kickoff.strftime('%a %I:%M%p').lstrip('0')} games",
            )
        )

    waiver_due = _this_calendar_week_at(now, _WEEKDAYS[waiver_weekday], waiver_hour)
    triggers.append(
        Trigger(
            id=f"pre_waiver_{waiver_due.date().isoformat()}",
            due_at=waiver_due,
            grace_minutes=_WAIVER_GRACE_MINUTES,
            label=f"pre-waiver check ({waiver_weekday} {waiver_hour:02d}:00)",
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
) -> "tuple[bool, week_report.ReportRun | None]":
    """Run `week_report.run_report`'s exact pipeline for `trigger`, writing
    reports/YYYY-wNN-<trigger-suffix>.md, and hand back the structured
    `ReportRun` too (for `actionable_summary`/notifications below) instead
    of only the rendered text `week_report.main()` used to produce.
    `--refresh` is always passed -- the whole point of a pre-kickoff/
    pre-waiver check firing at a specific moment is that it wants the
    latest information right then, not whatever happened to be cached from
    an earlier poll this week.

    Returns `(True, run)` on success; `(False, None)` on failure, so the
    caller does NOT mark the trigger fired -- an unattended run that failed
    should be retried on the next poll, within the trigger's own grace
    window, not silently treated as done.
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
    ]
    if stream_positions:
        week_report_argv += ["--stream", *stream_positions]
    if args.waivers:
        week_report_argv.append("--waivers")
    if args.priority is not None:
        week_report_argv += ["--priority", str(args.priority)]

    print(f"autorun: firing {trigger.label!r} ({trigger.id}) -> {out_path}", file=sys.stderr)
    try:
        run = week_report.run_report(week_report.parse_args(week_report_argv))
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
        print(f"autorun: {trigger.id} exited {rc}", file=sys.stderr)
        return False, None
    except Exception as exc:  # noqa: BLE001 -- one bad trigger must never take down the whole poll
        print(f"autorun: {trigger.id} failed ({exc})", file=sys.stderr)
        return False, None

    report_text = week_report.render_report(run.sections, week_num, "markdown")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    return True, run


def actionable_summary(run: "week_report.ReportRun", min_waiver_net: float) -> list[str]:
    """Pure -- what's worth waking a human up for from a completed
    unattended run. A non-empty return means "notify"; an empty one means
    "quiet, nothing to act on."

    Lineup moves ALWAYS count (the tool wants a real change made in the
    Sleeper app right now) -- read from `run.plan.start_sit` (the
    `ffbot.gameplan` engine's readable lines) when a plan was built
    (`--waivers`), falling back to `run.brief.lineup.moves` on a board-less
    run. A waiver candidate only counts when it's typed `kind == "claim"`
    (never `"add"` -- HOLD-PRIORITY economics, "don't spend anything on
    this yet") AND its `net` clears `min_waiver_net` -- a week where
    nothing clears the bar to be worth a real claim should stay quiet, not
    buzz a phone for a marginal one.
    """
    lines: list[str] = []
    if run.plan is not None and run.plan.start_sit:
        lines.append(f"Lineup: {len(run.plan.start_sit)} move(s)")
        lines.extend(m.text for m in run.plan.start_sit[:3])
    elif run.brief.lineup.moves:
        lines.append(f"Lineup: {len(run.brief.lineup.moves)} move(s)")
        lines.extend(str(m) for m in run.brief.lineup.moves[:3])
    for c in run.waivers:
        if c.kind == "claim" and c.net >= min_waiver_net:
            lines.append(f"CLAIM {c.add_name} (net {c.net:+.1f}, drop {c.drop_name or '-'})")
    return lines


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
    args = parse_args(argv)
    cfg = Config.load(args.config)
    now = datetime.now()

    season = args.season if args.season is not None else projections.current_nfl_season()
    try:
        week_num = args.week if args.week is not None else current_week(season)
        games = this_week_games(season, week_num)
    except ScheduleError as exc:
        print(f"autorun: schedule fetch failed ({exc}) -- cannot determine this week's triggers", file=sys.stderr)
        return 1

    triggers = build_triggers(games, now, args.lead_minutes, args.waiver_weekday, args.waiver_hour)

    if args.dry_run:
        print(f"season {season}, week {week_num} -- {len(triggers)} trigger(s):")
        for t in sorted(triggers, key=lambda t: t.due_at):
            window = f"{t.due_at:%a %Y-%m-%d %H:%M} .. +{t.grace_minutes:.0f}min"
            print(f"  {t.id:<40} due {window}   {t.label}")
        notify_note = f" (min_waiver_net={cfg.notify.min_waiver_net})" if cfg.notify.channel != "off" else ""
        print(f"notify: channel={cfg.notify.channel!r}{notify_note}")
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
        ok, run = _fire(trigger, args, cfg, season, week_num)
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
            summary = actionable_summary(run, cfg.notify.min_waiver_net)
            if summary:
                from ffbot import notify

                title = f"ffbot W{week_num}: {trigger.label}"
                body = "\n".join(summary)
                for alert in notify.send(cfg.notify, title, body):
                    print(f"autorun: notify: {alert}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
