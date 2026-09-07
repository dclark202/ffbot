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

That same property — value not depending on which eligible slot you fill —
means the *seating* is under-determined: many perfect matchings of the chosen
set score identically. Which one you pick is not cosmetic, because a fantasy
platform locks each player at their own kickoff and a flex slot is the only
one that accepts more than one position. See `_flex_seating_order` for the
rule this module uses to choose among them, and why.
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
    slot_permissiveness,
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
    # Players parked in an IR slot -- excluded from `assignments`/`bench`
    # entirely (see `optimize`'s own `held_in_ir` local), so a caller that
    # wants to show the WHOLE roster (e.g. the GUI's "My team" panel) needs
    # this third group explicitly rather than reconstructing it from the
    # other two.
    held_in_ir: list[Player] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not self.moves

    def as_lineup_write(self) -> list[dict]:
        """The payload shape a hypothetical future lineup-write call would
        need — `{player_id, selected_position}` per changed slot, including
        both sides of a swap (a real platform write API, were one ever
        available, would need the full changed set, not just players
        entering the lineup, to accept the resulting roster as valid).

        Sleeper's public API has no write capability at all (see
        docs/REFERENCE.md) — nothing in this repo actually calls this today.
        Kept as the shape any future write path would produce, not as
        evidence one exists.
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
    if status == "D" and not cfg.projection.doubtful_is_out:
        return None
    if status in STATUS_OUT:
        return f"status {status}"
    return None


def _fallback_points(player: Player, cfg: Config) -> float:
    """Estimate weekly output when Yahoo gives us no projection.

    Blends recent form with the season average. Noticeably worse than a real
    projection, which is why `projected_points` is preferred whenever present.
    """
    window = cfg.projection.recency_window
    recent = player.recent_points[-window:] if player.recent_points else []
    recent_avg = sum(recent) / len(recent) if recent else None
    season = player.season_avg_points

    if recent_avg is None and season is None:
        return 0.0
    if recent_avg is None:
        return float(season)  # type: ignore[arg-type]
    if season is None:
        return float(recent_avg)

    w = cfg.projection.recency_weight
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
        base *= cfg.projection.questionable_multiplier

    return base


def early_window_teams(kickoffs: dict[str, str] | None) -> frozenset[str]:
    """Teams playing BEFORE the week's main block, from `{team: ISO kickoff}`.

    Defined relative to the slate rather than against hardcoded NFL knowledge:
    the main block is simply the modal kickoff time of the week (the Sunday
    1pm ET window, which is most of the games), and anything strictly earlier
    is a standalone early window — Thursday night, the occasional Friday or
    Saturday game, and Sunday-morning international kickoffs. Later games
    (4pm, Sunday night, Monday night) are not early.

    Self-calibrating means it stays correct for a Week 18 slate with no
    Thursday game, for the international rounds, and for a league whose
    schedule this repo has never seen. Unparseable or missing values are
    simply not early — a team we know nothing about should not be pushed
    around by a rule that depends on knowing something.
    """
    if not kickoffs:
        return frozenset()
    parsed: dict[str, str] = {}
    for team, raw in kickoffs.items():
        text = str(raw or "").strip()
        if text:
            # ISO-8601 strings sort lexicographically iff they share a shape,
            # which `GameInfo.kickoff_et` guarantees ("YYYY-MM-DDTHH:MM").
            # Comparing as text avoids a tz-naive/aware datetime mismatch and
            # keeps this function pure.
            parsed[team] = text
    if not parsed:
        return frozenset()
    counts: dict[str, int] = {}
    for text in parsed.values():
        counts[text] = counts.get(text, 0) + 1
    # Ties on frequency break toward the EARLIER time, so a split slate can
    # only ever shrink the early set -- never invent one.
    main_block = min(counts, key=lambda t: (-counts[t], t))
    return frozenset(team for team, text in parsed.items() if text < main_block)


def _flex_seating_order(
    player: Player, scores: dict[int, float], early: frozenset[str]
) -> tuple[int, float, int, str]:
    """Sort key deciding who gets first claim on a DEDICATED slot — i.e. who
    is the worst candidate to leave sitting in the flex. Sorted ascending,
    so whoever ends up last is whoever the flex should hold.

    A flex is the only slot that accepts more than one position, which makes
    it the roster's late-swap valve: if the player sitting in it has to be
    replaced, the replacement can come from any of WR/RB/TE, and if a player
    in a *dedicated* slot goes down you can promote the flex occupant into
    that slot and backfill the flex from anywhere. Both of those maneuvers
    need the flex occupant to be someone you might actually move, and need
    him to still be unlocked. So, in order:

    1. **Not in an early standalone window.** A player whose game kicks off
       before the main block locks the flex for the entire rest of the week.
       Whatever else is true, park him in a dedicated slot -- an early game
       is the one condition that wastes the valve outright rather than merely
       using it poorly. This is why it outranks the projection: it should not
       be overturned by a tenth of a point of marginality.
    2. **Highest projected first.** Your best starter is the one you will
       never bench, so holding the flex with him spends the roster's only
       flexible slot on a certainty. The most replaceable starter -- the last
       man into the lineup -- is the one whose seat you actually want to be
       able to fill from anywhere.
    3. **Whoever already holds a flex sorts last**, so an exact tie on the
       two rules above leaves the lineup alone instead of swapping two
       interchangeable players and charging the user two drags in the Sleeper
       app for nothing. This is the minimal-move preference, demoted to where
       it belongs: a tie-break, not something that can veto the rule.
    4. **Name**, so the output is stable across runs (the same tie-break the
       scoring sort above uses).

    None of this can change WHO starts or cost a projected point: it only
    ever chooses among matchings that score identically.
    """
    return (
        0 if player.team in early else 1,
        -scores.get(player.player_id, 0.0),
        1 if slot_permissiveness(player.selected_position) > 1 else 0,
        player.name,
    )


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


def _reseat_reason(player: Player, to_slot: str, early: frozenset[str]) -> str:
    """Why a player already in the lineup is being moved to a different
    starting slot. Always a flexibility argument, never a points one — see
    `optimize`'s move loop.

    Reads both ends of the move, not just the destination: a
    multi-position-eligible player can be reseated between two DEDICATED
    slots (an RB/WR moving from RB to WR to free the flex for someone else),
    and calling that "out of flex" when no flex was involved would be a
    plainly wrong explanation of a move the user is being asked to make.
    """
    if slot_permissiveness(to_slot) > 1:
        return "flex: most replaceable starter (no points change)"
    if slot_permissiveness(player.selected_position) > 1:
        if player.team in early:
            return "out of flex: plays before the main block (no points change)"
        return "out of flex: too valuable to hold the flex (no points change)"
    return "reseated to free the flex (no points change)"


def optimize(
    players: list[Player],
    roster_positions: dict[str, int],
    week: int | None,
    cfg: Config,
    kickoffs: dict[str, str] | None = None,
) -> LineupPlan:
    """Compute the optimal lineup and the moves needed to reach it.

    `kickoffs` (`{team: ISO kickoff string}`, as carried by
    `ffbot.week.WeeklyIntel.games`) only refines the *seating* rule described
    in `_flex_seating_order` — which of several identically-scoring matchings
    to return. Omitted (the default, and what every season-long caller passes
    — `draft.need`, `board`, `denial`), the early-window half of the rule is
    inert and seating falls back to "most replaceable starter in the flex,"
    which needs no schedule at all.
    """
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

    # Phase 2 — decide *where* they sit. Every perfect matching of the chosen
    # set scores identically, so points cannot choose between them. What can:
    #
    #   (a) WHICH slot holds which player. A flex is the only slot accepting
    #       more than one position, and each player locks at his own kickoff,
    #       so seating your best (or earliest-playing) starter there spends
    #       the roster's only late-swap valve on someone who will never use
    #       it. `_flex_seating_order` is the rule and the reasoning.
    #   (b) How many players MOVE, since every move is a manual drag in the
    #       Sleeper app and a line in the audit log.
    #
    # (a) outranks (b), and that ordering is the whole point: a seating rule
    # that yielded to "but they're already sitting somewhere" could never fix
    # an existing bad lineup, which is the case that actually matters. A
    # roster imported from Sleeper arrives PRE-SEATED, so a minimal-move pass
    # on its own just ratifies whatever was already there and reports no
    # moves. (b) survives as the third component of the sort key, deciding
    # every exact tie, plus the fact that slots compare by NAME: a player
    # reseated from one WR slot to the other is not a move at all.
    early = early_window_teams(kickoffs)
    scores = {p.player_id: sc for sc, p in scored}
    order = sorted(
        range(len(starters)),
        key=lambda pi: _flex_seating_order(starters[pi], scores, early),
    )
    # Dedicated slots before flex ones, so each player claims the tightest
    # seat he fits and the flex is left for whoever `order` put last. The
    # original index is the final key so equally-permissive slots keep the
    # layout's own order and seating stays deterministic.
    slot_order = sorted(
        range(len(slots)), key=lambda si: (slot_permissiveness(slots[si]), si)
    )

    slot_owner: list[int | None] = [None] * len(slots)
    for pi in order:
        for si in slot_order:
            if slot_owner[si] is None and slot_accepts(slots[si], starters[pi]):
                slot_owner[si] = pi
                break

    for pi in range(len(starters)):
        if pi not in {o for o in slot_owner if o is not None}:
            # A perfect matching of `starters` exists by construction, but the
            # pass above is a first-fit and could in principle strand someone
            # where the player order and the slot order disagree. Fall back to
            # the augmenting search so a perfect matching is still guaranteed.
            _augment(pi, starters, slots, slot_owner, set())

    assignments: list[tuple[str, Player]] = []
    seated: set[int] = set()
    for si, owner in enumerate(slot_owner):
        if owner is not None:
            assignments.append((slots[si], starters[owner]))
            seated.add(starters[owner].player_id)

    unfilled = [slots[si] for si, owner in enumerate(slot_owner) if owner is None]
    bench = [p for p in pool if p.player_id not in seated]

    moves: list[Move] = []

    for slot, p in assignments:
        if p.selected_position == slot:
            continue
        # A player moving between two STARTING slots was already in the
        # lineup, so the move gains nothing on the scoreboard -- it is the
        # seating rule at work. Say so, rather than labelling it with a
        # projection the way a genuine promotion off the bench is labelled;
        # a zero-point move sitting unexplained in a list of point-gaining
        # ones reads as churn, which is exactly the complaint the old
        # minimal-move pass existed to avoid.
        if p.selected_position not in (BENCH,) and p.selected_position not in IR_SLOTS:
            reason = _reseat_reason(p, slot, early)
        else:
            reason = f"proj {scores[p.player_id]:.1f}"
        moves.append(Move(p, p.selected_position, slot, reason))

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
        held_in_ir=held_in_ir,
    )
