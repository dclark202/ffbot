"""Rank->points calibration curves fit from realized NFL seasons.

Preseason projections are not wrong about WHO is good so much as about HOW
FAR APART the good ones are. Measured over 2021-2024 (league-scored, weeks
1-15), projected points at each within-position rank versus what actually
happened:

    rank    QB proj -> real     RB              WR              TE
       1    352 -> 363 (+11)    331 -> 325      312 -> 339      254 -> 236
       2    318 -> 352 (+34)    325 -> 278      311 -> 288      235 -> 200
       4    303 -> 318 (+16)    272 -> 251      280 -> 267      201 -> 166
       8    291 -> 267 (-23)    247 -> 222      250 -> 226      172 -> 139
      12    285 -> 243 (-41)    243 -> 202      229 -> 204      162 -> 125
      20    259 -> 190 (-69)    203 -> 169      223 -> 186      142 ->  96

Two separate errors live in that table. Everything is optimistic in general
(projections don't regress to the mean), and -- the one that actually
changes decisions -- the QB curve is far too FLAT: genuinely elite
quarterbacks are UNDER-projected while QB6-20 are badly OVER-projected,
crossing over around QB5.

That flatness is exactly why a value-over-replacement draft engine punts the
position. VOR reads nothing but the gaps between players, so a flattened
curve says "the QB tail is safe, you can always wait" while simultaneously
saying "the top is nothing special" -- and the engine defers, every round,
until only replacement-level quarterbacks remain.

The fix has to be shape-aware, not a per-position multiplier. Scaling a
whole position's curve by one constant lifts QB10 exactly as much as QB1,
which encodes a blanket "draft a QB early" rule -- the wrong lesson, and
measurably so: applied end to end it moved the engine's quarterback from
round 10 to round 2. Re-mapping rank->points instead raises the genuinely
elite and lowers the replaceable tail, leaving the position's overall level
alone, so "an elite QB is worth an early pick" and "take a QB in round 2"
stop being the same statement.

Ordering is never touched. The curve is monotone non-increasing by
construction, and players are re-priced strictly in their existing
projected order, so this can only change the SPACING between players at a
position, never who is ranked above whom.

The leakage rule is the same one `ffbot.history.projections.ecr_projections`
already enforces and for the same reason: a curve fit on the season being
graded is look-ahead, and it is the single most likely way to fake a good
backtest here, since the natural fitting window and the natural grading
window are the same four seasons. `rank_points_curve` therefore RAISES on
an overlapping season rather than warning.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence

from ..config import Config, LeagueScoring
from .actuals import week_actuals
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener
from .projections import ECR_CLEAN_SEASONS

# Positions worth calibrating. K/DEF are excluded for the same reason
# `ffbot.edge.EDGE_EXCLUDED_POSITIONS` excludes them: their week-to-week
# scoring barely persists, so a rank->points curve fit on them describes
# last season's luck rather than a repeatable shape.
CALIBRATED_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")

# Scoring window per season. Matches the backtest's own default `--weeks`
# (1-15), i.e. the fantasy regular season, so a curve and the thing it is
# graded against cover the same games.
_DEFAULT_WEEKS: tuple[int, ...] = tuple(range(1, 16))

# How deep to fit. Past this the realized sample is dominated by players who
# barely played, and the curve is only ever consulted for draftable ranks
# anyway -- see `curve_value` for what happens beyond the fitted range.
_MAX_RANK = 60


def _season_points_by_rank(
    season: int,
    scoring: LeagueScoring,
    weeks: Sequence[int],
    cache_dir: Path | str,
    opener: UrlOpener,
) -> dict[str, list[float]]:
    """One season's realized points per position, sorted descending.

    Realized outcomes are read ONLY through `history.actuals.week_actuals`,
    per CLAUDE.md's rule that grading data has exactly one channel -- this
    module must never reach into `history.index`'s snapshot rows itself.
    """
    totals: dict[str, float] = defaultdict(float)
    position_of: dict[str, str] = {}
    for week in weeks:
        try:
            actual = week_actuals(season, week, scoring, cache_dir=cache_dir, opener=opener)
        except (ValueError, OSError):
            # A missing week is a thinner sample, not a fatal error -- the
            # same partial-degrade contract every other historical source
            # in this package follows.
            continue
        for key, points in actual.items():
            totals[key] += points
            position_of[key] = key.rsplit(":", 1)[-1]

    by_pos: dict[str, list[float]] = defaultdict(list)
    for key, points in totals.items():
        pos = position_of.get(key, "")
        if pos in CALIBRATED_POSITIONS:
            by_pos[pos].append(points)
    for values in by_pos.values():
        values.sort(reverse=True)
    return by_pos


def _monotone(values: Sequence[float]) -> list[float]:
    """Force a non-increasing sequence, keeping the earlier (better) value.

    Averaging a handful of seasons can leave a rank slightly above the one
    before it (a single freak season at rank 7, say). Left alone that would
    make the curve say the 8th-best quarterback outscores the 7th, which
    `apply_rank_calibration` would then hand to players in projected order
    -- inverting two players purely from fitting noise. Clamping keeps the
    "never reorders anyone" guarantee true by construction rather than by
    hoping the sample is smooth.
    """
    out: list[float] = []
    ceiling = float("inf")
    for value in values:
        ceiling = min(ceiling, value)
        out.append(ceiling)
    return out


def rank_points_curve(
    fit_seasons: Optional[Sequence[int]] = None,
    exclude_season: int | None = None,
    scoring: LeagueScoring | None = None,
    cfg: Config | None = None,
    weeks: Sequence[int] = _DEFAULT_WEEKS,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    max_rank: int = _MAX_RANK,
) -> dict[str, list[float]]:
    """`{position: [points at rank 1, rank 2, ...]}` averaged over
    `fit_seasons`, monotone non-increasing.

    `exclude_season` is the season being graded. It is REMOVED from the
    default fit set, and passing it explicitly inside `fit_seasons` raises
    -- calibrating on the season under test is look-ahead leakage, refused
    here exactly as `ecr_projections` refuses it, because a curve fit on the
    graded season would make any backtest of this feature meaningless while
    looking clean.

    A season contributes only the ranks it actually has data for, so a
    partially-cached season shortens the curve rather than dragging it down
    with zeros.
    """
    if fit_seasons is None:
        fit_seasons = tuple(s for s in ECR_CLEAN_SEASONS if s != exclude_season)
    else:
        fit_seasons = tuple(fit_seasons)
    if exclude_season is not None and exclude_season in fit_seasons:
        raise ValueError(
            f"fit_seasons must not include the season under test ({exclude_season}) — "
            "calibrating rank->points on the season being graded is look-ahead leakage"
        )
    if not fit_seasons:
        raise ValueError(
            "no seasons left to fit a rank->points curve on "
            f"(excluded {exclude_season}, known seasons {ECR_CLEAN_SEASONS})"
        )

    if scoring is None:
        scoring = (cfg.league if cfg is not None else None) or LeagueScoring.fantasypros_default()

    # rank index -> that rank's realized points in each fitted season
    samples: dict[str, dict[int, list[float]]] = {
        pos: defaultdict(list) for pos in CALIBRATED_POSITIONS
    }
    for season in fit_seasons:
        by_pos = _season_points_by_rank(season, scoring, weeks, cache_dir, opener)
        for pos, values in by_pos.items():
            for idx, points in enumerate(values[:max_rank]):
                samples[pos][idx].append(points)

    curve: dict[str, list[float]] = {}
    for pos, by_rank in samples.items():
        if not by_rank:
            continue
        depth = max(by_rank) + 1
        means = [
            statistics.fmean(by_rank[idx]) if by_rank.get(idx) else None for idx in range(depth)
        ]
        # Trim a ragged tail rather than interpolating across a hole: a rank
        # only some seasons reached is a thinner estimate than the ranks
        # above it, and `curve_value` already decays past the fitted end.
        trimmed: list[float] = []
        for value in means:
            if value is None:
                break
            trimmed.append(value)
        if trimmed:
            curve[pos] = _monotone(trimmed)
    return curve


def weekly_rank_points_curve(
    fit_seasons: Optional[Sequence[int]] = None,
    exclude_season: int | None = None,
    scoring: LeagueScoring | None = None,
    cfg: Config | None = None,
    weeks: Sequence[int] = _DEFAULT_WEEKS,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    max_rank: int = _MAX_RANK,
) -> dict[str, list[float]]:
    """The single-week sibling of `rank_points_curve`: realized points by
    within-position rank in ONE week, averaged over every fitted week.

    A separate fit rather than the season curve divided by 17, because the
    two have genuinely different shapes. A season total averages away the
    weeks a player missed or blew up; a single week keeps them, so the
    weekly distribution is far more top-heavy. Dividing the season curve
    would understate exactly the boom weeks that start/sit decisions are
    made on.

    Same leakage rule and the same monotone guarantee as the season curve —
    it feeds the identical `board.apply_rank_calibration`, so it must not be
    able to reorder players either.
    """
    if fit_seasons is None:
        fit_seasons = tuple(s for s in ECR_CLEAN_SEASONS if s != exclude_season)
    else:
        fit_seasons = tuple(fit_seasons)
    if exclude_season is not None and exclude_season in fit_seasons:
        raise ValueError(
            f"fit_seasons must not include the season under test ({exclude_season}) — "
            "calibrating rank->points on the season being graded is look-ahead leakage"
        )
    if not fit_seasons:
        raise ValueError("no seasons left to fit a weekly rank->points curve on")

    if scoring is None:
        scoring = (cfg.league if cfg is not None else None) or LeagueScoring.fantasypros_default()

    samples: dict[str, dict[int, list[float]]] = {
        pos: defaultdict(list) for pos in CALIBRATED_POSITIONS
    }
    for season in fit_seasons:
        for week in weeks:
            try:
                actual = week_actuals(season, week, scoring, cache_dir=cache_dir, opener=opener)
            except (ValueError, OSError):
                continue
            by_pos: dict[str, list[float]] = defaultdict(list)
            for key, points in actual.items():
                pos = key.rsplit(":", 1)[-1]
                if pos in CALIBRATED_POSITIONS:
                    by_pos[pos].append(points)
            for pos, values in by_pos.items():
                values.sort(reverse=True)
                for idx, points in enumerate(values[:max_rank]):
                    samples[pos][idx].append(points)

    curve: dict[str, list[float]] = {}
    for pos, by_rank in samples.items():
        if not by_rank:
            continue
        depth = max(by_rank) + 1
        means = [
            statistics.fmean(by_rank[idx]) if by_rank.get(idx) else None for idx in range(depth)
        ]
        trimmed: list[float] = []
        for value in means:
            if value is None:
                break
            trimmed.append(value)
        if trimmed:
            curve[pos] = _monotone(trimmed)
    return curve


def predictiveness(
    fit_seasons: Optional[Sequence[int]] = None,
    exclude_season: int | None = None,
    cfg: Config | None = None,
    scoring: LeagueScoring | None = None,
    weeks: Sequence[int] = _DEFAULT_WEEKS,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    num_teams: int = 12,
    depth_by_position: dict[str, int] | None = None,
) -> dict[str, float]:
    """Position -> 0..1, how much of a preseason projection's SPREAD is real.

    This is the number that decides whether a position deserves to be
    drafted early at all, and it is a different question from
    `rank_points_curve`'s. That curve is DISTRIBUTIONAL — it answers "what
    did the k-th best finisher score", computed by sorting outcomes after
    the fact, so it always shows a healthy spread even for a position whose
    projections are pure noise. This function answers the PREDICTIVE
    question instead: pair each player's preseason projection with what
    they actually scored, and measure how strongly the two move together
    (Pearson r, clamped to [0, 1]).

    Why it matters here: `ffbot.draft.recommend` currently suppresses K and
    DEF outright until the last two rounds, and the comment on that gate
    concedes why — "VOR genuinely rates the best kicker above a marginal
    receiver... Without this the optimizer spends a 5th-round pick on a
    kicker." That is a hard rule papering over a valuation nobody trusted.
    The valuation is wrong for a measurable reason: across 2021-2024 the
    correlation between projected and realized season points is ~0.50 for
    TE and ~0.38-0.42 for QB/RB/WR, but only ~0.20 for K and ~0.23 for DEF.
    Put another way, the player projected as the best kicker beat the one
    projected 12th by about 17 realized points across a whole season — one
    point a week — while the board projects several times that.

    Feeding these factors to `board.apply_predictiveness_shrinkage` scales
    each position's spread by how much of it survives contact with reality,
    which lets K/DEF sink on their own merits instead of being gated.

    Same leakage rule as everything else in this module: a factor fit on
    the season being graded is look-ahead, and raises.
    """
    if fit_seasons is None:
        fit_seasons = tuple(s for s in ECR_CLEAN_SEASONS if s != exclude_season)
    else:
        fit_seasons = tuple(fit_seasons)
    if exclude_season is not None and exclude_season in fit_seasons:
        raise ValueError(
            f"fit_seasons must not include the season under test ({exclude_season}) — "
            "calibrating rank->points on the season being graded is look-ahead leakage"
        )
    if not fit_seasons:
        raise ValueError("no seasons left to fit predictiveness on")

    if cfg is None:
        raise ValueError("predictiveness needs a Config to build the historical board")
    if scoring is None:
        scoring = cfg.league or LeagueScoring.fantasypros_default()

    # How deep to sample per position: roughly the draftable pool, so the
    # estimate reflects players actually under consideration rather than
    # hundreds of undrafted names whose projection and outcome are both ~0
    # (which would inflate the correlation for every position alike).
    depth = depth_by_position or {"QB": 24, "RB": 48, "WR": 60, "TE": 24, "K": 24, "DEF": 24}

    from .board import historical_board  # local: avoids a package-level cycle

    pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for season in fit_seasons:
        try:
            board = historical_board(season, cfg, num_teams=num_teams, cache_dir=cache_dir)
        except (ValueError, OSError):
            continue
        totals: dict[str, float] = defaultdict(float)
        for week in weeks:
            try:
                actual = week_actuals(season, week, scoring, cache_dir=cache_dir, opener=opener)
            except (ValueError, OSError):
                continue
            for key, points in actual.items():
                totals[key] += points
        for position in CALIBRATED_POSITIONS + ("K", "DEF"):
            pool = sorted(
                (p for p in board.players if p.position == position), key=lambda p: -p.points
            )
            for player in pool[: depth.get(position, 24)]:
                if player.key in totals:
                    pairs[position].append((player.points, totals[player.key]))

    out: dict[str, float] = {}
    for position, data in pairs.items():
        if len(data) < 20:  # too thin to estimate; leave the position uncorrected
            continue
        xs = [x for x, _ in data]
        ys = [y for _, y in data]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((x - mx) * (y - my) for x, y in data)
        if sxx <= 0 or syy <= 0:
            continue
        r = sxy / ((sxx * syy) ** 0.5)
        out[position] = max(0.0, min(1.0, r))
    return out


def curve_value(curve_for_pos: Sequence[float], rank: int) -> float:
    """Points the curve assigns to 1-indexed `rank`.

    Past the fitted end the curve decays along its own final slope rather
    than flattening onto the last fitted value -- a flat tail would price
    the 61st and the 200th receiver identically, re-creating in miniature
    the exact "everyone below the cut is worth the same" collapse this
    repo already fixed once in the bench-depth term. Floored at zero: a
    projection is a points total, and negative would invert ordering.
    """
    if not curve_for_pos:
        raise ValueError("empty curve")
    if rank <= len(curve_for_pos):
        return curve_for_pos[max(0, rank - 1)]
    if len(curve_for_pos) < 2:
        return max(0.0, curve_for_pos[-1])
    slope = curve_for_pos[-1] - curve_for_pos[-2]  # <= 0 by monotonicity
    return max(0.0, curve_for_pos[-1] + slope * (rank - len(curve_for_pos)))
