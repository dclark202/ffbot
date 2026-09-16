#!/usr/bin/env python3
"""Grade projections against real results, and propose -- never make -- a dial change.

    python scripts/grade_week.py --week 1 --fetch
    python scripts/grade_week.py --all --fetch        # every logged week, aggregated

Reads the per-run logs in `weekly/reports/` (`ffbot/week_log.py`) and, per
adjustment family (weather, Vegas, opponent...), reports whether our changes
to Sleeper's projection moved it toward what actually happened. Writes
`weekly/grades/{season}-wNN.json` (or `-season.json` with `--all`).

`--fetch` fills in actual points for players whose FINAL score isn't already
in a log, from Sleeper's league matchups (rostered players only) -- and only
for games the schedule says are over. It also groups players by game, which
the evidence needs.

`scripts/autorun.py` runs `scheduled_grade` every Tuesday morning
(`config.yml`'s `grade:` block): last week's grade, the season's per-game
evidence, and a PROPOSAL only when an adjustment has been consistently wrong
or right over enough weeks and games. Nothing here writes configuration.
Descriptive only -- see `ffbot/week_grade.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import week_grade as wg  # noqa: E402


@dataclass
class LiveContext:
    fetched: dict[int, dict[str, float]] = field(default_factory=dict)
    finished: dict[int, set[str]] = field(default_factory=dict)
    opponents: dict[int, dict[str, str]] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)


def live_context(
    season: int,
    weeks: Iterable[int],
    league_id: str,
    client,
    schedule_fn: Optional[Callable] = None,
    now_utc: Optional[datetime] = None,
) -> LiveContext:
    """Schedule (who played whom, which games are over) and Sleeper scores for
    `weeks`. Every failure is an alert, never a raise. `schedule_fn` and
    `client` are injectable."""
    from ffbot.live import schedule

    schedule_fn = schedule_fn or schedule.this_week_games
    now_utc = now_utc or datetime.now(timezone.utc)
    ctx = LiveContext()
    for i, wk in enumerate(sorted(weeks)):
        try:
            # One season file covers every week: download it once per run.
            games = schedule_fn(season, wk, refresh=(i == 0))
        except schedule.ScheduleError as exc:
            ctx.alerts.append(
                f"week {wk}: schedule unavailable ({exc}) — can't tell which games are over, "
                "so no fetched score is used."
            )
            continue
        ctx.opponents[wk] = {t.upper(): (g.opponent or "").upper() for t, g in games.items()}
        ctx.finished[wk] = wg.finished_teams(games, now_utc, schedule.eastern_to_utc)
        if client is not None and league_id:
            ctx.fetched[wk], alerts = wg.fetch_actuals(client, league_id, wk)
            ctx.alerts.extend(alerts)
    return ctx


def _write(out_dir: Path, name: str, payload: dict) -> Optional[str]:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return None
    except OSError as exc:
        return f"could not write {out_dir / name}: {exc}"


@dataclass
class ScheduledGrade:
    week: Optional[int]  # None: nothing finished to grade -- autorun retries on its next poll
    message: Optional[tuple[str, str]]
    alerts: list[str]
    proposals: list = field(default_factory=list)


def scheduled_grade(
    season: int,
    current_week: int,
    cfg,
    *,
    log_dir: Path | str = wg.WEEK_LOG_DIR,
    out_dir: Path | str = wg.GRADE_DIR,
    client=None,
    schedule_fn: Optional[Callable] = None,
    now_utc: Optional[datetime] = None,
) -> ScheduledGrade:
    """What `scripts/autorun.py`'s Tuesday trigger runs.

    Grades the latest logged week, at or before `current_week`, whose every
    game is over. (On a Tuesday `schedule.current_week` has already moved on
    to the coming week, so this is last week.) Then grades every logged week
    up to it for per-game evidence, writes both, and returns the push."""
    logs = wg.load_week_logs(season, None, log_dir)
    weeks = sorted(w for w in logs if w <= current_week)
    if not weeks:
        return ScheduledGrade(None, None, [f"no week logs for {season} in {log_dir}"])

    league_id = cfg.sleeper.league_id
    if client is None and league_id:
        from ffbot.sleeper.client import SleeperClient

        client = SleeperClient()
    ctx = live_context(season, weeks, league_id, client, schedule_fn, now_utc)
    alerts = list(ctx.alerts)
    if not league_id:
        alerts.append("no sleeper.league_id — grading only FINAL points already in the logs.")

    target = next(
        (wk for wk in reversed(weeks) if ctx.opponents.get(wk) and set(ctx.opponents[wk]) <= ctx.finished.get(wk, set())),
        None,
    )
    if target is None:
        return ScheduledGrade(None, None, alerts + ["no logged week has finished yet"])

    week_grade = wg.grade(season, {target: logs[target]}, ctx.fetched, alerts, ctx.finished)
    season_grade = wg.grade(season, {w: logs[w] for w in weeks if w <= target}, ctx.fetched, [], ctx.finished)
    evidence = wg.family_evidence(season_grade.players, ctx.opponents, cfg.grade.z)
    props = wg.proposals(evidence, cfg.grade.min_weeks, cfg.grade.min_games)

    out = Path(out_dir)
    for name, payload in (
        (f"{season}-w{target:02d}.json", wg.to_json(week_grade)),
        (f"{season}-season.json", wg.to_json(season_grade, evidence, props)),
    ):
        err = _write(out, name, payload)
        if err:
            alerts.append(err)

    title = f"ffbot W{target}: projections graded" + (" — a dial change is proposed" if props else "")
    body = "\n".join(wg.notification_lines(week_grade, evidence, props, cfg.grade.min_weeks, cfg.grade.min_games))
    return ScheduledGrade(target, (title, body), alerts, props)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--week", type=int, help="grade one week")
    which.add_argument("--all", action="store_true", help="grade and aggregate every logged week")
    p.add_argument("--season", type=int, default=None, help="default: the current NFL season")
    p.add_argument("--fetch", action="store_true", help="fetch the schedule and fill missing actuals from Sleeper")
    p.add_argument("--config", default="config.yml")
    p.add_argument("--log-dir", default=str(wg.WEEK_LOG_DIR))
    p.add_argument("--out-dir", default=str(wg.GRADE_DIR))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from ffbot.config import Config

    cfg = Config.load(args.config)
    if args.season is None:
        from ffbot import projections

        args.season = projections.current_nfl_season()

    logs = wg.load_week_logs(args.season, None if args.all else args.week, args.log_dir)
    if not logs:
        print(f"No week logs for {args.season}{'' if args.all else f' week {args.week}'} in {args.log_dir}.")
        return 1

    ctx = LiveContext()
    if args.fetch:
        league_id = cfg.sleeper.league_id
        client = None
        if league_id:
            from ffbot.sleeper.client import SleeperClient

            client = SleeperClient()
        ctx = live_context(args.season, list(logs), league_id, client)
        if not league_id:
            ctx.alerts.append("--fetch needs sleeper.league_id in config — grading logged FINAL points only.")
    else:
        ctx.alerts.append("no --fetch: players are grouped by team, not game, and only logged FINAL points count.")

    grade = wg.grade(args.season, logs, ctx.fetched, ctx.alerts, ctx.finished)
    evidence = wg.family_evidence(grade.players, ctx.opponents, cfg.grade.z)
    props = wg.proposals(evidence, cfg.grade.min_weeks, cfg.grade.min_games)
    print("\n".join(wg.render(grade, evidence, props)))

    name = f"{args.season}-season.json" if args.all else f"{args.season}-w{args.week:02d}.json"
    err = _write(Path(args.out_dir), name, wg.to_json(grade, evidence, props))
    print(f"\n{err}" if err else f"\nwrote {Path(args.out_dir) / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
