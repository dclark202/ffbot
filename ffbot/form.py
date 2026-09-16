"""The four form signals, as pure math over a game log.

These are the computations behind `usage_weight`, `momentum_weight`,
`divergence_weight`, `volatility_weight` and `upside_lean_weight` -- the
five dials `docs/dev/SPICE.md` classes **Validated** as part of the level-3
bundle that cleared train and test.

They live here, separate from any fetch, because there are now TWO feeds for
the same math and there must never be two implementations of it:

* `ffbot/history/signals.py` feeds them nflverse `stats_player_week`, for
  historical replay. That is the feed the validation was measured on.
* `ffbot/live/form.py` feeds them Sleeper's own realized weekly stats, for
  the live in-season path -- the same endpoint, cache and failure mode as
  every other live seam here, and scored by the league's own
  `scoring_settings` rather than by an approximation.

A second copy of "recent mean over season mean, percentile-ranked within
position" would be exactly the drift this repo keeps catching: the shipped
weights were selected against ONE definition of these signals, and a live
path quietly computing a slightly different one would be running an
untested configuration while claiming a validated one.
`tests/test_form.py::TestBothFeedsShareOneImplementation` asserts the two
callers agree given the same log.

WHAT THESE DO NOT DO. Nothing here decides coverage, and the neutral point
is deliberately NOT re-centred. A player with no entry scores 0.0 through
`week.usage_score` and friends, not a neutral 0.5, so a partially-covered
run scales covered players up and leaves everyone else alone. That is a
real cross-positional effect (`USAGE_POSITIONS` is RB/WR/TE, so a kicker
never gets a usage score at all) and it is reproduced here ON PURPOSE:
it is the behaviour the backtest measured, and "fixing" it here would
deviate from the configuration that was actually validated. It is surfaced
instead -- `report._intel_coverage_alerts` reports partial coverage every
run, in those words. Changing the neutral point is a tuning change and
needs its own backtest cell (W5/W8 in docs/dev/INSEASON-FINDINGS.md).

`min_games` is likewise load-bearing rather than incidental: every signal
needs three completed games before it says anything, so all five dials are
structurally silent through week 3 of any season and begin speaking in
week 4. A run before then is not broken; it has nothing to say yet.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

# Usage is a RECEIVING measurement (WOPR: target share and air-yards share),
# so it is meaningless for a quarterback, a kicker or a defense and they are
# excluded outright rather than scored as zero.
USAGE_POSITIONS = frozenset({"RB", "WR", "TE"})

MIN_GAMES = 3
RECENT_GAMES = 3

# `{key: [(week, value), ...]}` where key is "normalized name:POSITION".
GameLog = dict


def percentile_rank_within_position(
    raw: dict[str, float], position_by_key: dict[str, str]
) -> dict[str, float]:
    """`{key: raw_value}` -> `{key: 0..100 percentile rank within position}`.

    Percentile, not the raw value, because these are measured in wildly
    different units per position (a kicker's week-to-week swing is nothing
    like a receiver's) -- `WeeklyPlayerIntel`'s 0-100 contract is a rank
    within a comparable pool.

    A position with exactly one player ranks at the neutral midpoint (50.0)
    rather than an arbitrary 0 or 100: there is nothing to rank them
    against, so neither extreme is justified.

    TIED VALUES SHARE THE AVERAGE RANK, for exactly that reason one step
    further out. Spreading ties across the full 0-100 range by whatever
    order they happened to arrive in manufactures a ranking out of no
    information, and it does it hardest in the one week it matters most:
    with `min_games == recent_games == 3`, EVERY player's trend in week 4 is
    identically 1.0 (the recent window is the whole season), so the first
    week these dials ever speak would otherwise hand five identical players
    scores of 0, 25, 50, 75 and 100 purely by dict order. Averaging the tie
    group makes that week say 50.0 for everyone -- a uniform, harmless
    no-op -- and leaves every genuinely-differing week unchanged.
    """
    by_pos: dict[str, list[str]] = defaultdict(list)
    for key, pos in position_by_key.items():
        if key in raw:
            by_pos[pos].append(key)

    out: dict[str, float] = {}
    for pos, keys in by_pos.items():
        if len(keys) == 1:
            out[keys[0]] = 50.0
            continue
        ordered = sorted(keys, key=lambda k: raw[k])
        n = len(ordered)
        i = 0
        while i < n:
            j = i
            while j + 1 < n and raw[ordered[j + 1]] == raw[ordered[i]]:
                j += 1
            shared = 100.0 * ((i + j) / 2.0) / (n - 1)
            for key in ordered[i:j + 1]:
                out[key] = shared
            i = j + 1
    return out


def _position_of(key: str) -> str:
    return key.rsplit(":", 1)[1]


def _name_of(key: str) -> str:
    return key.rsplit(":", 1)[0]


def _recent_over_season(games, min_games: int, recent_games: int) -> float | None:
    """The shared trend shape: mean of the last `recent_games` over the mean
    of every game so far. `None` when it cannot be computed -- too few games,
    or a season mean of zero, which would divide by nothing rather than mean
    "no trend"."""
    if len(games) < min_games:
        return None
    ordered = sorted(games)
    season_avg = statistics.fmean(v for _wk, v in ordered)
    if season_avg <= 0:
        return None
    recent_avg = statistics.fmean(v for _wk, v in ordered[-recent_games:])
    return recent_avg / season_avg


def variance_scores(points_log: GameLog, min_games: int = MIN_GAMES) -> dict[str, dict[str, float]]:
    """`volatility` and `upside` from a league-scored points log.

    `volatility` is the coefficient of variation of a player's own per-game
    points; `upside` is how far his ceiling sits above his median game, as a
    fraction of that median. Both percentile-ranked within position.

    A stats proxy cannot capture what a beat writer knows about a game plan;
    this measures whether the volatility/upside MECHANISM is worth having,
    which is what B4 set out to answer.
    """
    raw_vol: dict[str, float] = {}
    raw_ups: dict[str, float] = {}
    position_by_key: dict[str, str] = {}

    for key, games in points_log.items():
        if len(games) < min_games:
            continue
        points = [pts for _w, pts in games]
        mean = statistics.fmean(points)
        median = statistics.median(points)
        position_by_key[key] = _position_of(key)
        raw_vol[key] = (statistics.pstdev(points) / mean) if mean > 0 else 0.0
        raw_ups[key] = ((max(points) - median) / median) if median > 0 else 0.0

    vol_pct = percentile_rank_within_position(raw_vol, position_by_key)
    ups_pct = percentile_rank_within_position(raw_ups, position_by_key)
    return {
        _name_of(key): {
            "volatility": vol_pct.get(key, 50.0),
            "upside": ups_pct.get(key, 50.0),
        }
        for key in position_by_key
    }


def usage_trend_scores(
    usage_log: GameLog, min_games: int = MIN_GAMES, recent_games: int = RECENT_GAMES,
) -> dict[str, dict[str, float]]:
    """`usage` -- recent opportunity share against the player's own season.

    Opportunity is stickier week to week than efficiency, so a real usage
    trend should predict next week better than past fantasy points can. Note
    what this measures and does not: it is ACCELERATION WITHIN AN ESTABLISHED
    ROLE, and it is blind to role CREATION -- a rookie whose season average
    IS his breakout week ranks mid-distribution. That is a known limit, not
    a bug (docs/dev/INSEASON-FINDINGS.md, 2026-09-16).
    """
    raw_trend: dict[str, float] = {}
    position_by_key: dict[str, str] = {}
    for key, games in usage_log.items():
        trend = _recent_over_season(games, min_games, recent_games)
        if trend is None:
            continue
        position_by_key[key] = _position_of(key)
        raw_trend[key] = trend

    trend_pct = percentile_rank_within_position(raw_trend, position_by_key)
    return {
        _name_of(key): {"usage": trend_pct.get(key, 50.0)} for key in position_by_key
    }


def scoring_trend_scores(
    points_log: GameLog, min_games: int = MIN_GAMES, recent_games: int = RECENT_GAMES,
) -> dict[str, dict[str, float]]:
    """`momentum` -- recent SCORING against the player's own season average.

    The direct "hot player stays hot" question, deliberately a separate
    field from `usage`: a points streak carries touchdown variance that a
    role-based measure filters out, and B5 exists to test which of the two
    actually carries signal.
    """
    raw_trend: dict[str, float] = {}
    position_by_key: dict[str, str] = {}
    for key, games in points_log.items():
        trend = _recent_over_season(games, min_games, recent_games)
        if trend is None:
            continue
        position_by_key[key] = _position_of(key)
        raw_trend[key] = trend

    trend_pct = percentile_rank_within_position(raw_trend, position_by_key)
    return {
        _name_of(key): {"momentum": trend_pct.get(key, 50.0)} for key in position_by_key
    }


def divergence_scores(
    usage_log: GameLog,
    points_log: GameLog,
    min_games: int = MIN_GAMES,
    recent_games: int = RECENT_GAMES,
) -> dict[str, dict[str, float]]:
    """`divergence` -- role trending up faster than production, or the
    reverse.

    Above 50 means the opportunity is arriving before the points do, which
    is the buy signal; below 50 means the points are outrunning the role,
    which usually means touchdown luck that is about to stop.

    Only a player with BOTH sides computed gets an entry -- a missing side
    is skipped rather than defaulted, because "no usage data" and "usage
    exactly on trend" are different statements.
    """
    raw_usage: dict[str, float] = {}
    position_by_key: dict[str, str] = {}
    for key, games in usage_log.items():
        position = _position_of(key)
        if position not in USAGE_POSITIONS:
            continue
        trend = _recent_over_season(games, min_games, recent_games)
        if trend is None:
            continue
        raw_usage[key] = trend
        position_by_key[key] = position

    raw_points: dict[str, float] = {}
    for key, games in points_log.items():
        if key not in raw_usage:
            continue
        trend = _recent_over_season(games, min_games, recent_games)
        if trend is None:
            continue
        raw_points[key] = trend

    both = set(raw_usage) & set(raw_points)
    position_by_key = {k: p for k, p in position_by_key.items() if k in both}
    usage_pct = percentile_rank_within_position(
        {k: v for k, v in raw_usage.items() if k in both}, position_by_key
    )
    points_pct = percentile_rank_within_position(
        {k: v for k, v in raw_points.items() if k in both}, position_by_key
    )

    out: dict[str, dict[str, float]] = {}
    for key in position_by_key:
        divergence = 50.0 + (usage_pct.get(key, 50.0) - points_pct.get(key, 50.0)) / 2.0
        out[_name_of(key)] = {"divergence": max(0.0, min(100.0, divergence))}
    return out
