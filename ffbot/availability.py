"""Is an unrostered player a free agent, on waivers, or locked by his game?

Sleeper's public API publishes no per-player waiver flag, so this is derived
from three things the league already exposes: its settings
(`waiver_clear_days`), its transaction log, and this week's kickoffs.

The engine used to treat EVERY unrostered player as a waiver claim that
spends rolling priority. On a Sunday that is simply wrong -- almost everyone
is a free agent you add instantly for nothing -- and it told a human "not
worth a claim" about players they could just go pick up. Pricing now depends
on the status computed here (see `week.acquisition_verdict`).

The rules, checked against this league's own week-1 transactions:

- **Dropped -> waivers.** A player whose most recent transaction was a drop
  stays on waivers until the waiver run on the calendar date (US/Eastern)
  `waiver_clear_days` after the drop. A Tuesday 7:09pm ET drop in week 1
  cleared Thursday 6:37pm ET -- the date rule, not a fixed 48 hours, and not
  the weekly `waiver_day_of_week` either. The run's time of day is learned
  from the latest processed `waiver` transaction; with none to learn from, a
  drop stays on waivers through the end of its clear day (the conservative
  side: calling a waiver player a free agent sends someone to an add button
  that is not there).
- **Game started -> no this-week points.** Once his team kicks off, a free
  agent cannot be added until the game ends (LOCKED), and whenever he is
  added afterwards his week is already over. `game_started` carries that so
  the valuation zeroes his this-week half either way.
- **Everyone else is a free agent.**

Pure: no network and no clock. `now` is injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Mapping

from .league_rosters import sleeper_player_name
from .live.schedule import eastern_to_utc, utc_to_eastern
from .names import normalize_name

FREE_AGENT = "free_agent"
WAIVERS = "waivers"
LOCKED = "locked"


def local_clock(t: datetime) -> str:
    """`Thu 6:37PM` on this machine's clock, for a human."""
    return t.astimezone().strftime("%a %I:%M%p").replace(" 0", " ")


@dataclass(frozen=True)
class PlayerAvailability:
    status: str = FREE_AGENT
    clears_at: datetime | None = None  # aware UTC -- WAIVERS only
    unlocks_at: datetime | None = None  # aware UTC -- LOCKED only
    dropped_at: datetime | None = None
    game_started: bool = False

    def label(self) -> str:
        if self.status == WAIVERS:
            return f"on waivers, clears {local_clock(self.clears_at)}" if self.clears_at else "on waivers"
        if self.status == LOCKED:
            return f"locked until ~{local_clock(self.unlocks_at)} (game in progress)"
        return "free agent (already played this week)" if self.game_started else "free agent"


@dataclass
class Availability:
    now: datetime
    waivers: dict[str, PlayerAvailability] = field(default_factory=dict)  # normalized name -> entry
    kickoffs: dict[str, datetime] = field(default_factory=dict)  # team -> aware UTC kickoff
    game_lock_hours: float = 3.5
    notes: list[str] = field(default_factory=list)

    def status_for(self, name: str, team: str = "") -> PlayerAvailability:
        kickoff = self.kickoffs.get((team or "").upper())
        started = kickoff is not None and kickoff <= self.now
        entry = self.waivers.get(normalize_name(name))
        if entry is not None:
            return replace(entry, game_started=started)
        if started:
            ends = kickoff + timedelta(hours=self.game_lock_hours)
            if self.now < ends:
                return PlayerAvailability(status=LOCKED, unlocks_at=ends, game_started=True)
            return PlayerAvailability(game_started=True)
        return PlayerAvailability()

    def game_states(self) -> dict[str, str]:
        """`{team: "LIVE" | "FINAL"}` for every team whose game has kicked
        off; a team not yet playing is absent. FINAL is kickoff plus
        `game_lock_hours`, the same estimate the lock uses."""
        out: dict[str, str] = {}
        for team, kickoff in self.kickoffs.items():
            if kickoff > self.now:
                continue
            out[team] = "LIVE" if self.now < kickoff + timedelta(hours=self.game_lock_hours) else "FINAL"
        return out

    def summary(self) -> str:
        in_progress = sorted(
            team for team, k in self.kickoffs.items()
            if k <= self.now < k + timedelta(hours=self.game_lock_hours)
        )
        parts = [f"{len(self.waivers)} player(s) on waivers"]
        if in_progress:
            parts.append(f"{len(in_progress)} team(s) locked mid-game")
        return "Availability: " + ", ".join(parts) + "; every other unrostered player is a free agent"


def _from_ms(ms: object) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def derive(
    settings: Mapping,
    transactions: Iterable[Mapping],
    players: Mapping[str, Mapping],
    kickoffs_et: Mapping[str, str],
    now: datetime,
    game_lock_hours: float = 3.5,
) -> Availability:
    """`settings` is the league's `settings` block, `transactions` every
    transaction from the rounds worth checking, `players` Sleeper's players
    dump (for id -> name), and `kickoffs_et` `{team: "YYYY-MM-DDTHH:MM"}`
    in US/Eastern (`week.kickoffs_by_team`). `now` must be timezone-aware."""
    notes: list[str] = []
    clear_days = int(settings.get("waiver_clear_days") or 0)
    complete = [t for t in transactions if t.get("status") == "complete"]

    processed = [t for t in complete if t.get("type") == "waiver" and t.get("status_updated")]
    run_time: time | None = None
    if processed:
        latest = max(processed, key=lambda t: int(t["status_updated"]))
        run_time = utc_to_eastern(_from_ms(latest["status_updated"])).time().replace(second=0, microsecond=0)
    else:
        notes.append(
            "no processed waiver claim yet to learn the waiver run's time from -- "
            "a dropped player counts as on waivers through the end of his clear day"
        )

    last_event: dict[str, tuple[int, str]] = {}
    for t in complete:
        ts = int(t.get("status_updated") or t.get("created") or 0)
        for kind in ("drops", "adds"):
            for pid in (t.get(kind) or {}):
                prev = last_event.get(str(pid))
                if prev is None or ts >= prev[0]:
                    last_event[str(pid)] = (ts, kind)

    waivers: dict[str, PlayerAvailability] = {}
    for pid, (ts, kind) in last_event.items():
        if kind != "drops" or clear_days <= 0:
            continue
        dropped = _from_ms(ts)
        clear_date = utc_to_eastern(dropped).date() + timedelta(days=clear_days)
        clears = eastern_to_utc(datetime.combine(clear_date, run_time or time(23, 59)))
        if clears <= now:
            continue
        p = players.get(pid)
        name = sleeper_player_name(p) if p else ""
        if not name:
            notes.append(f"dropped Sleeper player_id {pid!r} has no name on file -- status unknown")
            continue
        waivers[normalize_name(name)] = PlayerAvailability(status=WAIVERS, clears_at=clears, dropped_at=dropped)

    kickoffs: dict[str, datetime] = {}
    for team, iso in kickoffs_et.items():
        try:
            kickoffs[team.upper()] = eastern_to_utc(datetime.fromisoformat(iso))
        except (TypeError, ValueError):
            continue
    if not kickoffs:
        notes.append("kickoff times unknown this run -- players whose game already started are not detected")

    return Availability(
        now=now, waivers=waivers, kickoffs=kickoffs, game_lock_hours=game_lock_hours, notes=notes,
    )
