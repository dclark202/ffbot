"""N-manager snake draft simulation from a historical board — generalizes
`tests/test_edge.py`'s `_simulate` (a single-manager version scoped to that
test file) for B4's season simulator and the draft A/B (see docs/dev/BACKTEST.md).

One manager (`agent_slot`) drafts either via `draft.recommend()` or by the
same noisy-ADP process every other manager uses; running the identical seed
both ways, against the identical opponents, is the draft A/B — the only
variable that changes is how the agent's own slot picks. The
`recommend()`-drafted roster also replaces B3's *sampled* rosters with a
real drafted one, checking that B3's roster-sampling substitution (see
`ffbot/backtest/rosters.py`) didn't quietly bias its conclusions.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..board import Board
from ..config import Config
from ..draft import DraftState, recommend, team_slot_at


@dataclass(frozen=True)
class DraftResult:
    # `rosters[i]` is the list of board keys drafted by team slot `i + 1`
    # (0-indexed list, 1-indexed slot — matches `DraftState.my_slot`'s own
    # 1-indexed convention; `agent_roster()` below hides the off-by-one).
    rosters: list[list[str]]
    state: DraftState


def agent_roster(result: DraftResult, agent_slot: int) -> list[str]:
    """The board keys drafted by `agent_slot` (1-indexed) — the convenience
    accessor for the common case of wanting just the agent's own roster."""
    return result.rosters[agent_slot - 1]


def _adp_order(board: Board, rng: random.Random, adp_noise: float) -> list[str]:
    """Board keys ordered by ADP, jittered by `adp_noise * adp_stdev` (or a
    10%-of-ADP fallback spread when `adp_stdev` is missing) — real opponents
    behave like a noisy market, not a perfectly deterministic ADP list, and
    without the jitter every seed would draft an identical opponent order.
    `adp_noise=0.0` disables jitter entirely for a deterministic ADP draft.
    """
    scored: list[tuple[float, str]] = []
    for bp in board.players:
        if bp.adp is None:
            continue
        stdev = bp.adp_stdev if bp.adp_stdev else max(1.0, bp.adp * 0.1)
        jitter = rng.gauss(0.0, stdev * adp_noise) if adp_noise > 0 else 0.0
        scored.append((bp.adp + jitter, bp.key))
    scored.sort(key=lambda t: t[0])
    return [key for _adp, key in scored]


def simulate_draft(
    board: Board,
    cfg: Config,
    num_teams: int,
    rounds: int,
    agent_slot: int,
    seed: int,
    agent_uses_recommend: bool = True,
    adp_noise: float = 1.0,
    order: str = "snake",
) -> DraftResult:
    """A full `num_teams`-team draft over `rounds` rounds.

    `agent_uses_recommend=True` (default): `agent_slot` picks via
    `draft.recommend()`, every other slot drafts by noisy ADP.
    `agent_uses_recommend=False`: every slot, including `agent_slot`, drafts
    by the identical noisy-ADP process — the "just follow the market"
    counterfactual for the exact same seed and opponents.

    Opponents must NEVER be routed through `recommend()` — it applies the
    AGENT's caps/targets/forced-fill against the AGENT's own roster; routing
    every slot through it once had every opponent forced onto defenses
    until the pool ran dry (see `tests/test_edge.py::_simulate`'s own
    docstring, which this function generalizes). Real opponents behave like
    the market, which is what ADP is.
    """
    rng = random.Random(seed)
    state = DraftState(
        board=board, num_teams=num_teams, my_slot=agent_slot, rounds=rounds,
        roster_positions=cfg.roster_positions, order=order,
    )
    my_picks = set(state.my_picks())
    adp_order = _adp_order(board, rng, adp_noise)
    cursor = 0

    rosters: list[list[str]] = [[] for _ in range(num_teams)]
    total_picks = rounds * num_teams
    for pick in range(1, total_picks + 1):
        slot = team_slot_at(pick, num_teams, order)
        is_agent_pick = pick in my_picks

        if is_agent_pick and agent_uses_recommend:
            recs = recommend(state, cfg, limit=1)
            if not recs:
                break
            key = recs[0].player.key
        else:
            while cursor < len(adp_order) and adp_order[cursor] in state.taken_keys():
                cursor += 1
            if cursor >= len(adp_order):
                break
            key = adp_order[cursor]

        state.record(key, mine=is_agent_pick)
        rosters[slot - 1].append(key)

    return DraftResult(rosters=rosters, state=state)
