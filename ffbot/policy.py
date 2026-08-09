"""Guardrails on irreversible actions.

Lineup changes are freely reversible, so the optimizer runs unsupervised. A
drop is not: once another manager claims the player, they are gone. Since the
whole point of this agent is that it acts without asking, the drop path is
constrained by policy here rather than by a confirmation prompt.

Every rule answers "would I regret this on Monday?" and every threshold is
tunable in config.yml.
"""

from __future__ import annotations

import math
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


def max_faab_bid(remaining_budget: int, cfg: Config) -> int:
    """Largest bid permitted on a single claim.

    Bounded both by a share of what is left and by the reserve held back for
    the playoff run, so one exciting waiver week cannot spend the season.
    """
    if remaining_budget <= cfg.faab.min_reserve:
        return 0
    spendable = remaining_budget - cfg.faab.min_reserve
    capped = math.floor(remaining_budget * cfg.faab.max_bid_pct)
    return max(0, min(spendable, capped))


def can_bid_on(player: Player, cfg: Config) -> Verdict:
    """Whether a free agent is worth spending budget on at all."""
    if (
        player.percent_owned is not None
        and player.percent_owned < cfg.faab.min_pct_owned_to_bid
    ):
        return Verdict(
            False,
            f"owned in only {player.percent_owned:.0f}% of leagues "
            f"(floor {cfg.faab.min_pct_owned_to_bid:.0f}%)",
        )
    return Verdict(True, "eligible")


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
