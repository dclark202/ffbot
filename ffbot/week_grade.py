"""Grades a week's projections against what actually happened.

Every weekly run already writes `weekly/reports/{season}-wNN-{source}.json`
(`ffbot/week_log.py`) with, per player, Sleeper's own projection
(`sleeper_proj`), ours (`week_proj`), each named adjustment that turned one
into the other (weather, Vegas, opponent correlation...), and -- once his game
is over -- his real points. Nothing read those back. This does: for each
adjustment family it asks whether the adjustment moved the projection toward
the real result or away from it, on the same rows, in points.

It exists because week 1 of 2026 needed it and had nothing: a researched
40 mph wind (the real one was ~6) cut four JAX/CLE players, and three of them
beat Sleeper's uncut number. That was found by hand.

Descriptive only, the same contract as `season_ptd`: nothing in valuation
imports this module, and one week moves no dial -- every result here is a
hypothesis for a backtest, per docs/dev/INSEASON-FINDINGS.md. It lives
outside `ffbot/history/` and `ffbot/backtest/` because it reads live logs and
live results, not point-in-time replay.

Which projection is graded: the latest snapshot taken BEFORE the player's game
started (`game_state` empty). A later snapshot may have re-projected with
different inputs, and a pre-kickoff number is the one a decision was made on.
Which actual: `live_pts` from a snapshot where his game is `FINAL`, else a
fetched Sleeper `players_points` (rostered players league-wide) -- but only
for a team whose game the schedule says is over (`finished_teams`), because
that feed reports an unstarted game as 0 and a live one as a running total.
A player with neither is ungraded, never guessed.

Pure except `load_week_logs` (reads files) and `fetch_actuals` (network, with
an injectable client, degrading to an alert).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from .names import normalize_name

WEEK_LOG_DIR = Path("weekly/reports")
GRADE_DIR = Path("weekly/grades")


@dataclass(frozen=True)
class ProjectedRow:
    key: str
    name: str
    position: str
    team: str
    sleeper_proj: Optional[float]
    week_proj: float
    adjustments: tuple[tuple[str, float], ...]
    snapshot: str  # generated_at of the snapshot this projection came from
    source: str = "pre_kickoff"  # "pre_kickoff" | "post_kickoff" | "legacy" -- see `collect`

    @property
    def baseline(self) -> float:
        """Sleeper's number, or ours minus every named adjustment when the row
        has no Sleeper projection (an offline-sourced row)."""
        if self.sleeper_proj is not None:
            return self.sleeper_proj
        return self.week_proj - sum(d for _, d in self.adjustments)


@dataclass(frozen=True)
class PlayerGrade:
    row: ProjectedRow
    actual: float
    week: int

    @property
    def baseline_err(self) -> float:
        return abs(self.actual - self.row.baseline)

    @property
    def ours_err(self) -> float:
        return abs(self.actual - self.row.week_proj)

    def family_gain(self, family: str) -> float:
        """Points of absolute error this family's adjustments removed (positive)
        or added (negative), holding every other adjustment on the row fixed."""
        delta = family_delta(self.row, family)
        without = self.row.week_proj - delta
        return abs(self.actual - without) - self.ours_err


@dataclass(frozen=True)
class FamilyGrade:
    family: str
    rows: int
    total_delta: float  # sum of the adjustments applied, points
    helped: int
    hurt: int
    net_points: float  # sum of family_gain: positive means the family helped
    names: tuple[str, ...]


@dataclass(frozen=True)
class PositionGrade:
    position: str
    rows: int
    baseline_mae: float
    ours_mae: float


@dataclass
class WeekGrade:
    season: int
    weeks: tuple[int, ...]
    players: list[PlayerGrade]
    ungraded: list[ProjectedRow]
    families: list[FamilyGrade] = field(default_factory=list)
    positions: list[PositionGrade] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


def family_of(label: str) -> str:
    """`"weather: wind 40 mph, ..."` -> `"weather"`; `"opponent"` -> `"opponent"`."""
    return label.split(":", 1)[0].strip()


def family_delta(row: ProjectedRow, family: str) -> float:
    return sum(d for label, d in row.adjustments if family_of(label) == family)


def load_week_logs(season: int, week: Optional[int] = None, log_dir: Path | str = WEEK_LOG_DIR) -> dict[int, list[dict]]:
    """`{week: [log, ...]}` sorted by `generated_at`. Unreadable files are skipped."""
    out: dict[int, list[dict]] = {}
    for path in sorted(Path(log_dir).glob(f"{season}-w*.json")):
        try:
            log = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        wk = log.get("week")
        if not isinstance(wk, int) or log.get("season") != season or (week is not None and wk != week):
            continue
        out.setdefault(wk, []).append(log)
    for logs in out.values():
        logs.sort(key=lambda l: str(l.get("generated_at", "")))
    return out


def _metric_blocks(log: dict) -> Iterable[dict]:
    for lineup in ("lineup_current", "lineup_recommended"):
        for section in ("starters", "bench"):
            for r in (log.get(lineup) or {}).get(section) or []:
                yield r.get("metrics")
    for r in log.get("start_sit") or []:
        yield r.get("start_metrics")
        yield r.get("bench_metrics")
    for kind in ("adds", "claims"):
        for r in log.get(kind) or []:
            yield r.get("add_metrics")
            yield r.get("drop_metrics")


def _key(m: dict) -> str:
    return m.get("board_key") or f"{normalize_name(m.get('name', ''))}:{m.get('position', '')}"


def _row(key: str, m: dict, stamp: str, source: str) -> ProjectedRow:
    return ProjectedRow(
        key=key,
        name=m.get("name", ""),
        position=m.get("position", ""),
        team=m.get("team", "") or "",
        sleeper_proj=m.get("sleeper_proj"),
        week_proj=float(m["week_proj"]),
        adjustments=tuple((a["label"], float(a["delta"])) for a in m.get("adjustments") or []),
        snapshot=stamp,
        source=source,
    )


def collect(logs: list[dict]) -> tuple[dict[str, ProjectedRow], dict[str, float]]:
    """`(projection per player, FINAL points per player)` from one week's logs,
    which must be sorted oldest first.

    Which snapshot's projection, in order of preference:
      1. `pre_kickoff` -- the latest one taken before his game started that
         carries the adjustment breakdown. The number a decision was made on.
      2. `post_kickoff` -- the earliest one taken after kickoff that carries it.
         A started game's projection is the pre-game number as displayed, not
         re-projected, so this is the same number one run later -- flagged
         anyway, so a reader can tell.
      3. `legacy` -- the latest log from before the breakdown existed (no
         `adjustments`/`game_state` keys): a projection with nothing to
         attribute. Week 1 of 2026 is the case: its only JAX pre-kickoff
         snapshot predates the schema, and the wind cut is visible only after.
    """
    pre: dict[str, ProjectedRow] = {}
    post: dict[str, ProjectedRow] = {}
    legacy: dict[str, ProjectedRow] = {}
    finals: dict[str, float] = {}
    for log in logs:
        stamp = str(log.get("generated_at", ""))
        for m in _metric_blocks(log):
            if not m or m.get("week_proj") is None:
                continue
            key = _key(m)
            state = m.get("game_state") or ""
            if state == "FINAL" and m.get("live_pts") is not None:
                finals[key] = float(m["live_pts"])
            if "adjustments" not in m:
                legacy[key] = _row(key, m, stamp, "legacy")
            elif not state:
                pre[key] = _row(key, m, stamp, "pre_kickoff")
            elif key not in post:
                post[key] = _row(key, m, stamp, "post_kickoff")
    rows = {**legacy, **post, **pre}
    return rows, finals


GAME_HOURS = 4.5  # kickoff to a safely final score, generous for overtime


def finished_teams(games: dict, now_utc: datetime, eastern_to_utc: Callable[[datetime], datetime]) -> set[str]:
    """Teams whose game this week kicked off at least `GAME_HOURS` ago.

    Sleeper's matchups feed reports a player whose game hasn't started as 0
    and one mid-game as his running total, so a fetched score is only an
    actual once his game is over. `games` is `live.schedule.this_week_games`'
    `{team: LiveGame}` (naive US-Eastern kickoffs); `eastern_to_utc` is
    injected so this stays pure."""
    out: set[str] = set()
    for team, game in games.items():
        kickoff = getattr(game, "kickoff", None)
        if kickoff is not None and eastern_to_utc(kickoff) + timedelta(hours=GAME_HOURS) <= now_utc:
            out.add(team.upper())
    return out


def grade_rows(
    rows: dict[str, ProjectedRow],
    finals: dict[str, float],
    fetched_by_name: dict[str, float],
    week: int,
    finished: Optional[set[str]] = None,
) -> tuple[list[PlayerGrade], list[ProjectedRow]]:
    """`finished` gates fetched scores to teams whose game is over; `None`
    means no schedule was available, so no fetched score is trusted. A FINAL
    score recorded in a log never needs the gate."""
    graded: list[PlayerGrade] = []
    ungraded: list[ProjectedRow] = []
    for key in sorted(rows):
        row = rows[key]
        actual = finals.get(key)
        if actual is None and finished is not None and row.team.upper() in finished:
            actual = fetched_by_name.get(normalize_name(row.name))
        if actual is None:
            ungraded.append(row)
        else:
            graded.append(PlayerGrade(row=row, actual=actual, week=week))
    return graded, ungraded


def summarize(players: list[PlayerGrade]) -> tuple[list[FamilyGrade], list[PositionGrade]]:
    families: list[FamilyGrade] = []
    for fam in sorted({family_of(l) for p in players for l, _ in p.row.adjustments}):
        touched = [p for p in players if family_delta(p.row, fam) != 0.0]
        if not touched:
            continue
        gains = [p.family_gain(fam) for p in touched]
        families.append(FamilyGrade(
            family=fam,
            rows=len(touched),
            total_delta=sum(family_delta(p.row, fam) for p in touched),
            helped=sum(1 for g in gains if g > 1e-9),
            hurt=sum(1 for g in gains if g < -1e-9),
            net_points=sum(gains),
            names=tuple(p.row.name for p in touched),
        ))
    positions: list[PositionGrade] = []
    for pos in sorted({p.row.position for p in players}):
        ps = [p for p in players if p.row.position == pos]
        positions.append(PositionGrade(
            position=pos,
            rows=len(ps),
            baseline_mae=sum(p.baseline_err for p in ps) / len(ps),
            ours_mae=sum(p.ours_err for p in ps) / len(ps),
        ))
    return families, positions


def grade(
    season: int,
    logs_by_week: dict[int, list[dict]],
    fetched_by_week: Optional[dict[int, dict[str, float]]] = None,
    alerts: Iterable[str] = (),
    finished_by_week: Optional[dict[int, set[str]]] = None,
) -> WeekGrade:
    """Grade one week or several (a season aggregate) in one pass. A week
    missing from `finished_by_week` trusts no fetched score."""
    fetched_by_week = fetched_by_week or {}
    finished_by_week = finished_by_week or {}
    players: list[PlayerGrade] = []
    ungraded: list[ProjectedRow] = []
    for wk in sorted(logs_by_week):
        rows, finals = collect(logs_by_week[wk])
        g, u = grade_rows(rows, finals, fetched_by_week.get(wk, {}), wk, finished_by_week.get(wk))
        players.extend(g)
        ungraded.extend(u)
    families, positions = summarize(players)
    return WeekGrade(
        season=season, weeks=tuple(sorted(logs_by_week)), players=players, ungraded=ungraded,
        families=families, positions=positions, alerts=list(alerts),
    )


def fetch_actuals(client, league_id: str, week: int) -> tuple[dict[str, float], list[str]]:
    """League-wide rostered players' points for `week`, keyed by normalized
    name. A fetch failure is an alert and an empty dict, never a raise."""
    from .report import live_points_by_name  # lazy: keeps this module light
    from .sleeper.cache import SleeperFetchError

    try:
        return live_points_by_name(client, league_id, week, client.players()), []
    except SleeperFetchError as exc:
        return {}, [f"Sleeper scores unavailable ({exc}) — grading only players whose FINAL points are in the logs."]


# The dial behind each adjustment family (`week.adjusted_players_with_breakdown`'s
# step labels) -- what a proposal names. A family with no single dial behind it
# ("research trend/volatility" blends several) is reported, never proposed.
FAMILY_DIALS = {
    "weather": "weather_weight",
    "Vegas": "vegas_weight",
    "game script": "game_script_weight",
    "international venue": "venue_disruption_weight",
    "opponent": "opponent_correlation_weight",
}


@dataclass(frozen=True)
class FamilyEvidence:
    """A family's record across graded weeks, one observation per GAME.

    Every player in a game shares its weather, its Vegas line and its script,
    so their errors are one draw, not several: week 1 of 2026's four bad wind
    rows came from a single researched number. Counting them as four would
    have let one bad input look like a pattern."""

    family: str
    dial: Optional[str]
    rows: int
    games: int
    weeks: int
    mean_per_game: float  # points of absolute error removed per game; negative = added
    ci_low: Optional[float]  # None with fewer than two games
    ci_high: Optional[float]


@dataclass(frozen=True)
class Proposal:
    family: str
    dial: str
    direction: str  # "weaker" | "stronger"
    evidence: FamilyEvidence
    text: str


def game_key(week: int, team: str, opponents_by_week: Optional[dict[int, dict[str, str]]] = None) -> tuple:
    """`(week, sorted teams)` -- both sides of a game share one key. Without a
    schedule each team is its own key, which counts one game's two sides
    twice (less conservative, never silent: `family_evidence` says so)."""
    team = (team or "").upper()
    opp = ((opponents_by_week or {}).get(week) or {}).get(team, "")
    return (week, tuple(sorted({team, opp} - {""})))


def family_evidence(
    players: list[PlayerGrade],
    opponents_by_week: Optional[dict[int, dict[str, str]]] = None,
    z: float = 1.96,
) -> list[FamilyEvidence]:
    """Per family: the mean per-game `family_gain` and a normal-approximation
    interval of `z` standard errors over games."""
    out: list[FamilyEvidence] = []
    for fam in sorted({family_of(l) for p in players for l, _ in p.row.adjustments}):
        by_game: dict[tuple, float] = {}
        rows = 0
        for p in players:
            if family_delta(p.row, fam) == 0.0:
                continue
            rows += 1
            key = game_key(p.week, p.row.team, opponents_by_week)
            by_game[key] = by_game.get(key, 0.0) + p.family_gain(fam)
        if not by_game:
            continue
        vals = list(by_game.values())
        n = len(vals)
        mean = sum(vals) / n
        lo = hi = None
        if n > 1:
            half = z * math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1) / n)
            lo, hi = mean - half, mean + half
        out.append(FamilyEvidence(
            family=fam, dial=FAMILY_DIALS.get(fam), rows=rows, games=n,
            weeks=len({k[0] for k in by_game}), mean_per_game=mean, ci_low=lo, ci_high=hi,
        ))
    return out


def proposals(evidence: list[FamilyEvidence], min_weeks: int, min_games: int) -> list[Proposal]:
    """A dial worth a backtest: a family with a dial, at least `min_weeks`
    weeks and `min_games` games of evidence, whose interval excludes zero.

    TEXT, never an action. Nothing reads a proposal back into configuration:
    a human decides, and the repo's rule is that a backtest agrees before a
    shipped dial moves (docs/dev/INSEASON-FINDINGS.md)."""
    out: list[Proposal] = []
    for e in evidence:
        if e.dial is None or e.ci_low is None or e.ci_high is None:
            continue
        if e.weeks < min_weeks or e.games < min_games:
            continue
        if e.ci_high < 0:
            direction = "weaker"
        elif e.ci_low > 0:
            direction = "stronger"
        else:
            continue
        verb = "added" if direction == "weaker" else "removed"
        text = (
            f"{e.dial} may be too {'strong' if direction == 'weaker' else 'weak'}: {e.family} adjustments "
            f"{verb} {abs(e.mean_per_game):.1f} pts of error per game over {e.weeks} weeks / {e.games} games "
            f"(interval {e.ci_low:+.1f} to {e.ci_high:+.1f}). A hypothesis for a backtest — nothing was changed."
        )
        out.append(Proposal(family=e.family, dial=e.dial, direction=direction, evidence=e, text=text))
    return out


def evidence_line(e: FamilyEvidence) -> str:
    ci = f"interval {e.ci_low:+.1f} to {e.ci_high:+.1f}" if e.ci_low is not None else "one game, no interval"
    return f"{e.family}: {e.mean_per_game:+.1f} pts/game over {e.weeks} wk / {e.games} games ({ci})"


def notification_lines(
    week_grade: WeekGrade,
    evidence: list[FamilyEvidence],
    props: list[Proposal],
    min_weeks: int,
    min_games: int,
) -> list[str]:
    """The Tuesday push: last week's grade, the season's evidence, and any
    proposal -- or why there isn't one yet."""
    wk = week_grade.weeks[-1] if week_grade.weeks else "?"
    out = [f"Week {wk}: {len(week_grade.players)} players graded, {len(week_grade.ungraded)} ungraded."]
    if week_grade.families:
        out.extend(
            f"{f.family}: {f.net_points:+.1f} pts over {f.rows} rows (helped {f.helped}, hurt {f.hurt})"
            for f in week_grade.families
        )
    else:
        out.append("No graded player carried an adjustment.")
    if evidence:
        out.append("Season, per game:")
        out.extend(f"  {evidence_line(e)}" for e in evidence)
    if props:
        out.extend(f"PROPOSAL: {p.text}" for p in props)
    else:
        out.append(
            f"No change proposed — that needs {min_weeks}+ weeks and {min_games}+ games "
            "with an interval clear of zero."
        )
    out.extend(f"! {a}" for a in week_grade.alerts[:3])
    return out


def to_json(
    g: WeekGrade,
    evidence: Optional[list[FamilyEvidence]] = None,
    props: Optional[list[Proposal]] = None,
) -> dict:
    extra = {}
    if evidence is not None:
        extra["evidence"] = [e.__dict__ for e in evidence]
    if props is not None:
        extra["proposals"] = [
            {"family": p.family, "dial": p.dial, "direction": p.direction, "text": p.text} for p in props
        ]
    return extra | {
        "season": g.season,
        "weeks": list(g.weeks),
        "alerts": g.alerts,
        "families": [f.__dict__ | {"names": list(f.names)} for f in g.families],
        "positions": [p.__dict__ for p in g.positions],
        "players": [
            {
                "week": p.week, "name": p.row.name, "position": p.row.position, "team": p.row.team,
                "sleeper_proj": p.row.sleeper_proj, "week_proj": p.row.week_proj, "actual": p.actual,
                "adjustments": [{"label": l, "delta": d} for l, d in p.row.adjustments],
                "snapshot": p.row.snapshot, "projection_source": p.row.source,
            }
            for p in g.players
        ],
        "ungraded": [{"name": r.name, "position": r.position} for r in g.ungraded],
    }


def render(
    g: WeekGrade,
    evidence: Optional[list[FamilyEvidence]] = None,
    props: Optional[list[Proposal]] = None,
) -> list[str]:
    weeks = f"week {g.weeks[0]}" if len(g.weeks) == 1 else f"weeks {', '.join(map(str, g.weeks))}"
    lines = [
        f"PROJECTION GRADE — {g.season} {weeks}",
        "Descriptive only. A week of results is a hypothesis for a backtest, never a dial change.",
        f"{len(g.players)} player-weeks graded, {len(g.ungraded)} ungraded (no final points).",
    ]
    lines += [f"  ! {a}" for a in g.alerts]
    post = sum(1 for p in g.players if p.row.source == "post_kickoff")
    legacy = sum(1 for p in g.players if p.row.source == "legacy")
    if post:
        lines.append(f"  note: {post} projection(s) read from the first snapshot after kickoff (no breakdown before it).")
    if legacy:
        lines.append(f"  note: {legacy} projection(s) from logs older than the adjustment breakdown — nothing to attribute.")
    lines += ["", "ADJUSTMENTS  (did each one move the projection toward the real result?)"]
    if not g.families:
        lines.append("  (no graded row carried an adjustment)")
    for f in g.families:
        verdict = "helped" if f.net_points > 0 else "hurt" if f.net_points < 0 else "neutral"
        lines.append(
            f"  {f.family:10} rows {f.rows:3}  applied {f.total_delta:+6.1f}  "
            f"helped {f.helped:2} / hurt {f.hurt:2}  net {f.net_points:+6.1f} pts ({verdict})"
        )
    lines += ["", "BY POSITION  (mean absolute error, points)"]
    for p in g.positions:
        lines.append(f"  {p.position:4} n={p.rows:3}  Sleeper {p.baseline_mae:5.1f}  ours {p.ours_mae:5.1f}")
    big = sorted(g.players, key=lambda p: abs(p.row.week_proj - p.row.baseline), reverse=True)
    big = [p for p in big if abs(p.row.week_proj - p.row.baseline) >= 1.0]
    if big:
        lines += ["", "LARGEST ADJUSTMENTS"]
        for p in big[:10]:
            lines.append(
                f"  w{p.week:<2} {p.row.name[:22]:22} {p.row.position:3} Sleeper {p.row.baseline:5.1f}  "
                f"ours {p.row.week_proj:5.1f}  actual {p.actual:5.1f}  "
                f"{'closer' if p.ours_err < p.baseline_err else 'further'}"
            )
    if evidence is not None:
        lines += ["", "EVIDENCE  (one observation per game, across every week graded)"]
        lines += [f"  {evidence_line(e)}" for e in evidence] or ["  (none)"]
        lines += [f"  PROPOSAL: {p.text}" for p in props or []] or ["  No change proposed."]
    return lines
