"""Turning a per-stat line into fantasy points under a league's real rules.

`board.py` parses a `StatLine` out of each FantasyPros row (see its
`STAT_LAYOUTS`); this module only knows how to score one, with no CSV
knowledge at all. Keeping the two apart is what makes the arithmetic here
testable against hand-built stat lines instead of real export files.

Three outcomes for any one player, tracked via the returned flags:
exactly expressible (recomputed, no flag), partly expressible (recomputed,
flagged as estimated), or not expressible at all (not attempted here —
see `unmodeled_rules`, which reports on the *scoring config*, not any one
player, since these gaps are a property of the league's rules, not of
who's being scored).
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional

from .config import LeagueScoring, Tier


@dataclass(frozen=True)
class StatLine:
    """Per-stat inputs read from one FantasyPros row.

    Every field is optional — a K row has no `pass_yds`, a DEF row has no
    `rec`, and so on. Only the fields belonging to a row's own detected
    layout are ever populated; everything else stays `None` and contributes
    nothing (not "zero of a stat that was actually attempted" — genuinely
    "we don't know").
    """

    pass_att: Optional[float] = None
    pass_cmp: Optional[float] = None
    pass_yds: Optional[float] = None
    pass_td: Optional[float] = None
    pass_int: Optional[float] = None
    rush_att: Optional[float] = None
    rush_yds: Optional[float] = None
    rush_td: Optional[float] = None
    rec: Optional[float] = None
    rec_yds: Optional[float] = None
    rec_td: Optional[float] = None
    fumbles_lost: Optional[float] = None
    fg_made: Optional[float] = None
    fg_att: Optional[float] = None
    pat_made: Optional[float] = None
    sack: Optional[float] = None
    interception: Optional[float] = None
    fumble_recovery: Optional[float] = None
    forced_fumble: Optional[float] = None
    def_td: Optional[float] = None
    safety: Optional[float] = None
    points_allowed_season: Optional[float] = None
    yards_allowed_season: Optional[float] = None


def _tier_points(value: float, tiers: list[Tier]) -> float | None:
    """`value` -> the points of the first tier (by ascending `max`) it falls under."""
    if not tiers:
        return None
    ordered = sorted(tiers, key=lambda t: t.max)
    for t in ordered:
        if value <= t.max:
            return t.points
    return ordered[-1].points


def _fg_value_per_kick(scoring: LeagueScoring) -> tuple[float, tuple[str, ...]]:
    """Effective points per made FG, honest about whether it's exact.

    Exact when the league scores every make identically (`fg_made`, no
    distance ladder). When the league pays by distance but the export
    carries no distance split, the expected value is estimated from
    `fg_distance_mix` (or, lacking even that, a flat average across bands)
    and flagged `fg_distance_estimated` — right in expectation, wrong for
    any one kicker whose actual mix differs from the league-wide share.
    """
    k = scoring.kicking
    if not k.fg_by_distance:
        return k.fg_made, ()

    if k.fg_distance_mix:
        total = 0.0
        covered = 0.0
        for band in k.fg_by_distance:
            share = k.fg_distance_mix.get(f"{int(band.min)}-{int(band.max)}")
            if share is None:
                continue
            total += share * band.points
            covered += share
        if covered > 0:
            return total / covered, ("fg_distance_estimated",)

    avg = sum(b.points for b in k.fg_by_distance) / len(k.fg_by_distance)
    return avg, ("fg_distance_estimated",)


def _points_allowed_per_game(
    season_total: float, games: int, tiers: list[Tier], stdev: float
) -> float:
    """Expected per-game points-allowed score.

    `season_total / games` plugged straight into the tier step function is
    provably wrong, because a step function's average-of-inputs is not its
    average-of-outputs — a defense that alternates between a 10 and a -4
    week scores nothing like a defense that posts a steady 3 every week,
    even though both average the same points allowed. Modeling per-game
    points allowed as `Normal(mean, stdev)` and integrating the tier ladder
    over that distribution captures the difference; `stdev <= 0` collapses
    back to the naive point estimate for anyone who wants the simpler
    version.
    """
    if games <= 0 or not tiers:
        return 0.0
    mean = season_total / games
    if stdev <= 0:
        return _tier_points(mean, tiers) or 0.0

    dist = NormalDist(mu=mean, sigma=stdev)
    ordered = sorted(tiers, key=lambda t: t.max)
    expected = 0.0
    lower = float("-inf")
    for t in ordered:
        prob = dist.cdf(t.max) - dist.cdf(lower)
        expected += prob * t.points
        lower = t.max
    return expected


def score_statline(
    stats: StatLine, position: str, scoring: LeagueScoring
) -> tuple[float, tuple[str, ...]]:
    """Recompute fantasy points for one player's stat line under `scoring`.

    Returns `(points, flags)`. `flags` names anything in *this player's*
    number that is estimated rather than exact (`fg_distance_estimated`,
    `pa_distribution_estimated`) — league-wide gaps that no player's stat
    line could ever close (2-pt conversions, 40+ yard bonuses, ...) are
    reported once per league file by `unmodeled_rules`, not repeated here
    for every row.
    """
    flags: list[str] = []
    pts = 0.0

    p, r, c, m = scoring.passing, scoring.rushing, scoring.receiving, scoring.misc

    if stats.pass_yds is not None and p.yards_per_point:
        pts += stats.pass_yds / p.yards_per_point
    if stats.pass_td is not None:
        pts += stats.pass_td * p.td
    if stats.pass_int is not None:
        pts += stats.pass_int * p.int

    if stats.rush_yds is not None and r.yards_per_point:
        pts += stats.rush_yds / r.yards_per_point
    if stats.rush_td is not None:
        pts += stats.rush_td * r.td

    if stats.rec_yds is not None and c.yards_per_point:
        pts += stats.rec_yds / c.yards_per_point
    if stats.rec_td is not None:
        pts += stats.rec_td * c.td
    if stats.rec is not None:
        reception_value = c.reception_by_position.get(position, c.reception)
        pts += stats.rec * reception_value

    if stats.fumbles_lost is not None:
        pts += stats.fumbles_lost * m.fumble_lost

    if position == "K":
        k = scoring.kicking
        if stats.fg_made is not None:
            per_fg, fg_flags = _fg_value_per_kick(scoring)
            pts += stats.fg_made * per_fg
            flags.extend(fg_flags)
        if stats.fg_att is not None and stats.fg_made is not None and not k.fg_missed_by_distance:
            missed = max(0.0, stats.fg_att - stats.fg_made)
            pts += missed * k.fg_missed
        if stats.pat_made is not None:
            pts += stats.pat_made * k.pat_made

    if position in ("DEF", "D/ST"):
        d = scoring.defense
        if stats.sack is not None:
            pts += stats.sack * d.sack
        if stats.interception is not None:
            pts += stats.interception * d.interception
        if stats.fumble_recovery is not None:
            pts += stats.fumble_recovery * d.fumble_recovery
        if stats.forced_fumble is not None:
            pts += stats.forced_fumble * d.forced_fumble
        if stats.def_td is not None:
            pts += stats.def_td * d.touchdown
        if stats.safety is not None:
            pts += stats.safety * d.safety
        if stats.points_allowed_season is not None and d.points_allowed:
            per_game = _points_allowed_per_game(
                stats.points_allowed_season,
                scoring.games_per_season,
                d.points_allowed,
                d.points_allowed_stdev,
            )
            pts += per_game * scoring.games_per_season
            if d.points_allowed_stdev > 0:
                flags.append("pa_distribution_estimated")

    return pts, tuple(flags)


def unmodeled_rules(scoring: LeagueScoring) -> list[str]:
    """Scoring rules `scoring` enables that no FantasyPros export column can
    ever express, regardless of which player is being scored.

    Computed once per league file — see `board.Board.scoring_summary` — not
    per player. A rule that scores 0 in this league is not reported even if
    it would be unmodelable, since a 0 has no effect to warn about.
    """
    out: list[str] = []
    b = scoring.bonuses
    if b.pass_completion_40plus:
        out.append(f"40+ yard completions ({b.pass_completion_40plus:+g}) — no export column carries this")
    if b.rush_td_40plus:
        out.append(f"40+ yard rushing TDs ({b.rush_td_40plus:+g}) — no export column carries this")
    if b.rec_td_40plus:
        out.append(f"40+ yard receiving TDs ({b.rec_td_40plus:+g}) — no export column carries this")
    if scoring.passing.two_pt or scoring.rushing.two_pt or scoring.receiving.two_pt:
        out.append("2-point conversions — no export column carries this")
    if scoring.misc.off_fumble_return_td:
        out.append("offensive fumble return TDs — no export column carries this")
    if scoring.kicking.pat_missed:
        out.append("missed PATs — export has makes only, no attempts")
    if scoring.kicking.fg_missed_by_distance:
        out.append("missed FGs by distance — export has no distance split on misses")
    if scoring.defense.block_kick:
        out.append("blocked kicks — no export column carries this")
    if scoring.defense.extra_point_returned:
        out.append("extra points returned — no export column carries this")
    if scoring.defense.three_and_outs:
        out.append("three-and-outs forced — no export column carries this")
    return out
