"""Domain model for a fantasy roster.

`Player`'s field names and the status/slot vocabulary below trace back to
Yahoo's `yahoo_fantasy_api.Team.roster()` shape (this repo's original
target), and are kept even though the live source is now Sleeper
(`ffbot/sleeper/`) — Sleeper's own client translates onto this one
vocabulary at the boundary (see `ffbot/sleeper/models.py`) rather than a
second vocabulary spreading downstream. Fields no roster payload populates
directly (bye week, projections, ownership, draft round) are optional and
filled in separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Roster slots -----------------------------------------------------------
#
# Yahoo names multi-position slots by joining the accepted positions with "/",
# e.g. "W/R/T" is the standard flex. Sleeper spells the same concepts as
# standalone names (FLEX, SUPER_FLEX, ...) — both schemes are kept here
# rather than one replacing the other, so a roster built from either source
# (or `demo/2025/`'s fixed Yahoo-shaped fixtures) resolves slots correctly.
# We map every slot we might encounter to the set of positions it accepts.

SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DEF": frozenset({"DEF"}),
    "D/ST": frozenset({"DEF", "D/ST"}),
    # Yahoo flex variants
    "W/R": frozenset({"WR", "RB"}),
    "W/T": frozenset({"WR", "TE"}),
    "R/T": frozenset({"RB", "TE"}),
    "W/R/T": frozenset({"WR", "RB", "TE"}),
    "Q/W/R/T": frozenset({"QB", "WR", "RB", "TE"}),  # superflex
    # Sleeper flex variants — verified live against a real league.
    "FLEX": frozenset({"WR", "RB", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "WR", "RB", "TE"}),
    "WRRB_FLEX": frozenset({"WR", "RB"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
}

BENCH = "BN"
IR_SLOTS = frozenset({"IR", "IR+", "IR-R", "TAXI"})

# --- Injury / availability statuses ----------------------------------------
#
# An empty string means a healthy player on both Yahoo and Sleeper. These are
# the codes that mean "this player will not accumulate points this week", so
# starting one is always a mistake rather than a judgment call. Sleeper's own
# word-based `injury_status` values are translated onto this vocabulary at
# the client boundary (`ffbot.sleeper.models.normalize_injury_status`), never
# introduced as a second vocabulary here.

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
    team: str = ""  # NFL team abbreviation; Yahoo's roster payload carries this too

    # Populated outside the roster call.
    bye_week: int | None = None
    projected_points: float | None = None
    season_avg_points: float | None = None
    recent_points: list[float] = field(default_factory=list)
    percent_owned: float | None = None
    draft_round: int | None = None
    is_undroppable: bool = False

    # Set from roster.yml's `blocking: true` (see `roster_source.RosterEntry`)
    # — an explicit, honest admission that a hold is about denying a rival
    # rather than your own lineup. `week.hold_margin` cannot see denial value
    # on its own (it is a function of your roster alone), so this is the one
    # place a human judgment call about the *other* 11 teams enters the
    # otherwise fully-derived core/stream classification.
    blocking: bool = False

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


def bench_slots(roster_positions: dict[str, int]) -> int:
    """How many bench spots the league layout has."""
    return roster_positions.get(BENCH, 0)


def ir_slots(roster_positions: dict[str, int]) -> int:
    """How many IR-eligible spots the league layout has, summed across every
    IR slot variant (`IR`, `IR+`, `IR-R`)."""
    return sum(count for slot, count in roster_positions.items() if slot in IR_SLOTS)


def roster_capacity(roster_positions: dict[str, int]) -> int:
    """Total usable roster spots: starters plus bench.

    IR is deliberately excluded — an IR slot isn't a usable roster spot, it's
    a parking space for someone who can't play, and `lineup.optimize` already
    holds IR-eligible players there rather than counting them against
    capacity (see `lineup.optimize`'s `held_in_ir` handling).
    """
    return len(starting_slots(roster_positions)) + bench_slots(roster_positions)
