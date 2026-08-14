"""M3 roster route: your Sleeper roster's real identity — names, teams, and
live injury status/ownership% — fetched directly, the seam
`docs/INSEASON.md` always described as "once Yahoo API access lands." No
credentials needed, so it's just buildable now.

`roster.yml` (`ffbot.roster_source`, the M1 baseline) stays relevant even
here: it's read as an optional per-player FLAG overlay (`undroppable`/
`keeper_round`/`note`/`blocking` — human judgment no platform can supply),
matched onto the live roster by name, never as the identity list itself.
Missing entirely is a normal, inert case — every flag just stays at its
default, same "no file = no-op" contract every optional input in this repo
already uses.

`ffbot.roster_source.load_roster`'s `entries` parameter is the seam this
module plugs into: pass `merge_flags(...)`'s output there instead of letting
it call `load_roster_entries(roster_path)` itself, and everything downstream
(matching against weekly/provider/fallback projection rows, bye backfill)
runs completely unchanged. `apply_sleeper_identity`, run afterward, is the
one genuinely new step — it sets `status`/`percent_owned` directly from
Sleeper as the BASE layer, which `ffbot.week.apply_status_overrides` (run
after this, same as it always has been) can still win over: a hand-
researched `weekly/week-NN.yml` entry beats a live-but-possibly-stale API
field, never the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .models import BENCH, IR_SLOTS, Player, equivalent_slot
from .names import normalize_name
from .roster_source import RosterEntry, RosterError, load_roster_entries
from .sleeper.client import SleeperClient
from .sleeper.models import normalize_injury_status


class RosterSourceError(ValueError):
    """The configured Sleeper roster could not be resolved."""


@dataclass(frozen=True)
class SleeperRosterPlayer:
    """One player on the configured roster, resolved from Sleeper directly."""

    sleeper_id: str
    name: str
    team: str
    position: str
    status: str  # already normalized onto this repo's existing vocabulary
    percent_owned: Optional[float] = None
    started_pct: Optional[float] = None  # no existing-field equivalent; carried for a future consumer
    # This player's CURRENT slot in the Sleeper app -- "" (the default)
    # means unknown (no slot map was supplied to `fetch_my_roster`), not
    # "benched". See `starters_slot_map`.
    slot: str = ""


def resolve_roster_id(
    client: SleeperClient, league_id: str, username: str, roster_ttl_minutes: Optional[float] = None,
) -> int:
    """Look up `username`'s `roster_id` in `league_id` — the one extra
    lookup `sleeper.roster_id: null` costs each run, so it never needs to be
    hand-copied from `scripts/whoami.py`'s output if you'd rather not.

    `roster_ttl_minutes`, if given, overrides `SleeperClient.rosters`'s own
    default TTL — the seam `cfg.roster_source.cache_ttl_minutes` plugs into.
    """
    user = client.user(username)
    if user is None:
        raise RosterSourceError(f"no Sleeper user found for username {username!r}")
    kwargs = {} if roster_ttl_minutes is None else {"ttl_minutes": roster_ttl_minutes}
    rosters = client.rosters(league_id, **kwargs)
    mine = next((r for r in rosters if r.get("owner_id") == user.get("user_id")), None)
    if mine is None:
        raise RosterSourceError(f"no roster owned by {username!r} found in league {league_id!r}")
    return int(mine["roster_id"])


def starters_slot_map(
    league_roster_positions: Sequence[str],
    starters: Sequence[str],
    reserve: Sequence[str] = (),
    taxi: Sequence[str] = (),
) -> tuple[dict[str, str], list[str]]:
    """Zip a Sleeper roster's `starters` (ordered player ids; `"0"` marks an
    empty slot) against `league_roster_positions` -- the league's OWN
    ordered `roster_positions` array from `SleeperClient.league()`, NOT
    `cfg.roster_positions` (a `{slot: count}` dict that has already lost the
    ordering this zip depends on) -- to learn which slot each starter
    currently occupies in the Sleeper app. `reserve`/`taxi` map straight to
    `"IR"`/`"TAXI"`.

    Returns `(player_id -> slot, warnings)`. A `starters`/non-bench-slot
    length mismatch (a league shape this repo hasn't seen live) still zips
    as far as it can and adds a warning string rather than raising — a
    caveated live lineup is still better than none; the caller surfaces the
    warning as an alert.
    """
    non_bench = [s for s in league_roster_positions if s != BENCH and s not in IR_SLOTS]
    warnings: list[str] = []
    if len(starters) != len(non_bench):
        warnings.append(
            f"Sleeper's starters list has {len(starters)} entries but the league's "
            f"roster_positions has {len(non_bench)} non-bench slot(s) -- live lineup "
            "slots may be misaligned."
        )
    mapping: dict[str, str] = {}
    for slot, player_id in zip(non_bench, starters):
        if player_id and player_id != "0":
            mapping[player_id] = slot
    for player_id in reserve:
        if player_id and player_id != "0":
            mapping[player_id] = "IR"
    for player_id in taxi:
        if player_id and player_id != "0":
            mapping[player_id] = "TAXI"
    return mapping, warnings


def resolve_starters_slot_map(
    client: SleeperClient,
    league_id: str,
    roster_id: int,
    league_roster_positions: Sequence[str],
    roster_ttl_minutes: Optional[float] = None,
) -> tuple[dict[str, str], list[str]]:
    """`starters_slot_map`, but resolving `roster_id`'s `starters`/`reserve`/
    `taxi` from a live `client.rosters()` call first. Reuses that call's own
    TTL cache (same league_id/ttl `fetch_my_roster`/`waiver_position`
    already call it with) -- a caller that also fetches those pays for one
    HTTP request, not two, same convention as `waiver_position`'s own
    docstring. `roster_id` not found in this league returns `({}, [])`
    rather than raising -- `fetch_my_roster` already raises loudly for that
    case; this is a secondary lookup that should degrade quietly rather
    than fail a second time for the same misconfiguration.
    """
    kwargs = {} if roster_ttl_minutes is None else {"ttl_minutes": roster_ttl_minutes}
    rosters = client.rosters(league_id, **kwargs)
    mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if mine is None:
        return {}, []
    return starters_slot_map(
        league_roster_positions,
        mine.get("starters") or [],
        mine.get("reserve") or [],
        mine.get("taxi") or [],
    )


def fetch_my_roster(
    client: SleeperClient,
    league_id: str,
    roster_id: int,
    players: dict,
    ownership: Optional[dict] = None,
    roster_ttl_minutes: Optional[float] = None,
    slot_map: Optional[dict[str, str]] = None,
) -> list[SleeperRosterPlayer]:
    """The configured `roster_id`'s real player list, joined against
    `players` (the full dump — `SleeperClient.players()`, passed in rather
    than fetched here so a caller already holding a fresh copy, e.g. also
    building `league_rosters.yml`, doesn't pay for a second ~5MB download)
    for name/team/position/status, and optionally `ownership`
    (`SleeperClient.ownership()`) for `percent_owned`/`started_pct`.

    `slot_map` (`player_id -> slot`, from `resolve_starters_slot_map`), when
    given, additionally fills each player's `.slot` — the live lineup
    baseline `apply_sleeper_identity` can then write onto `Player
    .selected_position`. A player_id absent from the map (shouldn't happen —
    every rostered player is either a starter, on the bench, or on
    IR/taxi — but never assumed) just gets `slot=""`, i.e. "unknown", not
    "benched"; `""` reads as bench downstream regardless, so this never
    produces a wrong-looking lineup, only a possibly-incomplete one.

    Raises `RosterSourceError` if `roster_id` isn't in this league — a
    config typo (wrong league_id, stale roster_id after a league reset)
    should fail loudly here, not silently produce an empty roster.

    `roster_ttl_minutes`, if given, overrides `SleeperClient.rosters`'s own
    default TTL — the seam `cfg.roster_source.cache_ttl_minutes` plugs into.
    """
    kwargs = {} if roster_ttl_minutes is None else {"ttl_minutes": roster_ttl_minutes}
    rosters = client.rosters(league_id, **kwargs)
    mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if mine is None:
        raise RosterSourceError(f"roster_id {roster_id} not found in league {league_id!r}")

    ownership = ownership or {}
    slot_map = slot_map or {}
    out: list[SleeperRosterPlayer] = []
    for player_id in mine.get("players") or []:
        p = players.get(player_id)
        if p is None:
            continue  # a player_id the dump doesn't (yet) recognize -- rare; the identity list just won't include them this run
        name = p.get("full_name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
        if not name:
            continue
        own = ownership.get(player_id) or {}
        out.append(SleeperRosterPlayer(
            sleeper_id=player_id,
            name=name,
            team=(p.get("team") or "").strip().upper(),
            position=(p.get("position") or "").strip().upper(),
            status=normalize_injury_status(p.get("injury_status")),
            percent_owned=own.get("owned"),
            started_pct=own.get("started"),
            slot=slot_map.get(player_id, ""),
        ))
    return out


def waiver_position(
    client: SleeperClient, league_id: str, roster_id: int, roster_ttl_minutes: Optional[float] = None,
) -> Optional[int]:
    """Your current rolling waiver priority (1 = best/most valuable to keep),
    straight from Sleeper's own `roster.settings.waiver_position` field --
    the seam `report.LoadedReport.waiver_priority` fills in so `--priority`
    stops being something you have to type by hand every week.

    Reuses `client.rosters()`, the exact same call/TTL cache
    `fetch_my_roster` already makes for this league -- a caller that fetches
    both this run pays for one HTTP request, not two. `None` (not an error)
    if `roster_id` isn't in this league or the field is simply absent (a
    league that has never processed a waiver has no `waiver_position` yet);
    only an actual fetch failure is the caller's problem to handle, via
    `SleeperFetchError` propagating unchanged.
    """
    kwargs = {} if roster_ttl_minutes is None else {"ttl_minutes": roster_ttl_minutes}
    rosters = client.rosters(league_id, **kwargs)
    mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if mine is None:
        return None
    value = (mine.get("settings") or {}).get("waiver_position")
    return int(value) if value is not None else None


def merge_flags(
    sleeper_players: Sequence[SleeperRosterPlayer], roster_path: str = "roster.yml"
) -> list[RosterEntry]:
    """Sleeper's live roster -> the `RosterEntry` list
    `ffbot.roster_source.load_roster`'s `entries` parameter expects, with
    any matching `roster.yml` entry's flags merged on by normalized name.
    A missing (or unreadable) `roster.yml` is a normal case here — every
    flag simply stays at its inert default, exactly like every other
    optional input in this repo.
    """
    overlay: dict[str, RosterEntry] = {}
    try:
        for entry in load_roster_entries(roster_path):
            overlay[normalize_name(entry.name)] = entry
    except RosterError:
        pass

    out: list[RosterEntry] = []
    for sp in sleeper_players:
        flag = overlay.get(normalize_name(sp.name))
        out.append(RosterEntry(
            name=sp.name,
            undroppable=flag.undroppable if flag else False,
            keeper_round=flag.keeper_round if flag else None,
            acquired=flag.acquired if flag else "",
            note=flag.note if flag else "",
            blocking=flag.blocking if flag else False,
        ))
    return out


def apply_sleeper_identity(
    players: Sequence[Player],
    sleeper_players: Sequence[SleeperRosterPlayer],
    roster_positions: Optional[dict] = None,
) -> list[Player]:
    """Set `status`/`percent_owned` from a live Sleeper roster fetch, as the
    BASE layer — `ffbot.week.apply_status_overrides`, run afterward as it
    always has been, still lets a hand-researched `weekly/week-NN.yml` entry
    win over this. A player this fetch doesn't cover (shouldn't happen —
    `sleeper_players` is meant to be the same identity list `players` was
    resolved from — but never assumed) passes through unchanged rather than
    erroring.

    `roster_positions` (`cfg.roster_positions`), when given, ALSO sets
    `selected_position` for any player carrying a known `sp.slot` — the live
    lineup baseline (see `starters_slot_map`), translated through
    `models.equivalent_slot` so a Sleeper spelling like `"FLEX"` lands on
    whatever spelling the configured layout actually uses. Omitted (the
    default) or a player with `slot == ""` (unknown) leaves
    `selected_position` untouched, exactly as before this parameter existed.
    """
    by_name = {normalize_name(sp.name): sp for sp in sleeper_players}
    out = []
    for p in players:
        sp = by_name.get(normalize_name(p.name))
        if sp is None:
            out.append(p)
            continue
        updates: dict = {"status": sp.status, "percent_owned": sp.percent_owned}
        if sp.slot and roster_positions is not None:
            updates["selected_position"] = equivalent_slot(sp.slot, roster_positions)
        out.append(replace(p, **updates))
    return out
