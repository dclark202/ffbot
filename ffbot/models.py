"""Domain model for a fantasy roster.

Field names on `Player` deliberately mirror the keys Yahoo returns from
`yahoo_fantasy_api.Team.roster()` so the eventual fetch layer is a rename-free
translation. Fields Yahoo does not put in the roster payload (bye week,
projections, ownership, draft round) are optional and populated separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Roster slots -----------------------------------------------------------
#
# Yahoo names multi-position slots by joining the accepted positions with "/",
# e.g. "W/R/T" is the standard flex. We map every slot we might encounter to
# the set of positions it accepts.

SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DEF": frozenset({"DEF"}),
    "D/ST": frozenset({"DEF", "D/ST"}),
    # Flex variants
    "W/R": frozenset({"WR", "RB"}),
    "W/T": frozenset({"WR", "TE"}),
    "R/T": frozenset({"RB", "TE"}),
    "W/R/T": frozenset({"WR", "RB", "TE"}),
    "Q/W/R/T": frozenset({"QB", "WR", "RB", "TE"}),  # superflex
}

BENCH = "BN"
IR_SLOTS = frozenset({"IR", "IR+", "IR-R"})

# --- Injury / availability statuses ----------------------------------------
#
# Yahoo reports an empty string for a healthy player. These are the codes that
# mean "this player will not accumulate points this week", so starting one is
# always a mistake rather than a judgment call.

STATUS_OUT = frozenset(
    {
        "O",  # Out
        "D",  # Doubtful — ~75% miss rate, treated as out by default
        "IR",  # Injured reserve
        "IR-R",  # IR, designated to return
        "IR-L",  # IR, long term
        "NA",  # Not active / inactive
        "SUSP",  # Suspended
        "PUP",  # Physically unable to perform
        "NFI",  # Non-football injury
    }
)

# Available, but with meaningful risk of a late scratch.
STATUS_QUESTIONABLE = frozenset({"Q", "P"})

# Statuses that make a player IR-slot eligible.
STATUS_IR_ELIGIBLE = frozenset({"IR", "IR-R", "IR-L", "NA", "PUP", "NFI", "SUSP"})


@dataclass
class Player:
    """A single rostered or available player."""

    player_id: int
    name: str
    eligible_positions: list[str]
    selected_position: str = BENCH
    status: str = ""

    # Populated outside the roster call.
    bye_week: int | None = None
    projected_points: float | None = None
    season_avg_points: float | None = None
    recent_points: list[float] = field(default_factory=list)
    percent_owned: float | None = None
    draft_round: int | None = None
    is_undroppable: bool = False

    def is_out(self, week: int | None = None) -> bool:
        """True when this player cannot score this week — injury, suspension, or bye.

        This is the hard signal the optimizer refuses to override.
        """
        if self.status in STATUS_OUT:
            return True
        if week is not None and self.bye_week == week:
            return True
        return False

    def is_questionable(self) -> bool:
        return self.status in STATUS_QUESTIONABLE

    def is_ir_eligible(self) -> bool:
        return self.status in STATUS_IR_ELIGIBLE

    def unavailability_is_temporary(self, week: int | None = None) -> bool:
        """True when the player is unavailable now but expected back.

        A bye week or a Questionable tag is not a reason to drop somebody, and
        `policy` uses this to refuse those drops.
        """
        if week is not None and self.bye_week == week and self.status == "":
            return True
        return self.status in STATUS_QUESTIONABLE or self.status in {"IR-R", "D"}


def slot_accepts(slot: str, player: Player) -> bool:
    """Whether `player` may occupy `slot`, ignoring health."""
    if slot == BENCH:
        return True
    if slot in IR_SLOTS:
        return player.is_ir_eligible()
    accepted = SLOT_ELIGIBILITY.get(slot)
    if accepted is None:
        # Unknown slot name — fall back to an exact position match so a new
        # Yahoo slot type degrades to something safe rather than accepting all.
        return slot in player.eligible_positions
    return any(pos in accepted for pos in player.eligible_positions)


def starting_slots(roster_positions: dict[str, int]) -> list[str]:
    """Expand a {slot: count} league setting into a flat list of startable slots.

    Bench and IR slots are excluded — they are where the optimizer puts what it
    is not starting, not something it fills deliberately.
    """
    slots: list[str] = []
    for slot, count in roster_positions.items():
        if slot == BENCH or slot in IR_SLOTS:
            continue
        slots.extend([slot] * count)
    return slots
