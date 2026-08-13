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

from .models import Player
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


def fetch_my_roster(
    client: SleeperClient,
    league_id: str,
    roster_id: int,
    players: dict,
    ownership: Optional[dict] = None,
    roster_ttl_minutes: Optional[float] = None,
) -> list[SleeperRosterPlayer]:
    """The configured `roster_id`'s real player list, joined against
    `players` (the full dump — `SleeperClient.players()`, passed in rather
    than fetched here so a caller already holding a fresh copy, e.g. also
    building `league_rosters.yml`, doesn't pay for a second ~5MB download)
    for name/team/position/status, and optionally `ownership`
    (`SleeperClient.ownership()`) for `percent_owned`/`started_pct`.

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
        ))
    return out


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
    players: Sequence[Player], sleeper_players: Sequence[SleeperRosterPlayer]
) -> list[Player]:
    """Set `status`/`percent_owned` from a live Sleeper roster fetch, as the
    BASE layer — `ffbot.week.apply_status_overrides`, run afterward as it
    always has been, still lets a hand-researched `weekly/week-NN.yml` entry
    win over this. A player this fetch doesn't cover (shouldn't happen —
    `sleeper_players` is meant to be the same identity list `players` was
    resolved from — but never assumed) passes through unchanged rather than
    erroring.
    """
    by_name = {normalize_name(sp.name): sp for sp in sleeper_players}
    out = []
    for p in players:
        sp = by_name.get(normalize_name(p.name))
        if sp is None:
            out.append(p)
            continue
        out.append(replace(p, status=sp.status, percent_owned=sp.percent_owned))
    return out
