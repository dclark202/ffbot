"""Per-player weekly signal providers for historical replay (see
docs/dev/BACKTEST.md, milestone B4).

Two of `SeasonConfig`'s five spice dials — `volatility_weight` and
`upside_lean_weight` — are structurally inert in every backtest run before
this module: they read `week.WeeklyPlayerIntel.volatility`/`.upside`, which
`ffbot.history.index.as_of()` never populates (a live run gets those from
researched `weekly/week-NN.yml` notes, which have no historical equivalent).
A `SignalProvider` is what fills that gap — a function from `(season, week)`
to `{normalized_name: {"volatility": 0..100, "upside": 0..100}}`, merged onto
a `WeekSnapshot` via `WeekSnapshot.with_signals()` (see `ffbot/history/index.py`)
rather than into `as_of()` itself.

That split is deliberate, not incidental. `as_of()`'s whole value is a
*structural* leakage guarantee — see `tests/test_history_index.py::
TestAsOfLeakageGuarantee` — that it never fetches a results-bearing source at
all. A form-based signal provider genuinely needs `stats_player_week` (a
results-bearing source, by the letter of that guarantee, even though every
week it reads is safely in the past relative to the target week). Keeping
providers outside `as_of()` means that guarantee never has to make an
exception for "but this fetch is safe" — it stays exactly what it says.
Each provider is responsible for its own leakage boundary instead (see
`TestHistoricalFormLeakage` in `tests/test_history_signals.py`).

`historical_form` (B4) measures whether the volatility/upside *mechanism*
pays off — not whether researched intel is any good. A stats-derived proxy
cannot capture what a beat writer knows about a game plan; it can only tell
you whether "prefer the higher-variance player on a close call" is worth
doing at all, using the crudest available signal for variance.

`usage_form` (B6) is a genuine momentum signal — recent target/air-yards
share (WOPR) relative to season-to-date — and graded as the first dial in
this project's history whose train-selected weight held up (barely) on a
held-out season. `scoring_form` and `usage_divergence` (B5) exist to test
the momentum question head-on: does a raw recent-POINTS trend carry the
same signal a recent-ROLE trend does, or is a points streak mostly
touchdown variance that a role-based measure filters out? See each
provider's own docstring and docs/dev/BACKTEST.md's B5 writeup for the answer.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from ..config import Config, LeagueScoring
from ..form import (
    USAGE_POSITIONS,
    divergence_scores,
    percentile_rank_within_position,
    scoring_trend_scores,
    usage_trend_scores,
    variance_scores,
)
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_rows
from .names import actuals_key
from .projections import _game_log


class SignalProvider(Protocol):
    """`(season, week, cfg, cache_dir, opener) -> {normalized_name: {signal: 0..100}}`.

    Every provider must respect the same leakage boundary `naive_projections`
    does: nothing about week `>= week` of `season` may influence the output
    for that `(season, week)` call.
    """

    def __call__(
        self,
        season: int,
        week: int,
        cfg: Config,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        opener: UrlOpener = _default_opener,
    ) -> dict[str, dict[str, float]]: ...


# Re-exported so existing readers (and tests) still find it here; the
# definition lives in ffbot/form.py beside the math that uses it.
_percentile_rank_within_position = percentile_rank_within_position


def historical_form(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    min_games: int = 3,
) -> dict[str, dict[str, float]]:
    """A stats-only proxy for boom/bust rating and spike-week potential,
    from a player's own league-scored games strictly before `(season, week)`.

    `volatility` — the coefficient of variation (stdev / mean) of the
    player's per-game points, percentile-ranked within position. Cheap and
    crude (it can't distinguish "genuinely unpredictable" from "role
    changed mid-season"), but directly measures the thing
    `SeasonConfig.volatility_weight`'s docstring names: cross-observation
    disagreement about what this player is worth in a given week.

    `upside` — how far the player's own ceiling (max game) sits above their
    median game, as a fraction of the median, again percentile-ranked
    within position. A player whose best game towers over their typical one
    has more spike-week potential than one who is metronomically consistent
    at the same average.

    Both need `min_games` (default 3) prior games to compute at all; anyone
    with fewer gets no entry, which `WeekSnapshot.with_signals()` treats the
    same as "nothing was ever researched" — `volatility_score`/`upside_score`
    in `ffbot/week.py` already default an absent entry to 0.0, an exact
    no-op, never a crash.

    This reuses `projections._game_log` — the exact same in-season history
    `naive_projections` computes its recency-weighted average from — rather
    than re-deriving prior-week points a second way, so the leakage boundary
    is the one `TestNaiveProjectionsLeakage` already covers, not a new one.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    log = _game_log(season, scoring, cache_dir, opener, before_week=week)
    return variance_scores(log, min_games=min_games)


# Kept as an alias so existing readers of this module still find it; the
# definition lives in ffbot/form.py beside the math that uses it.
_USAGE_POSITIONS = USAGE_POSITIONS


def _usage_game_log(
    season: int, cache_dir: Path | str, opener: UrlOpener, before_week: int
) -> dict[str, list[tuple[int, float]]]:
    """`{actuals_key: [(week, wopr), ...]}`, weeks `< before_week` only —
    the identical leakage boundary `projections._game_log` enforces for
    points, applied here to WOPR (Weighted Opportunity Rating: 1.5 x target
    share + 0.7 x air-yards share, already computed by nflverse) instead.
    WOPR is a single, well-established usage number rather than something
    this module re-derives from the two shares by hand. QB/K/DEF are
    excluded outright — WOPR is a receiving-usage metric and reads as
    meaningless noise for any of the three.
    """
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    rows = fetch_rows("stats_player_week", season=season, cache_dir=cache_dir, opener=opener)
    for row in rows:
        try:
            w = int(row.get("week", -1))
        except (TypeError, ValueError):
            continue
        if w >= before_week:
            continue
        position = (row.get("position") or "").strip().upper()
        if position not in _USAGE_POSITIONS:
            continue
        name = row.get("player_display_name") or ""
        if not name:
            continue
        raw = row.get("wopr")
        if raw is None or raw == "":
            continue
        try:
            wopr = float(raw)
        except ValueError:
            continue
        out[actuals_key(name, position)].append((w, wopr))
    return out


def usage_form(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    min_games: int = 3,
    recent_games: int = 3,
) -> dict[str, dict[str, float]]:
    """A stats-only proxy for "is this player's role trending up or down
    right now" — recent WOPR (Weighted Opportunity Rating) relative to the
    player's own season-to-date WOPR, percentile-ranked within position,
    strictly before `(season, week)`.

    `usage` — 0..100. A player whose LAST `recent_games` games run hotter
    than their season average ranks high; one whose role is fading ranks
    low. Opportunity is stickier week-to-week than efficiency, so a real
    target-share trend is meant to predict next week better than past
    FANTASY POINTS alone (`historical_form`'s own signal) can — this is a
    genuinely different measurement, not a relabeling of the same one.

    Needs `min_games` (default 3) total prior games to compute at all;
    fewer gets no entry, same "absent = untouched" contract
    `historical_form`/`WeekSnapshot.with_signals` already guarantee.
    `cfg` is accepted for signature parity with every other `SignalProvider`
    (see the Protocol above) but unused — WOPR needs no league scoring
    rules, unlike `historical_form`'s points-based measurement.
    """
    log = _usage_game_log(season, cache_dir, opener, before_week=week)
    return usage_trend_scores(log, min_games=min_games, recent_games=recent_games)


def scoring_form(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    min_games: int = 3,
    recent_games: int = 3,
) -> dict[str, dict[str, float]]:
    """A stats-only proxy for "is this player's SCORING hot or cold right
    now" — recent league-scored points relative to the player's own
    season-to-date average, percentile-ranked within position, strictly
    before `(season, week)`. The finance-style momentum question asked in
    plain terms: do recent points predict next week's, on top of what a
    season-long average already says?

    Deliberately built as the direct counterpart to `usage_form` below,
    same shape and same leakage boundary (`projections._game_log`), so the
    two can be graded against each other head-to-head: the working
    hypothesis is that `usage_form` (a role/opportunity trend) should hold
    up better than this one (a raw points trend), because touchdowns are
    largely variance a team's role distribution doesn't control, while
    target share / air-yards share is comparatively sticky week to week —
    see docs/dev/BACKTEST.md's B5 writeup for the result. Covers EVERY scored
    position, including K/DEF, unlike `usage_form`/`usage_divergence` which
    are receiving-usage metrics and stop at RB/WR/TE.

    `min_games` (default 3) prior games are required to compute at all;
    fewer gets no entry, the same "absent = untouched" contract
    `historical_form`/`usage_form` already guarantee.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    log = _game_log(season, scoring, cache_dir, opener, before_week=week)
    return scoring_trend_scores(log, min_games=min_games, recent_games=recent_games)


def usage_divergence(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    min_games: int = 3,
    recent_games: int = 3,
) -> dict[str, dict[str, float]]:
    """Whether a player's ROLE is trending up faster than their PRODUCTION
    — `usage_form`'s trend percentile minus `scoring_form`'s trend
    percentile, both computed strictly before `(season, week)`. High
    (role rising, production lagging) reads as a positive-regression
    candidate: the opportunity is already there and the points haven't
    caught up yet. Low (production outrunning role) reads as
    touchdown-dependent scoring likely to cool off — usage says nothing
    special is happening, but the points have been good anyway.

    This is deliberately NOT a re-scoring of either input alone; it exists
    to test whether the GAP between opportunity and output carries
    information the two trends don't have on their own. RB/WR/TE only,
    inherited from `_USAGE_POSITIONS` (see `_usage_game_log`) — the same
    scope restriction `usage_form` has, for the same reason (WOPR is a
    receiving-usage metric).
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    points_log = _game_log(season, scoring, cache_dir, opener, before_week=week)
    usage_log = _usage_game_log(season, cache_dir, opener, before_week=week)
    return divergence_scores(
        usage_log, points_log, min_games=min_games, recent_games=recent_games,
    )


def combine_providers(*providers: SignalProvider) -> SignalProvider:
    """Merge several `SignalProvider`s into one callable — `WeekSnapshot.
    with_signals` is a single `(season, week) -> {name: {signal: value}}`
    call, so testing e.g. `historical_form` (volatility/upside) and
    `usage_form` (usage) TOGETHER needs their per-name dicts merged, not
    just concatenated. A name present in more than one provider gets both
    providers' keys merged into one dict (later providers win on an actual
    key collision, which none of the shipped providers cause -- they each
    own a disjoint signal name).
    """

    def _combined(
        season: int,
        week: int,
        cfg: Config,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        opener: UrlOpener = _default_opener,
    ) -> dict[str, dict[str, float]]:
        merged: dict[str, dict[str, float]] = defaultdict(dict)
        for provider in providers:
            for name, values in provider(season, week, cfg, cache_dir=cache_dir, opener=opener).items():
                merged[name].update(values)
        return dict(merged)

    return _combined
