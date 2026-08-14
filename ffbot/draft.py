"""Live snake-draft assistant: pick math, survival, and recommendations.

Works entirely offline. `DraftState` tracks the board and the pick log;
`recommend()` scans the whole remaining player pool every time (cheap enough
that no shortlist or approximation is needed — see the module-level
benchmarks in tests/test_draft.py) and returns it ranked by `value()`, which
builds on `lineup.optimize()` rather than hand-tuned positional weights.

Every `optimize()` call in `need()`, `value()`, and `recommend()`'s main scan
passes `week=None`. Passing a real week silently zeroes any player on bye
that week, which is correct for the in-season lineup path and wrong here —
draft valuation is a season-long decision, not a single week's. Two
deliberate, narrow exceptions pass a real week: the exact `BYE:` alert in
`alerts()`, and `_bye_pressure` (which sizes `bye_collision_weight`) — both
compute a single-week fact (does this bye actually cost me a start?) rather
than a season-long valuation, and neither feeds back into `need`.
"""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Sequence

from . import edge
from .board import Board, BoardPlayer, to_player
from .config import Config, DraftConfig
from .edge import EdgeContext
from .lineup import optimize
from .models import BENCH, IR_SLOTS, SLOT_ELIGIBILITY, Player, slot_accepts, starting_slots

# --- Snake order -------------------------------------------------------


def pick_number(round_: int, slot: int, num_teams: int, order: str = "snake") -> int:
    """Overall pick number for `slot` (1-indexed draft position) in `round_`.

    `order="snake"` (default): odd rounds run slot 1..T left to right; even
    rounds reverse. `order="linear"`: every round runs slot 1..T the same
    way (same team always picks first).
    """
    if round_ < 1:
        raise ValueError("round_ must be >= 1")
    if not (1 <= slot <= num_teams):
        raise ValueError(f"slot must be in 1..{num_teams}")
    if order == "linear":
        return (round_ - 1) * num_teams + slot
    if round_ % 2 == 1:
        return (round_ - 1) * num_teams + slot
    return (round_ - 1) * num_teams + (num_teams - slot + 1)


def round_and_slot(pick: int, num_teams: int, order: str = "snake") -> tuple[int, int]:
    """Inverse of `pick_number`: overall pick -> (round, slot)."""
    if pick < 1:
        raise ValueError("pick must be >= 1")
    round_ = (pick - 1) // num_teams + 1
    pos_in_round = pick - (round_ - 1) * num_teams  # 1..num_teams
    if order == "linear" or round_ % 2 == 1:
        slot = pos_in_round
    else:
        slot = num_teams - pos_in_round + 1
    return round_, slot


def team_slot_at(pick: int, num_teams: int, order: str = "snake") -> int:
    """The draft slot (team) on the clock at `pick`."""
    return round_and_slot(pick, num_teams, order)[1]


def my_pick_numbers(slot: int, num_teams: int, rounds: int, order: str = "snake") -> list[int]:
    """Every overall pick number belonging to draft slot `slot`."""
    return [pick_number(r, slot, num_teams, order) for r in range(1, rounds + 1)]


def picks_until(current_pick: int, my_picks: Sequence[int]) -> int | None:
    """Picks remaining before the next pick in `my_picks` at or after `current_pick`.

    0 means `current_pick` itself is mine. None means there are no more.
    """
    upcoming = [p for p in my_picks if p >= current_pick]
    if not upcoming:
        return None
    return min(upcoming) - current_pick


# --- ADP survival --------------------------------------------------------


def sigma_for(adp: float, stdev: float | None, cfg: DraftConfig) -> float:
    """Standard deviation of a player's ADP, from the CSV if given, else derived."""
    if stdev is not None and stdev > 0:
        return stdev
    return max(cfg.adp_sigma_floor, cfg.adp_sigma_scale * adp)


def survival(adp: float, sigma: float, from_pick: int, to_pick: int) -> float:
    """P(the player is still available at `to_pick` | available at `from_pick`).

    Models draft position as Normal(adp, sigma) and treats "taken by pick k"
    as P(position <= k). `from_pick` is normally the current overall pick —
    we already know the player is available there, so conditioning on that
    tightens the estimate for the picks after it.
    """
    if to_pick <= from_pick:
        return 1.0
    dist = NormalDist(mu=adp, sigma=max(sigma, 1e-6))
    numerator = 1.0 - dist.cdf(to_pick - 1)
    denominator = 1.0 - dist.cdf(from_pick - 1)
    if denominator <= 1e-9:
        # The model says this player should already be gone; conditioning on
        # an ~impossible event is undefined, so treat further survival as 0
        # rather than dividing by ~zero.
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


# --- Draft state -----------------------------------------------------------


@dataclass
class Pick:
    number: int
    key: str | None  # None = a pick happened but we don't know who
    mine: bool
    source: str = "manual"  # "manual" | "api"


@dataclass
class DraftState:
    board: Board
    num_teams: int
    my_slot: int
    rounds: int
    roster_positions: dict[str, int]
    my_picks_override: list[int] = field(default_factory=list)
    picks: list[Pick] = field(default_factory=list)
    order: str = "snake"

    def my_picks(self) -> list[int]:
        """Every overall pick number that belongs to me.

        `my_picks_override` (keepers, traded picks) wins whenever it is
        non-empty; it need not follow the arithmetic snake progression.
        """
        if self.my_picks_override:
            return list(self.my_picks_override)
        return my_pick_numbers(self.my_slot, self.num_teams, self.rounds, self.order)

    def current_pick(self) -> int:
        return len(self.picks) + 1

    def next_my_pick(self) -> int | None:
        upcoming = [p for p in self.my_picks() if p >= self.current_pick()]
        return min(upcoming) if upcoming else None

    def next_my_pick_after(self, pick: int) -> int | None:
        """My next pick STRICTLY after `pick` — the one survival is measured to.

        Every "if I pass on him, is he still there when I come back?"
        question is about this number, never `next_my_pick()`. The two agree
        while someone else is on the clock (the current pick isn't mine, so
        `>= current` and `> current` select the same pick), but they diverge
        at exactly the moment the question matters: on my own clock
        `next_my_pick()` returns the pick I am making right now, and
        `survival(..., from_pick=p, to_pick=p)` is 1.0 by definition — so
        every candidate reported "100% survives" on precisely the picks I
        actually had to decide. See tests/test_draft.py::TestSurvivalTarget.
        """
        upcoming = [p for p in self.my_picks() if p > pick]
        return min(upcoming) if upcoming else None

    def taken_keys(self) -> set[str]:
        return {p.key for p in self.picks if p.key is not None}

    def my_roster(self) -> list[BoardPlayer]:
        return [
            self.board.by_key[p.key]
            for p in self.picks
            if p.mine and p.key is not None and p.key in self.board.by_key
        ]

    def record(self, key: str | None, mine: bool | None = None, source: str = "manual") -> Pick:
        """Log the next pick. `mine=None` auto-infers from the pick number.

        Raises `ValueError` (state left unchanged) if `key` was already
        recorded — callers on the hot path should catch this and surface it
        as a message rather than let it propagate.
        """
        number = self.current_pick()
        if key is not None and key in self.taken_keys():
            raise ValueError(f"{key} is already taken")
        if mine is None:
            mine = number in self.my_picks()
        pick = Pick(number=number, key=key, mine=mine, source=source)
        self.picks.append(pick)
        return pick

    def undo(self) -> Pick | None:
        if not self.picks:
            return None
        return self.picks.pop()

    def slot_for(self, pick: Pick) -> int | None:
        """The draft slot that made `pick`.

        Derived from the pick number (`team_slot_at`), never stored -- so
        every existing `draft_log.jsonl` replays identically. Two guards for
        `my_picks_override` (keepers/traded picks), the one thing that can
        make the arithmetic slot wrong:

        - A pick I actually made (`pick.mine`) is always attributed to
          `my_slot`, even if the arithmetic slot for that number belongs to
          someone else -- that is the whole point of an override.
        - A pick that arithmetic says was mine, but that I did NOT make (I
          traded it away), has no known owner -- the override tells us I
          lost it, not who received it -- so this returns `None` rather
          than asserting the wrong team.

        Every other pick is exact: the override only ever describes my own
        picks, so an opponent's arithmetic slot is unaffected by it.
        """
        if pick.mine:
            return self.my_slot
        arithmetic_slot = team_slot_at(pick.number, self.num_teams, self.order)
        if self.my_picks_override and arithmetic_slot == self.my_slot:
            return None
        return arithmetic_slot

    def rosters_by_slot(self) -> dict[int, list[BoardPlayer]]:
        """Every known team's roster so far, keyed by draft slot.

        Picks with an unknown slot (see `slot_for`) or unknown player
        (`key is None`, or a key that isn't on the board) are simply
        omitted. Silently dropping the player would be wrong for `my_roster`
        (it never does this), but a partially-known opponent roster is
        still useful, and there is no "unknown player" placeholder worth
        showing.
        """
        out: dict[int, list[BoardPlayer]] = defaultdict(list)
        for p in self.picks:
            if p.key is None:
                continue
            bp = self.board.by_key.get(p.key)
            if bp is None:
                continue
            slot = self.slot_for(p)
            if slot is None:
                continue
            out[slot].append(bp)
        return dict(out)


# --- Valuation -------------------------------------------------------------


def _season_score(board: Board, roster_keys: Sequence[str], extra: BoardPlayer | None, cfg: Config) -> float:
    """Total points landing in a starting slot for `roster_keys` (+ `extra`)."""
    players: list[Player] = [
        to_player(board.by_key[k], uid) for uid, k in enumerate(roster_keys, start=1)
    ]
    if extra is not None:
        players.append(to_player(extra, uid=len(players) + 1))
    plan = optimize(players, cfg.roster_positions, None, cfg)
    return sum(p.projected_points for _, p in plan.assignments)


def _replacement_board_player(
    position: str, points: float, bye_week: int | None = None
) -> BoardPlayer:
    return BoardPlayer(
        key=f"__replacement_{position}__",
        name=f"replacement {position}",
        position=position,
        team="",
        bye_week=bye_week,
        points=points,
        adp=None,
        adp_stdev=None,
        adp_spread=None,
        platform_id=None,
        tier=0,
        vor=0.0,
        rank=0,
    )


def _bye_pressure(
    roster: Sequence[BoardPlayer],
    roster_positions: dict[str, int],
    position: str,
    bye_week: int,
    cfg: Config,
) -> float:
    """0..1: whether the roster ALREADY goes short at `position` during
    `bye_week` -- i.e. whether drafting another `position` player with that
    same bye would stack onto an existing hole rather than create a new one.

    Deliberately a property of the roster and week alone, not of the
    candidate: a lone player is *always* unavailable during their own bye,
    for every possible week, so comparing "this candidate on bye" against
    "this candidate always available" is nonzero for literally any bye and
    never discriminates between weeks -- it isn't measuring collision at
    all. What matters is whether OTHER already-rostered players at this
    position collide with the same week, which is exactly the same
    optimizer diff the exact `BYE:` alert in `alerts()` uses (flex-aware,
    unlike `_candidate_flags`'s naive per-position count), filtered to
    slots this position could fill. Exactly 0 on a fully empty roster --
    there is nothing yet to stack onto -- and grows as real depth at the
    position already collides with the same week (a single rostered player
    on that bye can already cost a multi-slot position one of its starting
    spots, which is real exposure, not noise).
    """
    if not roster:
        return 0.0
    starters = _starters_for_position(position, roster_positions)
    if starters == 0:
        return 0.0
    roster_players = [to_player(bp, uid) for uid, bp in enumerate(roster, start=1)]

    baseline = Counter(optimize(roster_players, roster_positions, None, cfg).unfilled_slots)
    at_week = Counter(optimize(roster_players, roster_positions, bye_week, cfg).unfilled_slots)
    extra = at_week - baseline
    caused = sum(
        n for slot, n in extra.items() if position in SLOT_ELIGIBILITY.get(slot, frozenset({slot}))
    )
    return max(0.0, min(1.0, caused / starters))


def demand_ahead(state: DraftState, cfg: Config) -> dict[str, float]:
    """Position -> 0..1 demand from teams picking before my next turn.

    For every pick between now and my next turn, the team on the clock's
    *current* roster (never a prediction of what they will take) is run
    through the same optimizer the lineup path uses, and its unfilled
    starting slots become that team's demand. A slot maps to every real
    position it accepts (`SLOT_ELIGIBILITY`) -- an unfilled flex counts
    toward RB, WR, and TE alike, since it is genuinely ambiguous which one
    fills it. Nearer picks are weighted higher than distant ones: the team
    on the clock right now is far more likely to actually take the position
    than one picking eight selections from now, most of which resolves via
    other signals long before it arrives.

    Deliberately positional, not per-opponent-per-candidate: reconstructing
    every opponent roster and running the full `need()` machinery for every
    candidate against every opponent would be roughly (candidates x
    opponents) optimizer calls per pick -- thousands, well past the pick
    clock. This costs at most one `optimize()` per team in the gap. Compare
    `denial.denial_value`, which affords the expensive per-opponent shape
    only because it runs once a week against a ~150-player waiver pool, not
    every pick against 500+.

    Empty when there is no next pick of mine, or it is immediately next
    (nobody picks in between).
    """
    current = state.current_pick()
    next_pick = state.next_my_pick()
    if next_pick is None or next_pick <= current:
        return {}

    gap = range(current, next_pick)
    rosters = state.rosters_by_slot()

    demand: dict[str, float] = defaultdict(float)
    total_weight = 0.0
    seen_slots: set[int] = set()
    gap_len = len(gap)
    for i, pick_num in enumerate(gap):
        slot = team_slot_at(pick_num, state.num_teams, state.order)
        if slot == state.my_slot or slot in seen_slots:
            continue
        seen_slots.add(slot)
        weight = 1.0 - (i / gap_len)  # nearer picks (small i) weigh more

        roster_players = [
            to_player(bp, uid) for uid, bp in enumerate(rosters.get(slot, []), start=1)
        ]
        plan = optimize(roster_players, state.roster_positions, None, cfg)
        for unfilled_slot in set(plan.unfilled_slots):
            for pos in SLOT_ELIGIBILITY.get(unfilled_slot, frozenset({unfilled_slot})):
                demand[pos] += weight
        total_weight += weight

    if total_weight <= 0:
        return {}
    return {pos: min(1.0, v / total_weight) for pos, v in demand.items()}


def need(candidate: BoardPlayer, roster_keys: Sequence[str], board: Board, cfg: Config) -> float:
    """How much better drafting `candidate` is than drafting a replacement-level
    player at the same position, given the roster you already have.

    Reduces to exactly `candidate.vor` on an empty roster and to ~0 once your
    starting lineup at that position is already saturated with better
    players — see tests/test_draft.py for both properties. Exception: if no
    slot in `cfg.roster_positions` can ever accept the position at all (e.g.
    a K in a no-kicker league), this is exactly 0 for every candidate at
    that position regardless of roster — never equal to `vor`, which is a
    separate, static number fixed at board-build time and merely guaranteed
    to be <= 0 in that case.
    """
    base = _season_score(board, roster_keys, None, cfg)
    marginal_x = _season_score(board, roster_keys, candidate, cfg) - base

    repl_points = board.replacement.get(candidate.position)
    if repl_points is None:
        return marginal_x
    repl = _replacement_board_player(candidate.position, repl_points)
    marginal_repl = _season_score(board, roster_keys, repl, cfg) - base
    return marginal_x - marginal_repl


def _depth_factors(
    roster: Sequence[BoardPlayer], roster_positions: dict[str, int], cfg: Config
) -> dict[str, float]:
    """Per-position multiplier on the bench-depth term, by how deep you already are.

    Bench depth is insurance: the first backup at a position covers a bye or an
    injury, the fourth covers almost nothing. `need` cannot express this — once
    your starters are full it is zero for every candidate alike — so without a
    decay the ranking falls back to raw VOR and happily stacks seven players at
    whichever position the market undervalues.
    """
    counts: dict[str, int] = defaultdict(int)
    for bp in roster:
        counts[bp.position] += 1

    factors: dict[str, float] = {}
    for pos, n in counts.items():
        # A configured target overrides the starter count as the point where
        # decay begins. The flex slot makes a 2nd TE technically "startable",
        # so starter-based surplus never discounts it — but a league strategy
        # of "really only 1 TE" wants exactly that discount.
        target = cfg.draft.position_targets.get(pos)
        baseline = target if target is not None else _starters_for_position(pos, roster_positions)
        # n + 1: the factor describes what the *candidate* is worth, so it is
        # the roster this pick would create that matters. Measuring the roster
        # as it stands charges the third tight end the second one's rate and
        # the decay never bites.
        surplus = max(0, (n + 1) - baseline)
        factors[pos] = cfg.draft.depth_decay ** surplus
    return factors


def _combine(
    need_val: float, candidate: BoardPlayer, ctx: EdgeContext, cfg: Config
) -> float:
    """The one place a final ranking score is assembled.

    `value()` and `recommend()` both route through here. They previously
    computed the same formula independently, which meant any new term had to be
    added in two places or the public function and the actual recommendations
    would silently disagree.
    """
    depth = cfg.draft.depth_weight * max(0.0, candidate.vor)
    depth *= ctx.depth_factor.get(candidate.position, 1.0)
    return need_val + depth + edge.bonus(candidate, ctx, cfg)


def value(
    candidate: BoardPlayer,
    roster_keys: Sequence[str],
    board: Board,
    cfg: Config,
    ctx: EdgeContext | None = None,
) -> float:
    """`need`, plus bench depth, plus the edge terms.

    `need` alone collapses to 0 once your starters at a position are full —
    correct for roster construction, but it makes every late-round player
    look equally worthless. The depth term (VOR, clamped at 0) is what a
    player who won't crack your starting lineup is still worth: bye cover,
    injury insurance, upside. It cannot change the ranking at either
    extreme (empty or saturated roster) — only in the middle rounds where
    roster construction and depth genuinely trade off.

    `ctx` is optional so this stays usable as a one-shot call; pass one from
    `edge.build_context` to get round-dependent behaviour, since a bare
    context reports round 1 and therefore no risk tolerance.
    """
    if ctx is None:
        roster = [board.by_key[k] for k in roster_keys if k in board.by_key]
        ctx = dataclasses.replace(
            edge.build_context(board, roster),
            depth_factor=_depth_factors(roster, cfg.roster_positions, cfg),
        )
    return _combine(need(candidate, roster_keys, board, cfg), candidate, ctx, cfg)


@dataclass(frozen=True)
class Recommendation:
    player: BoardPlayer
    value: float
    need: float
    vor: float
    survival: float | None
    flags: tuple[str, ...]
    reason: str

    # Edge signals, surfaced so the UI can show and sort on them. Defaulted
    # because they are all exactly zero when the edge weights are.
    upside: float = 0.0  # 0..1, researched breakout potential
    volatility: float = 0.0  # 0..1, cross-site ADP disagreement
    arbitrage: float = 0.0  # picks of surplus vs. our rank
    scoring_edge: float = 0.0  # league-scored points minus consensus points


def _starters_for_position(position: str, roster_positions: dict[str, int]) -> int:
    dummy = Player(player_id=-1, name="", eligible_positions=[position])
    return sum(1 for slot in starting_slots(roster_positions) if slot_accepts(slot, dummy))


def _candidate_flags(
    candidate: BoardPlayer,
    bye_counts: dict[tuple[str, int], int],
    starters: dict[str, int],
) -> tuple[str, ...]:
    flags = []
    if candidate.bye_week is not None:
        collision = bye_counts.get((candidate.position, candidate.bye_week), 0) + 1
        cap = starters.get(candidate.position, 0)
        if cap and collision > cap:
            flags.append(f"bye week {candidate.bye_week}: {collision} {candidate.position}s collide")
    return tuple(flags)


def _reason(
    candidate: BoardPlayer,
    need_val: float,
    survival_pct: float | None,
    remaining_in_tier: int,
    flags: tuple[str, ...],
    edge_reasons: Sequence[str] = (),
) -> str:
    parts: list[str] = []
    if need_val > 0.0:
        # The position is named because `need` is position-specific by
        # construction (marginal lineup points vs. a replacement at THAT
        # position) -- "fills a need" on its own leaves the reader to infer
        # which hole from the Pos column two tables away.
        parts.append(f"fills a need ({candidate.position} +{need_val:.1f})")
    if remaining_in_tier <= 1:
        parts.append(f"last of tier {candidate.tier}")
    if survival_pct is not None and survival_pct < 0.35:
        parts.append(f"{survival_pct * 100:.0f}% to survive to your next pick")
    # Edge reasons come after the structural ones: "fills a need" is why the
    # player is ranked here at all, and the intel note is why they are
    # interesting. Both matter, in that order.
    parts.extend(edge_reasons)
    parts.extend(flags)
    if not parts:
        parts.append(f"best value available (+{max(0.0, need_val):.1f})")
    return "; ".join(parts)


def recommend(
    state: DraftState,
    cfg: Config,
    limit: int = 12,
    position: str | None = None,
    kalshi_scores: dict[str, float] | None = None,
) -> list[Recommendation]:
    """Rank every remaining player on the board by `value()`.

    Scans the entire board rather than a shortlist — measured at tens of
    milliseconds against a 500+ player pool, well inside the pick clock, so
    there is no need to approximate.

    `kalshi_scores` (`{board_key: 0..1}`, see
    `ffbot.markets.kalshi_nfl.draft_signal`) is a CALLER-supplied,
    already-fetched value, never fetched in here — a live market read is
    session-scoped, fetched once at draft start (see `scripts/draft.py`'s
    `build_state`), not on every render the way this function is called.
    `None`/empty is the default and an exact no-op regardless of
    `cfg.draft.kalshi_weight`.
    """
    board = state.board
    roster = state.my_roster()
    roster_keys = [bp.key for bp in roster]
    taken = state.taken_keys()

    candidates = [bp for bp in board.players if bp.key not in taken]
    if position is not None:
        candidates = [bp for bp in candidates if bp.position == position]
    else:
        have: dict[str, int] = defaultdict(int)
        for bp in roster:
            have[bp.position] += 1

        # Positions you already have enough of drop out entirely. See
        # `DraftConfig.position_caps` for why a cap rather than a discount.
        if cfg.draft.position_caps:
            capped = {
                pos for pos, cap in cfg.draft.position_caps.items() if have[pos] >= cap
            }
            candidates = [bp for bp in candidates if bp.position not in capped]

        # Kickers and defenses are held back until the end, the same way the
        # pre-draft export buries them.
        #
        # VOR genuinely rates the best kicker above a marginal receiver — the
        # gap from K1 to the 12th kicker is real arithmetic. It is still the
        # wrong call, because kicker and defense scoring barely persists from
        # one season to the next, so a projection implies a reliability that
        # does not exist. Without this the optimizer spends a 5th-round pick
        # on a kicker.
        #
        # Suppressed rather than excluded: `p K` still lists them, for the
        # rounds where you actually want one.
        current_round = round_and_slot(state.current_pick(), state.num_teams, state.order)[0]
        if current_round < max(1, state.rounds - 1):
            deferred = set(cfg.draft.export_defer_positions)
            candidates = [bp for bp in candidates if bp.position not in deferred]

        # Forced fill: once I have only as many picks left as dedicated
        # starting positions with nobody rostered, every remaining pick must
        # fill one. This is exact, not strategic — an empty K slot scores a
        # literal zero every week, and no bench player at an already-covered
        # position outweighs that. Without it, the deficit-urgency boost on a
        # still-short RB/WR happily out-bids the kicker forever.
        mono_slots = {
            slot
            for slot, count in state.roster_positions.items()
            if count > 0 and "/" not in slot and slot != BENCH and slot not in IR_SLOTS
        }
        missing = {slot for slot in mono_slots if have[slot] == 0}
        my_remaining = len([p for p in state.my_picks() if p >= state.current_pick()])
        if missing and 0 < my_remaining <= len(missing):
            forced = [bp for bp in candidates if bp.position in missing]
            if forced:
                candidates = forced

    base = _season_score(board, roster_keys, None, cfg)
    repl_marginal: dict[str, float] = {}
    for pos in {bp.position for bp in candidates}:
        repl_points = board.replacement.get(pos)
        if repl_points is None:
            continue
        repl = _replacement_board_player(pos, repl_points)
        repl_marginal[pos] = _season_score(board, roster_keys, repl, cfg) - base

    starters = {pos: _starters_for_position(pos, state.roster_positions) for pos in {bp.position for bp in roster}}
    bye_counts: dict[tuple[str, int], int] = defaultdict(int)
    for bp in roster:
        if bp.bye_week is not None:
            bye_counts[(bp.position, bp.bye_week)] += 1

    remaining_in_tier: dict[tuple[str, int], int] = defaultdict(int)
    for bp in candidates:
        remaining_in_tier[(bp.position, bp.tier)] += 1

    current_pick = state.current_pick()
    # Strictly after the current pick -- see `next_my_pick_after` for why
    # `next_my_pick()` is the wrong target here (it silently reports 100%
    # survival for everyone on every pick of my own).
    survival_to = state.next_my_pick_after(current_pick)

    # Needs are computed first, because the edge bonus is sized as a fraction
    # of the spread between the realistic options at *this* pick — see
    # `edge.decision_scale`. Sizing it any other way lets a flat bonus swamp a
    # late-round decision where the real gaps are only a point or two.
    needs: dict[str, float] = {}
    for bp in candidates:
        marginal_x = _season_score(board, roster_keys, bp, cfg) - base
        needs[bp.key] = marginal_x - repl_marginal.get(bp.position, 0.0)

    depth_factor = _depth_factors(roster, state.roster_positions, cfg)
    scored = [
        (
            bp.key,
            needs[bp.key]
            + cfg.draft.depth_weight * max(0.0, bp.vor) * depth_factor.get(bp.position, 1.0),
        )
        for bp in candidates
    ]

    # Deficit urgency per position: how short of the configured target we are,
    # against the picks I have left to fix it. Early this is near-equal across
    # positions (no distortion); it grows only for whichever position falls
    # behind as the draft runs.
    balance: dict[str, float] = {}
    if cfg.draft.position_targets:
        have: dict[str, int] = defaultdict(int)
        for bp in roster:
            have[bp.position] += 1
        my_remaining = len([p for p in state.my_picks() if p >= current_pick])
        for pos, target in cfg.draft.position_targets.items():
            deficit = max(0, target - have[pos])
            if deficit:
                balance[pos] = min(1.0, deficit / max(1, my_remaining))

    # Bye pressure, memoized per (position, bye week) pair actually present
    # among the candidates rather than per candidate -- see
    # `_bye_pressure`. Skipped entirely at the 0.0 default, both for the
    # exact no-op guarantee and to avoid the extra optimizer calls.
    bye_pressure: dict[tuple[str, int], float] = {}
    if cfg.draft.bye_collision_weight != 0.0:
        for bp in candidates:
            if bp.bye_week is None:
                continue
            key = (bp.position, bp.bye_week)
            if key in bye_pressure:
                continue
            bye_pressure[key] = _bye_pressure(
                roster, state.roster_positions, bp.position, bp.bye_week, cfg
            )

    # Same no-op-skip reasoning for the opponent-demand term.
    position_demand = demand_ahead(state, cfg) if cfg.draft.block_weight != 0.0 else {}

    # Built once for the whole scan: the volatility ranking is board-wide and
    # the roster/round facts are identical for every candidate.
    round_ = round_and_slot(current_pick, state.num_teams, state.order)[0]
    ctx = dataclasses.replace(
        edge.build_context(board, roster, round_, scored),
        depth_factor=depth_factor,
        balance=balance,
        bye_pressure=bye_pressure,
        position_demand=position_demand,
        kalshi=kalshi_scores or {},
    )

    recs: list[Recommendation] = []
    for bp in candidates:
        need_val = needs[bp.key]
        val = _combine(need_val, bp, ctx, cfg)

        surv = None
        if bp.adp is not None and survival_to is not None:
            sigma = sigma_for(bp.adp, bp.adp_stdev, cfg.draft)
            surv = survival(bp.adp, sigma, current_pick, survival_to)

        starters_for_pos = {**starters, bp.position: _starters_for_position(bp.position, state.roster_positions)}
        flags = _candidate_flags(bp, bye_counts, starters_for_pos)
        edge_reasons = edge.reasons(bp, ctx, cfg)
        reason = _reason(
            bp, need_val, surv, remaining_in_tier[(bp.position, bp.tier)], flags, edge_reasons
        )

        recs.append(
            Recommendation(
                player=bp, value=val, need=need_val, vor=bp.vor,
                survival=surv, flags=flags, reason=reason,
                upside=edge.upside_score(bp),
                volatility=ctx.volatility.get(bp.key, 0.0),
                arbitrage=edge.arbitrage_picks(bp),
                scoring_edge=edge.scoring_edge(bp),
            )
        )

    recs.sort(key=lambda r: (-r.value, r.player.rank))
    return recs[:limit]


# --- Alerts ------------------------------------------------------------


def _alert_positions(state: DraftState, cfg: Config) -> set[str]:
    """The positions an alert could still plausibly change my mind about.

    An alert only earns a line if it could move my next pick, so every
    position I am not going to draft anyway drops out. All three rules reuse
    what `recommend()` already enforces rather than inventing a second
    opinion about roster construction:

    - deferred this round (K/DEF before the last two, see
      `export_defer_positions`) — `recommend()` doesn't even list them, so
      alerting about their scarcity is pure noise;
    - at its `position_caps` ceiling — not a candidate at all;
    - already as deep as I want: `position_targets` when configured, and
      otherwise one past the starting slots that accept the position
      (starters plus a backup). Without this the run alert keeps shouting
      about quarterbacks long after I've taken two.
    """
    have: dict[str, int] = defaultdict(int)
    for bp in state.my_roster():
        have[bp.position] += 1

    current_round = round_and_slot(state.current_pick(), state.num_teams, state.order)[0]
    deferred = (
        set(cfg.draft.export_defer_positions)
        if current_round < max(1, state.rounds - 1)
        else set()
    )

    live: set[str] = set()
    for pos in {bp.position for bp in state.board.players}:
        if pos in deferred:
            continue
        cap = cfg.draft.position_caps.get(pos)
        if cap is not None and have[pos] >= cap:
            continue
        target = cfg.draft.position_targets.get(pos)
        if target is None:
            target = _starters_for_position(pos, state.roster_positions) + 1
        if have[pos] >= target:
            continue
        live.add(pos)
    return live


def alerts(state: DraftState, cfg: Config) -> list[str]:
    """Situational alerts: what should change my next pick, and nothing else.

    Three kinds, deliberately few and all filtered to `_alert_positions`:

    - `RUN` — the room is hammering a position right now (model-free, just
      the pick log).
    - `WAIT` — what passing on a position until my next turn costs, in the
      same projected points every other number in the UI is denominated in.
    - `BYE` — my own roster already has a bye-week hole (exact, via the
      optimizer).

    The whole list is capped at `cfg.draft.alert_limit`, because a panel
    that always has something in it is a panel nobody reads.

    Two tier-based alerts used to live here and were deliberately removed.
    `CLIFF` (<=2 players left in a position's best remaining tier) fired at
    nearly every position on nearly every pick — a "best remaining tier" is
    thin by construction, so its own emptiness is not news — and `ROOM`
    (chance any of that tier's top 3 survives) asked the scarcity question
    through an arbitrary top-3-of-a-tier window. `WAIT` answers that same
    question directly, in points, with no tier boundary involved: tiers are
    a board-building device (see `board.assign_tiers`), not a decision.
    """
    board = state.board
    taken = state.taken_keys()
    live = _alert_positions(state, cfg)

    available: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in board.players:
        if bp.key not in taken and bp.position in live:
            available[bp.position].append(bp)

    runs: list[str] = []
    waits: list[tuple[float, str]] = []
    byes: list[str] = []

    # RUN: opponents only. My own picks were counted here once, which meant
    # a roster I had deliberately built could trip an alert about my own
    # strategy -- a run is by definition what the *other* teams are doing.
    recent = [p for p in state.picks if p.key is not None and not p.mine][-cfg.draft.run_window:]
    counts: dict[str, int] = defaultdict(int)
    for p in recent:
        bp = board.by_key.get(p.key)
        if bp is not None:
            counts[bp.position] += 1
    for pos, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= cfg.draft.run_threshold and pos in live:
            runs.append(f"RUN: {n} of the last {len(recent)} opponent picks were {pos}")

    # WAIT: the best player available at a position now, against the best one
    # likely to still be there at my next turn. That difference IS the cost
    # of waiting -- no tier, no rank, just the points I give up by taking
    # something else first. `next_my_pick_after` (not `next_my_pick`) is the
    # target for the same reason `recommend()` uses it.
    current_pick = state.current_pick()
    target = state.next_my_pick_after(current_pick)
    if target is not None:
        for pos, plist in available.items():
            # Both sides need an ADP or the comparison is not like-for-like:
            # a player with no ADP can never clear the survival floor, so
            # including him on the "now" side alone would invent a gap.
            priced = [bp for bp in plist if bp.adp is not None]
            if len(priced) < 2:
                continue
            best_now = max(priced, key=lambda bp: bp.points)
            likely = [
                bp
                for bp in priced
                if survival(bp.adp, sigma_for(bp.adp, bp.adp_stdev, cfg.draft), current_pick, target)
                >= cfg.draft.alert_survival_floor
            ]
            if not likely:
                continue
            best_later = max(likely, key=lambda bp: bp.points)
            drop = best_now.points - best_later.points
            if drop >= cfg.draft.alert_gap_points:
                waits.append(
                    (
                        drop,
                        f"WAIT: {pos} drops {drop:.0f} pts by your pick {target} — "
                        f"{best_now.name} now vs. {best_later.name} then",
                    )
                )
    # Capped independently of `alert_limit` so a board where every position
    # is thinning at once can't crowd the bye holes off the end of the list.
    waits.sort(key=lambda w: -w[0])
    del waits[3:]

    # BYE: exact, via the same optimizer used for the lineup path -- for
    # every distinct bye week on my roster, does that week leave *more*
    # slots unfillable than an incomplete roster already does? Comparing
    # against the week=None baseline (rather than raw unfilled_slots) is
    # what keeps this quiet early in the draft, when almost every slot is
    # unfilled simply because the roster is still mostly empty.
    roster = state.my_roster()
    bye_weeks = {bp.bye_week for bp in roster if bp.bye_week is not None}
    if roster and bye_weeks:
        roster_players = [to_player(bp, uid) for uid, bp in enumerate(roster, start=1)]
        baseline = Counter(optimize(roster_players, state.roster_positions, None, cfg).unfilled_slots)
        for week in sorted(bye_weeks):
            plan = optimize(roster_players, state.roster_positions, week, cfg)
            extra = Counter(plan.unfilled_slots) - baseline
            if extra:
                caused = sorted(extra.elements())
                byes.append(f"BYE: week {week} leaves {','.join(caused)} unfilled that would otherwise be covered")

    out = runs + [text for _, text in waits] + byes
    return out[: max(0, cfg.draft.alert_limit)]


def needs_between(state: DraftState) -> dict[str, int]:
    """Positions other teams drafted since my last pick, up to (not including) now.

    An exact, model-free signal about what is being taken right as I come
    on the clock — "the teams since your last turn took 5 RB, 1 TE."
    """
    current = state.current_pick()
    prior_mine = [p for p in state.my_picks() if p < current]
    start = max(prior_mine) if prior_mine else 0

    counts: dict[str, int] = defaultdict(int)
    for p in state.picks:
        if start < p.number < current and not p.mine and p.key is not None:
            bp = state.board.by_key.get(p.key)
            if bp is not None:
                counts[bp.position] += 1
    return dict(counts)
