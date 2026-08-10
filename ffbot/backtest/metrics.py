"""Statistics protocol implementation — see docs/BACKTEST.md's "Statistics
protocol" section. This module implements exactly what's pre-registered
there and nothing extra: scoring a `Lineup` against ground truth, lineup
efficiency, discordant-pair extraction, a block bootstrap over `(season,
week)` rather than over individual decisions — outcomes within one week
correlate (same games, same weather), so bootstrapping decisions
independently would understate the real uncertainty — and, for B4's season
simulator, per-manager win-rate deltas (`paired_win_rate_deltas`). Win-rate
records themselves (`ffbot.backtest.schedule.ManagerRecord`) are NOT
imported here — this module stays self-contained by taking plain
`{manager: win_rate}` dicts rather than depending on `schedule.py`'s types.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Sequence

from .baselines import Lineup


def score_lineup(lineup: Lineup, actuals_by_key: dict[str, float], key_by_id: dict[int, str]) -> float:
    """Realized league-scored total for one lineup's starters — the only
    function anything in this module (or `replay.py`) uses to turn a
    `Lineup` into a number, so every metric here is graded identically."""
    total = 0.0
    for _slot, p in lineup.assignments:
        key = key_by_id.get(p.player_id)
        if key is not None:
            total += actuals_by_key.get(key, 0.0)
    return total


def lineup_efficiency(lineup_points: float, oracle_points: float) -> float:
    """Fraction of the oracle's points this lineup captured, clamped to
    [0, 1]. A non-positive oracle total (every eligible player busted) is
    the one case a plain ratio breaks; a lineup that matches or beats a
    non-positive oracle is scored 1.0, anything worse 0.0."""
    if oracle_points <= 0:
        return 1.0 if lineup_points >= oracle_points else 0.0
    return max(0.0, min(1.0, lineup_points / oracle_points))


def points_left_on_bench(lineup_points: float, oracle_points: float) -> float:
    """Absolute points short of hindsight-optimal — the same oracle gap
    `lineup_efficiency` reports as a ratio, here as a raw point total."""
    return max(0.0, oracle_points - lineup_points)


def lineups_differ(a: Lineup, b: Lineup) -> bool:
    """Whether two lineups started a different SET of players. The
    discordant-pair test the statistics protocol calls for: only decisions a
    weight actually flipped should count toward a paired McNemar-style
    comparison, not every week regardless of whether anything changed."""
    ids_a = frozenset(p.player_id for _slot, p in a.assignments)
    ids_b = frozenset(p.player_id for _slot, p in b.assignments)
    return ids_a != ids_b


@dataclass(frozen=True)
class Decision:
    """One (season, week, sampled-roster) unit's graded outcome — the
    record every metric in this module operates on."""

    season: int
    week: int
    roster_index: int
    points: dict[str, float]  # baseline name -> realized points

    @property
    def block_key(self) -> tuple[int, int]:
        return (self.season, self.week)

    def delta(self, a: str, b: str) -> float:
        return self.points[a] - self.points[b]


def paired_deltas(decisions: Sequence[Decision], a: str, b: str) -> list[float]:
    """`[d.points[a] - d.points[b] for d in decisions]` — the raw paired
    series. Shared variance between `a` and `b` (same roster, same week)
    cancels in each element, which is the entire point of pairing rather
    than comparing the two baselines' totals independently."""
    return [d.delta(a, b) for d in decisions]


def discordant_deltas(
    decisions: Sequence[Decision], lineups: Sequence[dict[str, Lineup]], a: str, b: str
) -> list[float]:
    """`paired_deltas` restricted to decisions where `a` and `b` actually
    started a different set of players (see `lineups_differ`) — a decision
    the compared weight had zero opportunity to affect contributes no
    information about whether that weight is any good, and diluting the
    sample with a large number of unaffected weeks is exactly what makes a
    naive season-total comparison read as confident when it's actually
    mostly noise (see docs/BACKTEST.md's statistics protocol).
    """
    out: list[float] = []
    for decision, lineup_set in zip(decisions, lineups):
        if lineups_differ(lineup_set[a], lineup_set[b]):
            out.append(decision.delta(a, b))
    return out


def block_bootstrap_mean_ci(
    values: Sequence[float],
    block_keys: Sequence[tuple[int, int]],
    iterations: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """`(mean, lo, hi)` — a `1 - alpha` percentile-bootstrap confidence
    interval on `mean(values)`, resampling whole `(season, week)` blocks
    with replacement rather than individual observations.

    Fewer than 2 distinct blocks can't support a bootstrap at all (there is
    nothing to resample) — that case returns the point estimate as its own
    interval, which reads as an honestly-zero-width CI rather than raising,
    so a caller sweeping a single week doesn't have to special-case it.
    """
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)

    by_block: dict[tuple[int, int], list[float]] = {}
    for v, key in zip(values, block_keys):
        by_block.setdefault(key, []).append(v)
    blocks = list(by_block.values())

    point_estimate = statistics.fmean(values)
    if len(blocks) < 2:
        return point_estimate, point_estimate, point_estimate

    means: list[float] = []
    for _ in range(iterations):
        sample: list[float] = []
        for _ in range(len(blocks)):
            sample.extend(blocks[rng.randrange(len(blocks))])
        if sample:
            means.append(statistics.fmean(sample))
    means.sort()
    lo_idx = int((alpha / 2) * len(means))
    hi_idx = min(len(means) - 1, int((1 - alpha / 2) * len(means)))
    return point_estimate, means[lo_idx], means[hi_idx]


def paired_win_rate_deltas(
    win_rates_a: dict[int, float], win_rates_b: dict[int, float]
) -> dict[int, float]:
    """Per-manager `(win_rate_a - win_rate_b)`, for managers present in
    both — e.g. the same simulated manager's win rate under the agent's
    lineup policy vs. under the frozen-projection control, one full season
    each, same schedule. Managers absent from either side are dropped
    rather than defaulted to 0.0 — a manager who didn't play a full season
    under both conditions isn't a valid paired observation.

    Feed the returned values into `block_bootstrap_mean_ci` with the season
    (not the manager) as the block key: within one simulated season, one
    manager's win rate is mechanically correlated with every other
    manager's (beating one manager means someone else lost that same week),
    so managers are not independent observations the way separate
    `(season, week)` lineup decisions are.
    """
    common = set(win_rates_a) & set(win_rates_b)
    return {t: win_rates_a[t] - win_rates_b[t] for t in common}
