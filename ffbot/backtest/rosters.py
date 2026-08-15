"""Sampled synthetic rosters — the design decision that makes B3's
statistics protocol achievable (see docs/dev/BACKTEST.md).

Four clean ECR seasons x 15 regular-season weeks is ~60 manager-weeks if you
replay one real team, nowhere near enough to resolve the effect sizes the
statistics protocol is built around. Sampling N rosters per (season, week)
decouples statistical power from how many historical seasons exist, and —
unlike B4's 12 drafted managers — needs no draft simulator to exist first.
B4 later swaps in real drafted rosters to check the conclusion survives on
realistic ones; this module is deliberately NOT that, and does not try to be.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Sequence

from ..models import roster_capacity

# A representative redraft roster's position mix, as a fraction of total
# roster capacity (starters + bench). NOT derived from `roster_positions`
# the way the live board's replacement level is (see CLAUDE.md's invariant
# on that) — sampling a plausible manager's roster composition for
# statistical power is a different problem than pricing one, and a fixed,
# documented mix is the right tool for it here.
_POSITION_MIX: dict[str, float] = {
    "QB": 0.12,
    "RB": 0.28,
    "WR": 0.32,
    "TE": 0.14,
    "K": 0.07,
    "DEF": 0.07,
}


_MIN_COUNTS: dict[str, int] = {"RB": 2, "WR": 2}


def _target_counts(capacity: int) -> dict[str, int]:
    """`_POSITION_MIX` scaled to `capacity` total roster spots, rounded to
    integers that sum exactly to `capacity` when `capacity` is large enough
    to honor `_MIN_COUNTS` (rounding drift is absorbed by RB/WR first — the
    deepest, most fungible positions — but never below their floor of 2).

    A `capacity` too small to satisfy every floor at once (below 8: 1 each
    for QB/TE/K/DEF plus 2 each for RB/WR) can't hit both goals — this never
    happens for any real `roster_positions` shape in this codebase (default
    capacity is 19), so the floors win and the returned counts may sum to
    slightly more than `capacity` in that degenerate case rather than
    silently violating the RB/WR minimum.
    """
    counts = {pos: max(1, round(frac * capacity)) for pos, frac in _POSITION_MIX.items()}
    for pos, floor in _MIN_COUNTS.items():
        counts[pos] = max(counts[pos], floor)

    diff = capacity - sum(counts.values())
    order = ["RB", "WR", "TE", "QB", "K", "DEF"]
    i = 0
    while diff != 0 and i < 10_000:  # bounded — diff shrinks by 1 every pass through `order`
        pos = order[i % len(order)]
        floor = _MIN_COUNTS.get(pos, 1)
        if diff > 0:
            counts[pos] += 1
            diff -= 1
        elif counts[pos] > floor:
            counts[pos] -= 1
            diff += 1
        i += 1
    return counts


def sample_roster(
    pool: Sequence[dict],
    projections: dict[str, float],
    roster_positions: dict[str, int],
    rng: random.Random,
    candidates_per_position: int = 40,
) -> list[dict]:
    """One seeded synthetic roster: `_target_counts(capacity)` players per
    position, drawn uniformly at random from that position's top
    `candidates_per_position` by this week's projection.

    The top-N-by-projection cutoff keeps the sample from including players
    no real manager would ever have rostered (a third-string tight end),
    without requiring an actual draft simulator to exist first. `pool` and
    `projections` are `ffbot.history.projections.player_pool`/
    `naive_projections`/`ecr_projections`'s own return shapes.
    """
    capacity = roster_capacity(roster_positions)
    target = _target_counts(capacity)

    by_pos: dict[str, list[dict]] = defaultdict(list)
    for row in pool:
        by_pos[row["position"]].append(row)
    for rows in by_pos.values():
        rows.sort(key=lambda r: -(projections.get(r["key"], 0.0) or 0.0))

    out: list[dict] = []
    for position, n in target.items():
        candidates = by_pos.get(position, [])[:candidates_per_position]
        if not candidates:
            continue
        k = min(n, len(candidates))
        out.extend(rng.sample(candidates, k))
    return out


def sample_rosters(
    pool: Sequence[dict],
    projections: dict[str, float],
    roster_positions: dict[str, int],
    n: int,
    seed: int,
) -> list[list[dict]]:
    """`n` independent seeded rosters — the replay unit `ffbot.backtest.replay`
    iterates over for one `(season, week)`. Deterministic given `seed`."""
    rng = random.Random(seed)
    return [sample_roster(pool, projections, roster_positions, rng) for _ in range(n)]
