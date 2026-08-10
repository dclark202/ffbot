"""Per-player weekly signal providers for historical replay (see
docs/BACKTEST.md, milestone B4).

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

Ship one reference provider, `historical_form`. It measures whether the
volatility/upside *mechanism* pays off — not whether researched intel is any
good. A stats-derived proxy cannot capture what a beat writer knows about a
game plan; it can only tell you whether "prefer the higher-variance player
on a close call" is worth doing at all, using the crudest available signal
for variance. Future spice providers (researched-flag proxies, snap-share
volatility, whatever comes out of a later session) plug in at the same seam.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from ..config import Config, LeagueScoring
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener
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


def _percentile_rank_within_position(
    raw: dict[str, float], position_by_key: dict[str, str]
) -> dict[str, float]:
    """`{key: raw_value}` -> `{key: 0..100 percentile rank within that key's
    position}`. Percentile, not the raw value directly, because volatility
    and upside are measured in wildly different units per position (a K's
    week-to-week swing is nothing like a WR's) — `WeeklyPlayerIntel`'s 0-100
    contract is a rank within a comparable pool, the same way `board.py`'s
    `upside`/`availability_risk` fields are documented to be.

    A position with exactly one player ranks at the neutral midpoint (50.0)
    rather than an arbitrary 0 or 100 — there is nothing to rank them
    against, so neither extreme is justified.
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
        for i, key in enumerate(ordered):
            out[key] = 100.0 * i / (n - 1)
    return out


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

    raw_vol: dict[str, float] = {}
    raw_ups: dict[str, float] = {}
    position_by_key: dict[str, str] = {}

    for key, games in log.items():
        if len(games) < min_games:
            continue
        points = [pts for _w, pts in games]
        mean = statistics.fmean(points)
        stdev = statistics.pstdev(points)
        median = statistics.median(points)
        ceiling = max(points)

        position_by_key[key] = key.rsplit(":", 1)[1]
        raw_vol[key] = (stdev / mean) if mean > 0 else 0.0
        raw_ups[key] = ((ceiling - median) / median) if median > 0 else 0.0

    vol_pct = _percentile_rank_within_position(raw_vol, position_by_key)
    ups_pct = _percentile_rank_within_position(raw_ups, position_by_key)

    out: dict[str, dict[str, float]] = {}
    for key in position_by_key:
        name = key.rsplit(":", 1)[0]  # normalize_name(...) -- the actuals_key convention
        out[name] = {"volatility": vol_pct.get(key, 50.0), "upside": ups_pct.get(key, 50.0)}
    return out
