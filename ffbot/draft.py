"""Live snake-draft assistant: pick math, survival, and recommendations.

Works entirely offline. `DraftState` tracks the board and the pick log;
`recommend()` scans the whole remaining player pool every time (cheap enough
that no shortlist or approximation is needed — see the module-level
benchmarks in tests/test_draft.py) and returns it ranked by `value()`, which
builds on `lineup.optimize()` rather than hand-tuned positional weights.

Every `optimize()` call in this module passes `week=None`. Passing a real
week silently zeroes any player on bye that week, which is correct for the
in-season lineup path and wrong here — draft valuation is a season-long
decision, not a single week's.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Sequence

from .board import Board, BoardPlayer, to_player
from .config import Config, DraftConfig
from .lineup import optimize
from .models import Player, slot_accepts, starting_slots

# --- Snake order -------------------------------------------------------


def pick_number(round_: int, slot: int, num_teams: int) -> int:
    """Overall pick number for `slot` (1-indexed draft position) in `round_`.

    Odd rounds run slot 1..T left to right; even rounds reverse.
    """
    if round_ < 1:
        raise ValueError("round_ must be >= 1")
    if not (1 <= slot <= num_teams):
        raise ValueError(f"slot must be in 1..{num_teams}")
    if round_ % 2 == 1:
        return (round_ - 1) * num_teams + slot
    return (round_ - 1) * num_teams + (num_teams - slot + 1)


def round_and_slot(pick: int, num_teams: int) -> tuple[int, int]:
    """Inverse of `pick_number`: overall pick -> (round, slot)."""
    if pick < 1:
        raise ValueError("pick must be >= 1")
    round_ = (pick - 1) // num_teams + 1
    pos_in_round = pick - (round_ - 1) * num_teams  # 1..num_teams
    if round_ % 2 == 1:
        slot = pos_in_round
    else:
        slot = num_teams - pos_in_round + 1
    return round_, slot


def team_slot_at(pick: int, num_teams: int) -> int:
    """The draft slot (team) on the clock at `pick`."""
    return round_and_slot(pick, num_teams)[1]


def my_pick_numbers(slot: int, num_teams: int, rounds: int) -> list[int]:
    """Every overall pick number belonging to draft slot `slot`."""
    return [pick_number(r, slot, num_teams) for r in range(1, rounds + 1)]


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

    def my_picks(self) -> list[int]:
        """Every overall pick number that belongs to me.

        `my_picks_override` (keepers, traded picks) wins whenever it is
        non-empty; it need not follow the arithmetic snake progression.
        """
        if self.my_picks_override:
            return list(self.my_picks_override)
        return my_pick_numbers(self.my_slot, self.num_teams, self.rounds)

    def current_pick(self) -> int:
        return len(self.picks) + 1

    def next_my_pick(self) -> int | None:
        upcoming = [p for p in self.my_picks() if p >= self.current_pick()]
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


def _replacement_board_player(position: str, points: float) -> BoardPlayer:
    return BoardPlayer(
        key=f"__replacement_{position}__",
        name=f"replacement {position}",
        position=position,
        team="",
        bye_week=None,
        points=points,
        adp=None,
        adp_stdev=None,
        yahoo_id=None,
        tier=0,
        vor=0.0,
        rank=0,
    )


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


def value(candidate: BoardPlayer, roster_keys: Sequence[str], board: Board, cfg: Config) -> float:
    """`need` plus a roster-independent term pricing bench depth.

    `need` alone collapses to 0 once your starters at a position are full —
    correct for roster construction, but it makes every late-round player
    look equally worthless. The depth term (VOR, clamped at 0) is what a
    player who won't crack your starting lineup is still worth: bye cover,
    injury insurance, upside. It cannot change the ranking at either
    extreme (empty or saturated roster) — only in the middle rounds where
    roster construction and depth genuinely trade off.
    """
    return need(candidate, roster_keys, board, cfg) + cfg.draft.depth_weight * max(0.0, candidate.vor)


@dataclass(frozen=True)
class Recommendation:
    player: BoardPlayer
    value: float
    need: float
    vor: float
    survival: float | None
    flags: tuple[str, ...]
    reason: str


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
) -> str:
    parts: list[str] = []
    if need_val > 0.0:
        parts.append(f"fills a need (+{need_val:.1f})")
    if remaining_in_tier <= 1:
        parts.append(f"last of tier {candidate.tier}")
    if survival_pct is not None and survival_pct < 0.35:
        parts.append(f"{survival_pct * 100:.0f}% to survive to your next pick")
    parts.extend(flags)
    if not parts:
        parts.append(f"best value available (+{max(0.0, need_val):.1f})")
    return "; ".join(parts)


def recommend(
    state: DraftState,
    cfg: Config,
    limit: int = 12,
    position: str | None = None,
) -> list[Recommendation]:
    """Rank every remaining player on the board by `value()`.

    Scans the entire board rather than a shortlist — measured at tens of
    milliseconds against a 500+ player pool, well inside the pick clock, so
    there is no need to approximate.
    """
    board = state.board
    roster = state.my_roster()
    roster_keys = [bp.key for bp in roster]
    taken = state.taken_keys()

    candidates = [bp for bp in board.players if bp.key not in taken]
    if position is not None:
        candidates = [bp for bp in candidates if bp.position == position]

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

    next_pick = state.next_my_pick()
    current_pick = state.current_pick()

    recs: list[Recommendation] = []
    for bp in candidates:
        marginal_x = _season_score(board, roster_keys, bp, cfg) - base
        need_val = marginal_x - repl_marginal.get(bp.position, 0.0)
        val = need_val + cfg.draft.depth_weight * max(0.0, bp.vor)

        surv = None
        if bp.adp is not None and next_pick is not None:
            sigma = sigma_for(bp.adp, bp.adp_stdev, cfg.draft)
            surv = survival(bp.adp, sigma, current_pick, next_pick)

        starters_for_pos = {**starters, bp.position: _starters_for_position(bp.position, state.roster_positions)}
        flags = _candidate_flags(bp, bye_counts, starters_for_pos)
        reason = _reason(bp, need_val, surv, remaining_in_tier[(bp.position, bp.tier)], flags)

        recs.append(
            Recommendation(
                player=bp, value=val, need=need_val, vor=bp.vor,
                survival=surv, flags=flags, reason=reason,
            )
        )

    recs.sort(key=lambda r: (-r.value, r.player.rank))
    return recs[:limit]


# --- Alerts ------------------------------------------------------------


def alerts(state: DraftState, cfg: Config) -> list[str]:
    """Situational alerts: positional runs, tier cliffs, bye holes, room."""
    out: list[str] = []
    board = state.board
    taken = state.taken_keys()
    available = [bp for bp in board.players if bp.key not in taken]

    # Positional run: at least run_threshold of the last run_window recorded
    # (non-mine, known) picks share a position.
    recent = [p for p in state.picks if p.key is not None][-cfg.draft.run_window:]
    counts: dict[str, int] = defaultdict(int)
    for p in recent:
        bp = board.by_key.get(p.key)
        if bp is not None:
            counts[bp.position] += 1
    for pos, n in counts.items():
        if n >= cfg.draft.run_threshold:
            out.append(f"RUN: {n} of the last {len(recent)} picks were {pos}")

    # Tier cliff: at most 2 players left in the best remaining tier at a
    # position, with the point drop down to the next tier.
    by_pos: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in available:
        by_pos[bp.position].append(bp)
    for pos, plist in by_pos.items():
        best_tier = min(bp.tier for bp in plist)
        in_tier = [bp for bp in plist if bp.tier == best_tier]
        if len(in_tier) <= 2:
            next_tier = [bp for bp in plist if bp.tier > best_tier]
            if next_tier:
                drop = min(bp.points for bp in in_tier) - max(bp.points for bp in next_tier)
                out.append(
                    f"CLIFF: {len(in_tier)} {pos} left in tier {best_tier} "
                    f"— {drop:.1f} pt drop to tier {best_tier + 1}"
                )

    # Bye hole: exact, via the same optimizer used for the lineup path — for
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
                out.append(f"BYE: week {week} leaves {','.join(caused)} unfilled that would otherwise be covered")

    # Room: chance at least one of the top remaining players at a scarce
    # position survives to my next pick.
    next_pick = state.next_my_pick()
    current_pick = state.current_pick()
    if next_pick is not None and next_pick > current_pick:
        for pos, plist in by_pos.items():
            best_tier = min(bp.tier for bp in plist)
            top = [bp for bp in plist if bp.tier == best_tier][:3]
            probs = []
            for bp in top:
                if bp.adp is None:
                    continue
                sigma = sigma_for(bp.adp, bp.adp_stdev, cfg.draft)
                probs.append(survival(bp.adp, sigma, current_pick, next_pick))
            if probs:
                at_least_one = 1.0 - _prod(1.0 - p for p in probs)
                if at_least_one < 0.5:
                    out.append(
                        f"ROOM: {at_least_one * 100:.0f}% chance a tier-{best_tier} "
                        f"{pos} lasts to pick {next_pick}"
                    )

    return out


def _prod(values):
    result = 1.0
    for v in values:
        result *= v
    return result


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
