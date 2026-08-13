"""Translate Sleeper's payload shapes onto this repo's existing domain
vocabulary (`ffbot.models`) — the one place Sleeper-specific spellings are
allowed to exist. Everything downstream of this module (`lineup.py`,
`policy.py`, `ffbot/history/index.py`) keeps speaking the single vocabulary
it always has; a second one is never introduced past this boundary.

Sleeper's own flex slot names (`FLEX`/`SUPER_FLEX`/`WRRB_FLEX`/`REC_FLEX`)
are already registered directly in `ffbot.models.SLOT_ELIGIBILITY` alongside
Yahoo's `"W/R/T"` scheme — no translation needed there, since Sleeper's
`roster_positions` values match those names verbatim. What this module does
translate is `injury_status`: Sleeper's word-based codes (`Questionable`,
`Doubtful`, `IR`, ...) onto the existing single-letter vocabulary in
`ffbot.models.STATUS_OUT`/`STATUS_QUESTIONABLE`. This must run at the client
boundary, not downstream — `lineup._must_bench` and `policy.can_drop` only
ever check membership in those two frozensets.
"""

from __future__ import annotations

from typing import Optional

# `Player.status` vocabulary this repo has used since before any live route
# existed (`ffbot.models.STATUS_OUT` / `STATUS_QUESTIONABLE` /
# `STATUS_IR_ELIGIBLE`) — Yahoo-derived single-letter codes. Sleeper's
# `injury_status` is word-based; this table is the one-time translation.
#
# "DNR" ("Did Not Return", seen on real 2026 data during scoping — a player
# who left a game and didn't come back) has no equivalent in the existing
# vocabulary. Mapped to "D" (Doubtful) deliberately conservative — an
# unresolved in-game injury is closer to "likely to miss" than a clean bill
# of health, matching this codebase's existing bias (`doubtful_is_out`
# defaults to treating Doubtful as a hard bench). Revisit this mapping once
# a season of real DNR outcomes is on hand; it is a judgment call, not a
# verified fact, and is called out here for exactly that reason.
_INJURY_STATUS_MAP: dict[str, str] = {
    "questionable": "Q",
    "doubtful": "D",
    "out": "O",
    "ir": "IR",
    "pup": "PUP",
    "sus": "SUSP",
    "suspended": "SUSP",
    "na": "NA",
    "dnr": "D",
    "dnp": "Q",  # "Did Not Practice" -- risk signal, not a ruled-out player
}


def normalize_injury_status(sleeper_status: Optional[str]) -> str:
    """Sleeper's `injury_status` (or `None`/`""` for healthy) -> this repo's
    existing status vocabulary. Unrecognized non-empty values pass through
    unchanged rather than being silently dropped to healthy — the same
    "surface it, don't hide it" instinct `ffbot.names`/`ffbot.intel` use for
    an unmatched player, applied to an unmatched status string.
    """
    if not sleeper_status:
        return ""
    key = sleeper_status.strip().lower()
    return _INJURY_STATUS_MAP.get(key, sleeper_status.strip())


def is_defense_id(player_id: str) -> bool:
    """Sleeper spells a defense's `player_id` as its team abbreviation
    itself (`"KC"`, `"SF"`, ...) rather than a numeric string — verified
    live during scoping. A defense's `player_id` is always short, uppercase,
    and alphabetic; every real player id seen was purely numeric."""
    return player_id.isalpha() and player_id.isupper() and len(player_id) <= 4
