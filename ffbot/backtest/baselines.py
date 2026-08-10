"""The five lineups scored for one (season, week, sampled roster) — see
docs/BACKTEST.md's baselines table.

| Name          | How it's built                                                |
|---------------|----------------------------------------------------------------|
| oracle        | `lineup.optimize()` on realized points — ceiling               |
| control       | `lineup.optimize()` on raw projections, no spice — THE CONTROL |
| agent         | `week.adjusted_players()` then `lineup.optimize()` — the system|
| consensus     | Best-projected healthy player per slot, greedy, no optimizer   |
| random_legal  | Uniform-ish over legal slot assignments, seeded — floor        |

All five are built from the exact same sampled roster (`roster_rows`), which
is what makes the agent-vs-control comparison a true paired A/B rather than
two different samples. `oracle` and `control`/`agent` differ only in which
points dictionary drives them — same players, same statuses, same slots.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .. import week as weekmod
from ..config import Config
from ..lineup import optimize
from ..models import (
    SLOT_ELIGIBILITY,
    STATUS_OUT,
    Player,
    slot_accepts,
    starting_slots,
)
from ..history.projections import players_asof

if TYPE_CHECKING:
    from ..history.index import WeekSnapshot


@dataclass(frozen=True)
class Lineup:
    name: str
    assignments: list[tuple[str, Player]]
    bench: list[Player]


def key_by_player_id(roster_rows: Sequence[dict]) -> dict[int, str]:
    """`player_id -> actuals_key` for one sampled roster.

    `players_asof` assigns `player_id = 1..len(pool)` by enumeration order
    (see its docstring), so calling it repeatedly on the SAME `roster_rows`
    list — once per baseline, with a different points dict each time —
    always produces the same id-to-identity mapping. This is what lets
    `metrics.score_lineup` grade any of the five lineups against
    `actuals.week_actuals` without needing the `Lineup` itself to carry
    identity information beyond the `Player` objects it already has.
    """
    return {i: row["key"] for i, row in enumerate(roster_rows, start=1)}


def _greedy_consensus_lineup(
    players: Sequence[Player], roster_positions: dict[str, int]
) -> tuple[list[tuple[str, Player]], list[Player]]:
    """"Start the best-projected healthy player, never reconsider" — the
    "just follow the market" floor. Dedicated single-position slots are
    filled before flex slots (a real "just start the best guy" manager fills
    the obvious slots first, then whatever's left goes in flex), and once a
    slot claims a player the assignment never changes — the property that
    distinguishes this from `lineup.optimize()`'s global, swap-aware search.
    """
    slots = starting_slots(roster_positions)
    slots_ordered = sorted(slots, key=lambda s: len(SLOT_ELIGIBILITY.get(s, frozenset())))

    available = [p for p in players if p.status not in STATUS_OUT and p.projected_points is not None]
    available.sort(key=lambda p: (-(p.projected_points or 0.0), p.name))

    assignments: list[tuple[str, Player]] = []
    used: set[int] = set()
    for slot in slots_ordered:
        for p in available:
            if p.player_id in used:
                continue
            if slot_accepts(slot, p):
                assignments.append((slot, p))
                used.add(p.player_id)
                break
    bench = [p for p in players if p.player_id not in used]
    return assignments, bench


def _random_legal_lineup(
    players: Sequence[Player], roster_positions: dict[str, int], rng: random.Random
) -> tuple[list[tuple[str, Player]], list[Player]]:
    """A uniform-ish random legal slot assignment — deliberately the ONE
    baseline that does NOT respect hard-out status. It calibrates whether
    the other four differ from pure chance at all; it is not meant to be a
    plausible manager, so injury/bye availability is out of scope for it by
    design, not by oversight.
    """
    slots = list(starting_slots(roster_positions))
    rng.shuffle(slots)
    pool = list(players)
    rng.shuffle(pool)

    assignments: list[tuple[str, Player]] = []
    used: set[int] = set()
    for slot in slots:
        for p in pool:
            if p.player_id in used:
                continue
            if slot_accepts(slot, p):
                assignments.append((slot, p))
                used.add(p.player_id)
                break
    bench = [p for p in players if p.player_id not in used]
    return assignments, bench


def build_baselines(
    roster_rows: Sequence[dict],
    projections: dict[str, float],
    actuals_by_key: dict[str, float],
    snapshot: "WeekSnapshot",
    cfg: Config,
    week: int,
    seed: int,
) -> dict[str, Lineup]:
    """The five lineups for one `(roster_rows, season, week)` unit.

    `control`/`agent`/`consensus`/`random_legal` are all built from the same
    `control_players` list (raw projections + pre-game status, via
    `players_asof`) — `agent` layers `week.adjusted_players` on top of it,
    exactly as the live weekly path does; `consensus`/`random_legal` are
    hand-built rather than run through the optimizer, since the entire point
    of comparing against them is that they DON'T do global optimal
    assignment. `oracle` is the same players re-scored on realized points
    (`actuals_by_key`, from `ffbot.history.actuals.week_actuals` — the one
    permitted ground-truth read, per this package's `__init__` docstring)
    with status left exactly as it was pre-game, so the oracle knows the
    final score but still can't start someone who was ruled out.
    """
    control_players = players_asof(roster_rows, projections, snapshot)
    oracle_players = players_asof(roster_rows, actuals_by_key, snapshot)
    agent_players = weekmod.adjusted_players(
        control_players, snapshot.weekly_intel(), cfg.season, snapshot.stadiums
    )

    oracle_plan = optimize(oracle_players, cfg.roster_positions, week, cfg)
    control_plan = optimize(control_players, cfg.roster_positions, week, cfg)
    agent_plan = optimize(agent_players, cfg.roster_positions, week, cfg)

    consensus_assign, consensus_bench = _greedy_consensus_lineup(control_players, cfg.roster_positions)
    random_assign, random_bench = _random_legal_lineup(control_players, cfg.roster_positions, random.Random(seed))

    return {
        "oracle": Lineup("oracle", oracle_plan.assignments, oracle_plan.bench),
        "control": Lineup("control", control_plan.assignments, control_plan.bench),
        "agent": Lineup("agent", agent_plan.assignments, agent_plan.bench),
        "consensus": Lineup("consensus", consensus_assign, consensus_bench),
        "random_legal": Lineup("random_legal", random_assign, random_bench),
    }
