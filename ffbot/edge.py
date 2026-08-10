"""Contrarian valuation terms layered on top of `draft.need`.

`need` answers "how many more points does my starting lineup score?" — the right
question for building a competent roster, and the wrong one for building an
*interesting* one. It ranks by consensus projections, so it reproduces consensus
opinion, which is exactly what everyone else in the league is drafting from.

The terms here are the ways to beat consensus that don't require being better at
projecting football than the projections are:

- **Round-ramped risk appetite.** A steady floor early is worth more than upside,
  because early picks are most of your roster's points. By the late rounds the
  reverse holds: a replacement-level player is worth roughly nothing, so a 10%
  chance at a league-winner beats a certain bench body. `need` has no concept of
  this at all — it values a player the same in round 1 and round 14.
- **Market disagreement** (`adp_spread`) as a free volatility signal.
- **Stacking**, which deliberately *increases* variance by correlating outcomes.
- **ADP arbitrage**, the gap between where your board ranks a player and where the
  market actually drafts them.

Every weight defaults to 0.0, so with a stock `Config` this module is an exact
no-op and the optimizer behaves precisely as it did before. The aggressive values
live in config.yml, which makes "how contrarian is it" one number the user can
turn mid-draft rather than a code change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from .board import Board, BoardPlayer
from .config import Config

# Kickers and defenses are excluded from every edge term, because every one of
# them misreads this pair of positions:
#
# - Volatility: they top the raw `adp_spread` list for an uninteresting reason,
#   namely that nobody agrees on them because nobody cares.
# - Arbitrage: VOR is compressed across K/DEF (only one starts, and the pool is
#   barely deeper than the league), so our board ranks a mediocre defense near
#   100th while the market correctly waits until pick 150. That reads as a
#   60-pick bargain and is really just an artifact of comparing a within-position
#   VOR against a whole-board rank. Left in, it drafts six defenses.
# - Upside and stacking: a kicker is never a league-winner, which is the only
#   thing this layer exists to find.
EDGE_EXCLUDED_POSITIONS = frozenset({"K", "DEF"})

PASS_CATCHERS = frozenset({"WR", "TE"})

# A player the market takes far earlier or later than our board is informative;
# beyond this many picks it is far more likely to be a data problem (a stale ADP,
# a name that merged badly) than a real edge, so the signal is capped.
_MAX_ARBITRAGE_PICKS = 60.0

# Points, not picks: caps `scoring_edge` so a single mis-parsed stat line
# (or a position where FantasyPros' own consensus and this league's rules
# happen to diverge wildly, like an uncapped points-allowed swing) cannot
# dominate the ranking on its own.
_MAX_SCORING_EDGE = 30.0


def risk_ramp(round_: int, cfg: Config) -> float:
    """How much variance to tolerate in `round_`, from 0.0 (none) to 1.0 (full).

    Flat 0 through `risk_ramp_start`, linear to 1.0 at `risk_ramp_full`, flat
    after. This is what makes the same player a bad pick in round 1 and a good
    one in round 8.
    """
    start = cfg.draft.risk_ramp_start
    full = cfg.draft.risk_ramp_full
    if round_ <= start:
        return 0.0
    if full <= start or round_ >= full:
        return 1.0
    return (round_ - start) / (full - start)


def volatility_map(board: Board) -> dict[str, float]:
    """Board key -> 0..1 volatility, from cross-site ADP disagreement.

    Ranked within position rather than min-max scaled, so one wildly disputed
    player cannot flatten everyone else, and so a WR's uncertainty is judged
    against other WRs rather than against the whole board. Players with no
    `adp_spread` (roughly the deep half of the board, where too few sites
    publish an ADP to compute one) are simply absent and score 0.
    """
    by_pos: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in board.players:
        if bp.position in EDGE_EXCLUDED_POSITIONS or bp.adp_spread is None:
            continue
        by_pos[bp.position].append(bp)

    out: dict[str, float] = {}
    for plist in by_pos.values():
        ordered = sorted(plist, key=lambda b: (b.adp_spread, b.name))
        n = len(ordered)
        for i, bp in enumerate(ordered):
            out[bp.key] = i / (n - 1) if n > 1 else 0.0
    return out


# How many of the best available players count as "the decision" when sizing
# the bonus. Roughly the number you would genuinely consider at any one pick.
_DECISION_POOL = 25

# Smallest spread a pick is ever treated as having, in projected season points.
# Keeps the edge layer alive once the base values collapse to a flat field of
# zeros — see `decision_scale`.
_MIN_DECISION_SCALE = 1.0


def _decision_pool(scored: Sequence[tuple[str, float]]) -> frozenset[str]:
    """Keys of the players plausibly in play at this pick.

    Defined relative to what is actually available rather than by an absolute
    cutoff. An absolute one (say "at or above replacement") looks reasonable and
    silently switches the whole layer off after round 7, because by then every
    remaining player is below replacement — which is exactly when upside is
    supposed to matter most.
    """
    ranked = sorted(scored, key=lambda kv: -kv[1])[:_DECISION_POOL]
    return frozenset(key for key, _ in ranked)


def decision_scale(base_values: Sequence[float]) -> float:
    """How much value is actually at stake in this pick.

    The spread between the best available option and the ~25th best. This is
    the unit the edge weights are expressed in, and it has to be measured per
    pick rather than fixed, because `need` collapses toward zero as a roster
    fills: by round 8 the real gap between the top options is a couple of
    points, and a bonus calibrated against round-1 numbers (where the gap is
    over a hundred) would stop being a tiebreak and simply become the ranking.

    That is not a hypothetical — a flat bonus drafts seven tight ends.

    Floored at `_MIN_DECISION_SCALE` rather than allowed to reach zero. Late in
    a draft every remaining player is below replacement, and the depth term
    clamps at `max(0, vor)`, so the base values collapse to a field of exact
    zeros. A zero scale would then silence the edge layer completely, in the
    rounds where it is the only thing left with an opinion — when nothing else
    separates two players, upside is precisely what should break the tie.
    """
    if not base_values:
        return 0.0
    top = sorted(base_values, reverse=True)[:_DECISION_POOL]
    spread = abs(top[0]) if len(top) < 2 else max(0.0, top[0] - top[-1])
    return max(spread, _MIN_DECISION_SCALE)


@dataclass(frozen=True)
class EdgeContext:
    """Per-pick state the edge terms need, computed once rather than per candidate.

    `recommend()` scans the whole board every pick, so anything derivable from
    the roster or the round belongs here instead of inside the candidate loop.
    """

    round_: int = 1
    qb_teams: frozenset[str] = frozenset()
    pass_catcher_teams: frozenset[str] = frozenset()
    volatility: dict[str, float] = field(default_factory=dict)
    scale: float = 0.0
    contenders: frozenset[str] = frozenset()
    # Per-position multiplier on bench depth; see `draft._depth_factors`.
    # Empty means no decay, which is the behaviour with a stock config.
    depth_factor: dict[str, float] = field(default_factory=dict)
    # Position -> deficit urgency in [0, 1]: how short of the configured
    # roster target we are at that position, relative to the picks I have
    # left to fix it. Computed in `recommend()`; empty = no targets = no-op.
    balance: dict[str, float] = field(default_factory=dict)
    # How many ROSTERED players (any position, blank team excluded -- same
    # guard `stack_match` uses) already share the candidate's team. See
    # `team_concentration_penalty`.
    team_counts: dict[str, int] = field(default_factory=dict)
    # Best rostered QB's / pass-catcher's VOR per team, for magnitude-aware
    # stacking. See `stack_magnitude`.
    qb_vor: dict[str, float] = field(default_factory=dict)
    pass_catcher_best_vor: dict[str, float] = field(default_factory=dict)

    def ramp(self, cfg: Config) -> float:
        return risk_ramp(self.round_, cfg)


def build_context(
    board: Board,
    roster: Sequence[BoardPlayer],
    round_: int = 1,
    scored: Sequence[tuple[str, float]] | None = None,
) -> EdgeContext:
    """Assemble the per-pick context. Cheap enough to call once per render.

    `scored` is `(board key, pre-edge score)` for everyone still available; it
    sizes the bonus and decides who is in contention. When omitted — a
    standalone `value()` call with no live draft — the board's own VOR stands
    in, which is exactly right on an empty roster, since `need` equals VOR there.
    """
    qb_teams = {bp.team for bp in roster if bp.position == "QB" and bp.team}
    pc_teams = {bp.team for bp in roster if bp.position in PASS_CATCHERS and bp.team}

    team_counts: dict[str, int] = defaultdict(int)
    qb_vor: dict[str, float] = {}
    pc_best_vor: dict[str, float] = {}
    for bp in roster:
        if not bp.team:
            continue
        team_counts[bp.team] += 1
        if bp.position == "QB":
            qb_vor[bp.team] = max(qb_vor.get(bp.team, bp.vor), bp.vor)
        elif bp.position in PASS_CATCHERS:
            pc_best_vor[bp.team] = max(pc_best_vor.get(bp.team, bp.vor), bp.vor)

    if scored is None:
        scored = [(bp.key, bp.vor) for bp in board.players]
    return EdgeContext(
        round_=round_,
        qb_teams=frozenset(qb_teams),
        pass_catcher_teams=frozenset(pc_teams),
        volatility=volatility_map(board),
        scale=decision_scale([v for _, v in scored]),
        contenders=_decision_pool(scored),
        team_counts=dict(team_counts),
        qb_vor=qb_vor,
        pass_catcher_best_vor=pc_best_vor,
    )


def stack_match(candidate: BoardPlayer, ctx: EdgeContext) -> bool:
    """True when drafting `candidate` completes a QB / pass-catcher pairing.

    Stacking is a variance play, not a points play: when the quarterback throws a
    touchdown to his own receiver, a stacked roster scores it twice. It raises
    ceiling and floor together in the same direction, which is the point.
    """
    if not candidate.team:
        return False
    if candidate.position == "QB":
        return candidate.team in ctx.pass_catcher_teams
    if candidate.position in PASS_CATCHERS:
        return candidate.team in ctx.qb_teams
    return False


# Concentration penalty stops compounding past this many already-rostered
# teammates -- a real but bounded portfolio risk, not something that should
# make a 5th same-team bench piece read as catastrophic.
_MAX_CONCENTRATION_COUNT = 4

# `stack_magnitude`'s blend range: even the weakest partner still stacks at
# half strength (correlation doesn't vanish, it just matters less), and an
# elite partner is capped so this one term can never dominate the whole bonus.
_MIN_STACK_MAGNITUDE = 0.5
_MAX_STACK_MAGNITUDE = 2.0


def team_concentration_penalty(candidate: BoardPlayer, ctx: EdgeContext, cfg: Config) -> float:
    """Fraction-of-scale penalty for how many of `candidate`'s own NFL
    teammates are already rostered — a QB injury, an offensive-line
    collapse, or a coordinator change hits every same-team player at once,
    a portfolio risk no per-player projection can see. Unramped, like
    `risk_weight`: concentration risk doesn't care what round it is.

    Deliberately separate from `stack_bonus` rather than unified into one
    signed term — an existing `stack_bonus` config (config.yml ships
    `0.20`) must never be silently reinterpreted by this addition. Blank
    team (DEF, before `names.defense_key` resolution) never concentrates,
    the same guard `stack_match` uses. 0.0 whenever
    `team_concentration_weight` is 0.0 (the default) or the candidate has
    no rostered teammates yet.
    """
    if not candidate.team:
        return 0.0
    count = ctx.team_counts.get(candidate.team, 0)
    if count == 0:
        return 0.0
    return cfg.draft.team_concentration_weight * min(count, _MAX_CONCENTRATION_COUNT)


def stack_magnitude(candidate: BoardPlayer, ctx: EdgeContext, cfg: Config) -> float:
    """Multiplier on `stack_bonus` from the stacked partner's own strength.

    `stack_magnitude_weight == 0.0` (the default) returns EXACTLY 1.0 —
    every existing config, including config.yml's `stack_bonus: 0.20`,
    stays bit-identical to before this function existed. Above 0.0, blends
    from flat (1.0) toward scaling by the partner's VOR relative to this
    pick's own decision scale — a stack with a QB1 correlates real
    touchdowns far more than a stack with a backup, and a second same-team
    WR stacks with whichever partner is already rostered, not with itself.
    Normalized against `ctx.scale` rather than a board-wide constant, so
    the same partner reads as more significant in a tight decision than a
    blowout one.
    """
    if cfg.draft.stack_magnitude_weight == 0.0:
        return 1.0
    if candidate.position == "QB":
        partner_vor = ctx.pass_catcher_best_vor.get(candidate.team)
    else:
        partner_vor = ctx.qb_vor.get(candidate.team)
    if partner_vor is None or ctx.scale <= 0:
        return 1.0

    raw = max(_MIN_STACK_MAGNITUDE, min(_MAX_STACK_MAGNITUDE, partner_vor / ctx.scale))
    return 1.0 + cfg.draft.stack_magnitude_weight * (raw - 1.0)


def scoring_edge(candidate: BoardPlayer) -> float:
    """League-scored points minus consensus FantasyPros points, clamped to
    `_MAX_SCORING_EDGE`.

    Positive means this league's rules pay the player more than the
    consensus PPR market that sets ADP does — and unlike `arbitrage_picks`,
    the market has no way to correct this, because it isn't playing this
    league. Exactly 0.0 whenever no `league.yml` is configured: `points ==
    points_fp` then by construction (see `board.apply_league_scoring`).
    """
    delta = candidate.points - candidate.points_fp
    return max(-_MAX_SCORING_EDGE, min(_MAX_SCORING_EDGE, delta))


def arbitrage_picks(candidate: BoardPlayer) -> float:
    """How many picks later than our board's rank the market drafts this player.

    Positive means the market undervalues them relative to our board — we can
    wait, or get a bargain. Negative means the market reaches for them. Zero when
    no ADP is available, which is about 15% of the board.
    """
    if candidate.adp is None:
        return 0.0
    delta = candidate.adp - candidate.rank
    return max(-_MAX_ARBITRAGE_PICKS, min(_MAX_ARBITRAGE_PICKS, delta))


def upside_score(candidate: BoardPlayer) -> float:
    """Researched breakout potential as 0..1, or 0.0 when no intel exists."""
    if candidate.upside is None:
        return 0.0
    return max(0.0, min(100.0, candidate.upside)) / 100.0


def risk_score(candidate: BoardPlayer) -> float:
    """Researched availability risk as 0..1, or 0.0 when no intel exists.

    The one intel signal that *subtracts* — reserved for verifiable
    availability facts (suspension, PUP, holdout, no-timetable injury), never
    opinion. See the ffbot.intel module docstring for the line.
    """
    if candidate.availability_risk is None:
        return 0.0
    return max(0.0, min(100.0, candidate.availability_risk)) / 100.0


def is_contender(candidate: BoardPlayer, ctx: EdgeContext) -> bool:
    """Whether this player is a real option worth applying any bonus to.

    Membership of the current decision pool — the best ~25 available on base
    value. A player far outside it is not someone "high upside" argues for; it
    argues for a *better* player with the same property. Without the gate the
    bonus promotes a receiver 146 points below replacement, because deep bench
    players have the widest ADP disagreement (nobody knows or cares where they
    go) and that reads as volatility.

    Relative rather than absolute on purpose: see `_decision_pool`.

    Kickers and defenses are excluded outright — see EDGE_EXCLUDED_POSITIONS.
    """
    if candidate.position in EDGE_EXCLUDED_POSITIONS:
        return False
    if not ctx.contenders:
        return True
    return candidate.key in ctx.contenders


def bonus(candidate: BoardPlayer, ctx: EdgeContext, cfg: Config) -> float:
    """Total edge adjustment for one candidate, in projected-points units.

    Weights are fractions of `ctx.scale` — the value actually at stake in this
    pick — rather than absolute point totals, so the same config stays
    proportionate in round 1 and round 14.

    Exactly 0.0 under a stock `Config`: every weight defaults to zero.
    """
    d = cfg.draft

    # Roster balance sits outside the contender gate on purpose: late in a
    # draft the position you are short of may have nobody left in the top-25
    # pool, and that is precisely when the deficit most needs to pull picks
    # toward it. Uniform within a position, so it reorders positions against
    # each other but never players within one. Not risk-ramped — needing a
    # fifth RB is roster construction, not a variance bet.
    total = 0.0
    if candidate.position not in EDGE_EXCLUDED_POSITIONS:
        total += d.balance_weight * ctx.balance.get(candidate.position, 0.0) * ctx.scale

    if not is_contender(candidate, ctx):
        return total

    ramp = ctx.ramp(cfg)

    fraction = 0.0
    fraction += d.upside_weight * ramp * upside_score(candidate)
    # Unramped, unlike upside: variance is a choice, availability is a fact,
    # and the fact costs the same in every round.
    fraction -= d.risk_weight * risk_score(candidate)
    # Unramped, like risk: concentration is a portfolio fact about the
    # roster you'd end up with, not a variance bet that only makes sense
    # late.
    fraction -= team_concentration_penalty(candidate, ctx, cfg)
    fraction += d.volatility_weight * ramp * ctx.volatility.get(candidate.key, 0.0)
    fraction += d.arbitrage_weight * (arbitrage_picks(candidate) / _MAX_ARBITRAGE_PICKS)
    # Unramped, like arbitrage_weight: this is a market-mispricing signal,
    # not a variance play, so it is not something to lean into only late.
    fraction += d.scoring_arbitrage_weight * (scoring_edge(candidate) / _MAX_SCORING_EDGE)
    if stack_match(candidate, ctx):
        # Ramped like the other variance terms — a stack deliberately
        # correlates outcomes, which is exactly the risk the early rounds are
        # supposed to refuse. Unramped, this reaches for a same-team QB in
        # round 2. `stack_magnitude` is an exact 1.0 (flat) multiplier under
        # the default `stack_magnitude_weight == 0.0`.
        fraction += d.stack_bonus * ramp * stack_magnitude(candidate, ctx, cfg)
    return total + fraction * ctx.scale


def reasons(candidate: BoardPlayer, ctx: EdgeContext, cfg: Config) -> list[str]:
    """Plain-English explanations for whatever the edge terms did.

    The user does not follow football, so a number moving is not an explanation.
    Only reasons that actually applied are returned, and only when their weight
    is non-zero — a disabled term must stay invisible.
    """
    d = cfg.draft
    ramp = ctx.ramp(cfg)
    out: list[str] = []

    # The note is researched fact about the player and stands on its own, so it
    # shows even for someone the bonus does not apply to.
    if candidate.intel_note:
        out.append(candidate.intel_note)

    # Only announced once it is genuinely urgent (deficit >= half the picks
    # remaining) — early on every position is "needed" and saying so on every
    # row would be noise.
    if d.balance_weight and ctx.balance.get(candidate.position, 0.0) >= 0.5:
        out.append(f"running out of picks to fill {candidate.position}")

    if not is_contender(candidate, ctx):
        return out

    if d.stack_bonus and ramp > 0 and stack_match(candidate, ctx):
        other = "QB" if candidate.position in PASS_CATCHERS else "pass catcher"
        out.append(f"stacks with your {candidate.team} {other}")

    if d.team_concentration_weight and candidate.team and ctx.team_counts.get(candidate.team, 0) > 0:
        n = ctx.team_counts[candidate.team]
        out.append(f"already have {n} {candidate.team} player{'s' if n != 1 else ''} — concentration risk")

    if d.volatility_weight and ramp > 0:
        vol = ctx.volatility.get(candidate.key, 0.0)
        if vol >= 0.85:
            out.append("wide disagreement on him — boom or bust")

    if d.arbitrage_weight:
        surplus = arbitrage_picks(candidate)
        if surplus >= 20:
            out.append(f"market lets him fall ~{surplus:.0f} picks past our rank")

    if d.scoring_arbitrage_weight:
        edge_pts = scoring_edge(candidate)
        if abs(edge_pts) >= 5:
            direction = "pays" if edge_pts > 0 else "docks"
            out.append(f"your league {direction} him {edge_pts:+.0f} pts vs. consensus")

    return out
