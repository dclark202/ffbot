"""Starting-lineup optimizer.

Deterministic and offline: given a roster and the league's slot layout, produce
the highest-scoring legal lineup and the minimal set of position changes to
reach it. No LLM in this path — the Sunday-morning run has a hard deadline and
this needs to be predictable and testable.

The assignment is solved exactly rather than heuristically. Because a player's
value does not depend on *which* eligible slot they fill, the set of startable
players forms a transversal matroid, so taking players in descending score
order and keeping each one that can be matched (via augmenting path) yields the
optimal lineup — not merely a good one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .models import (
    BENCH,
    IR_SLOTS,
    STATUS_OUT,
    Player,
    slot_accepts,
    starting_slots,
)


@dataclass
class Move:
    """One position change to send to Yahoo."""

    player: Player
    from_slot: str
    to_slot: str
    reason: str

    def __str__(self) -> str:
        return f"{self.player.name}: {self.from_slot} -> {self.to_slot} ({self.reason})"


@dataclass
class LineupPlan:
    assignments: list[tuple[str, Player]] = field(default_factory=list)
    bench: list[Player] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    unfilled_slots: list[str] = field(default_factory=list)
    benched_for_cause: list[tuple[Player, str]] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not self.moves

    def as_yahoo_changes(self) -> list[dict]:
        """The payload shape `yahoo_fantasy_api.Team.change_positions` expects.

        Every player whose slot changed must be included, not just the ones
        entering the lineup, or Yahoo rejects the resulting roster as invalid.
        """
        return [
            {"player_id": m.player.player_id, "selected_position": m.to_slot}
            for m in self.moves
        ]


def _must_bench(player: Player, week: int | None, cfg: Config) -> str | None:
    """Return a reason string when the player cannot be started, else None."""
    if week is not None and player.bye_week == week:
        return f"bye week {week}"

    status = player.status
    if status == "D" and not cfg.scoring.doubtful_is_out:
        return None
    if status in STATUS_OUT:
        return f"status {status}"
    return None


def _fallback_points(player: Player, cfg: Config) -> float:
    """Estimate weekly output when Yahoo gives us no projection.

    Blends recent form with the season average. Noticeably worse than a real
    projection, which is why `projected_points` is preferred whenever present.
    """
    window = cfg.scoring.recency_window
    recent = player.recent_points[-window:] if player.recent_points else []
    recent_avg = sum(recent) / len(recent) if recent else None
    season = player.season_avg_points

    if recent_avg is None and season is None:
        return 0.0
    if recent_avg is None:
        return float(season)  # type: ignore[arg-type]
    if season is None:
        return float(recent_avg)

    w = cfg.scoring.recency_weight
    return w * recent_avg + (1.0 - w) * season


def score_player(player: Player, week: int | None, cfg: Config) -> float | None:
    """Expected points this week, or None when the player must not be started."""
    if _must_bench(player, week, cfg) is not None:
        return None

    base = (
        float(player.projected_points)
        if player.projected_points is not None
        else _fallback_points(player, cfg)
    )

    if player.is_questionable() or player.status == "D":
        base *= cfg.scoring.questionable_multiplier

    return base


def _augment(
    pi: int,
    players: list[Player],
    slots: list[str],
    slot_owner: list[int | None],
    visited: set[int],
) -> bool:
    """Kuhn's augmenting path: try to seat player `pi`, displacing if needed."""
    for si, slot in enumerate(slots):
        if si in visited or not slot_accepts(slot, players[pi]):
            continue
        visited.add(si)
        owner = slot_owner[si]
        if owner is None or _augment(owner, players, slots, slot_owner, visited):
            slot_owner[si] = pi
            return True
    return False


def optimize(
    players: list[Player],
    roster_positions: dict[str, int],
    week: int | None,
    cfg: Config,
) -> LineupPlan:
    """Compute the optimal lineup and the moves needed to reach it."""
    slots = starting_slots(roster_positions)

    # Players parked in an IR slot stay there — they cannot score, and pulling
    # them out would need a bench spot we may not have. A player who has
    # recovered is no longer IR-eligible and rejoins the pool automatically.
    held_in_ir = [
        p for p in players if p.selected_position in IR_SLOTS and p.is_ir_eligible()
    ]
    held_ids = {p.player_id for p in held_in_ir}
    pool = [p for p in players if p.player_id not in held_ids]

    benched_for_cause: list[tuple[Player, str]] = []
    scored: list[tuple[float, Player]] = []
    for p in pool:
        reason = _must_bench(p, week, cfg)
        if reason is not None:
            if p.selected_position not in (BENCH,) and p.selected_position not in IR_SLOTS:
                benched_for_cause.append((p, reason))
            continue
        s = score_player(p, week, cfg)
        if s is not None:
            scored.append((s, p))

    # Descending score; name breaks ties so output is stable across runs.
    scored.sort(key=lambda t: (-t[0], t[1].name))
    candidates = [p for _, p in scored]

    # Phase 1 — decide *who* starts. Greedy over a transversal matroid, so the
    # resulting set maximises total projected points exactly.
    chosen: list[int | None] = [None] * len(slots)
    for pi in range(len(candidates)):
        _augment(pi, candidates, slots, chosen, set())
    starters = [candidates[o] for o in chosen if o is not None]

    # Phase 2 — decide *where* they sit. Any perfect matching of the chosen set
    # scores identically, so prefer the one that leaves players where they
    # already are. Without this the optimizer churns equivalent slots (moving a
    # WR into the flex and the flex player into WR) for no gain, which costs a
    # write to Yahoo and makes the audit log unreadable.
    slot_owner: list[int | None] = [None] * len(slots)
    for pi, p in enumerate(starters):
        for si, slot in enumerate(slots):
            if (
                slot_owner[si] is None
                and slot == p.selected_position
                and slot_accepts(slot, p)
            ):
                slot_owner[si] = pi
                break

    for pi in range(len(starters)):
        if pi not in {o for o in slot_owner if o is not None}:
            # A perfect matching of `starters` exists by construction, so this
            # always succeeds; it may displace a player seated above, which is
            # still an improvement on moving everyone.
            _augment(pi, starters, slots, slot_owner, set())

    assignments: list[tuple[str, Player]] = []
    seated: set[int] = set()
    for si, owner in enumerate(slot_owner):
        if owner is not None:
            assignments.append((slots[si], starters[owner]))
            seated.add(starters[owner].player_id)

    unfilled = [slots[si] for si, owner in enumerate(slot_owner) if owner is None]
    bench = [p for p in pool if p.player_id not in seated]

    scores = {p.player_id: s for s, p in scored}
    moves: list[Move] = []

    for slot, p in assignments:
        if p.selected_position != slot:
            moves.append(
                Move(p, p.selected_position, slot, f"proj {scores[p.player_id]:.1f}")
            )

    cause = {p.player_id: r for p, r in benched_for_cause}
    for p in bench:
        if p.selected_position == BENCH:
            continue
        reason = cause.get(p.player_id)
        if reason is None:
            s = scores.get(p.player_id)
            reason = f"outscored (proj {s:.1f})" if s is not None else "not startable"
        moves.append(Move(p, p.selected_position, BENCH, reason))

    return LineupPlan(
        assignments=assignments,
        bench=bench,
        moves=moves,
        unfilled_slots=unfilled,
        benched_for_cause=benched_for_cause,
    )
