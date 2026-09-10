"""Guardrails on irreversible actions.

Lineup changes are freely reversible, so the optimizer runs unsupervised. A
drop is not: once another manager claims the player, they are gone. Since the
whole point of this agent is that it acts without asking, the drop path is
constrained by policy here rather than by a confirmation prompt.

Every rule answers "would I regret this on Monday?" and every threshold is
tunable in config.yml.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import Player


@dataclass
class Verdict:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def can_drop(player: Player, cfg: Config, week: int | None = None) -> Verdict:
    """Whether the agent is permitted to drop this player.

    Checks run cheapest-and-most-absolute first so the reported reason is the
    most meaningful one.
    """
    if player.is_undroppable:
        return Verdict(False, "Yahoo marks this player undroppable")

    protected = {n.strip().lower() for n in cfg.drops.never_drop if n.strip()}
    if player.name.strip().lower() in protected:
        return Verdict(False, "on the never_drop list")

    if (
        player.draft_round is not None
        and player.draft_round <= cfg.drops.protect_draft_rounds
    ):
        return Verdict(
            False,
            f"drafted round {player.draft_round} "
            f"(protected through {cfg.drops.protect_draft_rounds})",
        )

    if (
        player.percent_owned is not None
        and player.percent_owned >= cfg.drops.protect_pct_owned
    ):
        return Verdict(
            False,
            f"owned in {player.percent_owned:.0f}% of leagues "
            f"(protected at {cfg.drops.protect_pct_owned:.0f}%)",
        )

    # A bye week or a Questionable tag makes someone useless this week and
    # valuable the next. Dropping on that basis is the classic self-inflicted
    # wound, so it is refused outright.
    if player.unavailability_is_temporary(week):
        return Verdict(False, "unavailable only temporarily (bye or day-to-day)")

    return Verdict(True, "no protection applies")


def droppable(
    players: list[Player],
    cfg: Config,
    week: int | None = None,
    key=None,
) -> list[Player]:
    """The subset of `players` the agent may drop, worst-first.

    Ordering puts the least valuable candidate at the front so a caller needing
    one roster spot takes the cheapest available.

    `key` overrides the default `percent_owned`-based ordering — pass one
    when a real value signal exists (e.g. `week.hold_margin`). Without live
    Yahoo data `percent_owned` is `None` for every player, which makes the
    default ordering a total tie that degenerates to input order; that is
    the bug `week.waiver_candidates` used to route around by restricting its
    input to `LineupPlan.bench` rather than by supplying a real key. Prefer
    passing `key` over that workaround now that one exists.
    """
    allowed = [p for p in players if can_drop(p, cfg, week).allowed]
    if key is not None:
        allowed.sort(key=key)
    else:
        allowed.sort(key=lambda p: (p.percent_owned if p.percent_owned is not None else 0.0))
    return allowed


def can_deny_claim(my_priority: int, cfg: Config) -> Verdict:
    """Whether a pure-denial claim (`ffbot.denial.denial_candidates` —
    rostering someone you would never start, solely to keep a rival from
    getting him) is allowed to spend this much rolling waiver priority.

    A denial hold has no lineup value of its own to justify the cost the
    way a real add does, so it gets its own, tighter guardrail — the same
    `Verdict`-returning treatment every other irreversible action in this
    module gets, per the invariant that a new irreversible action is never
    a silent allow. `cfg.season.denial_priority_floor` (default 0) is the
    priority number at or below which a denial claim is refused; 0 means no
    restriction.
    """
    floor = cfg.season.denial_priority_floor
    if floor > 0 and my_priority <= floor:
        return Verdict(
            False,
            f"priority {my_priority} is too valuable to spend on a denial-only "
            f"claim (floor {floor})",
        )
    return Verdict(True, "eligible")

# Floor on the predictiveness divisor, so a position whose measured factor is
# absent or pathologically small can't produce an unbounded noise floor.
_MIN_PREDICTIVENESS = 0.1


def can_claim(
    gain: float,
    position: str,
    scale: float,
    cfg: Config,
    predictiveness: dict[str, float] | None = None,
) -> Verdict:
    """Whether a `gain` this small is distinguishable from projection noise
    at all -- the guardrail half of the waiver decision.

    The split with `week.claim_verdict` is deliberate and follows where each
    concern already lives in this repo. `claim_verdict` owns the ECONOMICS
    ("is this gain worth a priority slot"), alongside `hold_margin` and
    `drop_cost` in `ffbot.week`. This owns the GUARDRAIL ("is this a real
    difference"), alongside `can_drop` and `can_deny_claim` here, per the
    invariant that an irreversible action gets a `Verdict` with its reason
    surfaced rather than a silent allow -- a claim burns rolling priority and
    drops a player.

    Every recommendation bar on the weekly path used to be a bare `> 0.0`
    sign test, which recommends a +0.001 difference exactly as readily as a
    +50 one. `cfg.season.noise_floor_weight` is a fraction of this week's
    `week.decision_scale`, divided by how much of that position's projected
    spread historically survives contact with reality -- B10 measured that at
    0.50 for TE down to 0.23 for DEF and 0.20 for K, so a DEF needs roughly
    twice a WR's margin before the difference means anything.

    `predictiveness` absent or empty (the state of every live board before
    `draft.rank_calibration` was pointed at the curve file) degrades to a
    position-blind global floor rather than failing -- the per-position
    sharpening is a bonus, never a prerequisite.

    NOT the same thing as B10's `predictiveness_shrinkage_blend`, which
    measured -25.8 over 180 drafts and ships off. That RESHAPED THE BOARD
    across every position and could reorder picks; this raises a
    RECOMMENDATION THRESHOLD and cannot reorder anything. B10's own section
    notes that no cheaper hypothesis was tested; this is one. It ships at 0.0
    (an exact no-op) until a backtest says otherwise -- see
    docs/dev/BACKTEST.md's B15 and docs/dev/INSEASON-FINDINGS.md.
    """
    weight = cfg.season.noise_floor_weight
    if weight <= 0.0:
        return Verdict(True, "no noise floor configured")
    pred = (predictiveness or {}).get(position.upper(), 1.0)
    floor = weight * scale / max(_MIN_PREDICTIVENESS, pred)
    if gain < floor:
        detail = (
            f" (projections explain ~{pred * 100:.0f}% of {position.upper()} "
            f"outcome variance)" if predictiveness else ""
        )
        return Verdict(
            False,
            f"+{gain:.1f} is inside the {floor:.1f}-point noise floor for "
            f"{position.upper()}{detail}",
        )
    return Verdict(True, "clears the noise floor")

