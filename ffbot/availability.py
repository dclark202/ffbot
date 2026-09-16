"""Is an unrostered player a free agent or on waivers?

Sleeper's public API publishes no per-player waiver flag, so this is derived
from what the league already exposes: its settings (`waiver_day_of_week`,
`waiver_clear_days`, `daily_waivers`), its transaction log, and the kickoffs
of this week and last.

The engine used to treat EVERY unrostered player as a waiver claim that
spends rolling priority. On a Sunday that is simply wrong -- almost everyone
is a free agent you add instantly for nothing. The 2026-09-13 fix modelled
only the per-DROP half of Sleeper's rules, so on Tuesday 2026-09-15, once
week 1's two drops had cleared, it reported "0 player(s) on waivers; every
other unrostered player is a free agent" and told the manager to add three
defenses that were all sitting on waivers until Wednesday's run. Pricing
depends on the status computed here (see `week.acquisition_verdict`), so
this module has to carry the whole rule.

The rules, from Sleeper's support article "Waivers for Regular Season &
Playoffs" and checked against this league's own transactions:

- **Played -> waivers until the weekly run.** "If a player's game begins on
  Thursday night, they will lock at kickoff and remain on waivers until your
  selected waiver clear day." Every unrostered player whose team has kicked
  off since the last weekly run is on waivers until the next one, on
  `waiver_day_of_week` (Sleeper counts Monday as 0) at about 12:05am Pacific
  -- `weekly_run_time_et` until a claim processed on that weekday teaches
  the real time. A claim can be placed during his game; his this-week
  points are gone either way (`game_started`).
- **Dropped -> waivers until his clear date.** A player whose most recent
  transaction was a drop stays on waivers until the waiver run on the
  calendar date (US/Eastern) `waiver_clear_days` after the drop. A Tuesday
  7:09pm ET drop in week 1 cleared Thursday 6:37pm ET -- the date rule, not
  a fixed 48 hours. That run's time of day is learned from the latest
  processed claim on a dropped player; with none to learn from, a drop stays
  on waivers through the end of its clear day (the conservative side:
  calling a waiver player a free agent sends someone to an add button that
  is not there). If his team also kicked off this cycle he clears at the
  LATER of the two moments -- Sleeper: "the pending claims would be pushed
  back to whichever has the later time."
- **Everyone else is a free agent**: a bye-week team's player all week, and
  everyone after the weekly run until his next kickoff.
- Daily-waiver leagues (`daily_waivers`) are not modelled: the weekly rule
  is off, with a note, and only the per-drop rule applies.

Last week's kickoffs (`Availability.prior_kickoffs`) feed ONLY the
waiver-cycle rule. `kickoffs` is this week's and is the only thing
`game_states()` reads: `ffbot.gameplan` turns every team in it into
`Player.game_locked`, so merging last week's games in would lock the whole
roster on a Tuesday.

Pure: no network and no clock. `now` is injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Mapping

from .league_rosters import sleeper_player_name
from .live.schedule import eastern_to_utc, utc_to_eastern
from .names import canonical_team, defense_key, normalize_name

FREE_AGENT = "free_agent"
WAIVERS = "waivers"

# Sleeper's usual weekly run, ~12:05am Pacific, as US/Eastern -- the config
# default (`WaiverStatusSourceConfig.weekly_run_time_et`) until a processed
# claim on the run's weekday teaches the real time.
DEFAULT_WEEKLY_RUN_TIME_ET = "03:05"


def local_clock(t: datetime) -> str:
    """`Thu 6:37PM` on this machine's clock, for a human."""
    return t.astimezone().strftime("%a %I:%M%p").replace(" 0", " ")


@dataclass(frozen=True)
class PlayerAvailability:
    status: str = FREE_AGENT
    clears_at: datetime | None = None  # aware UTC -- WAIVERS: the later of his drop clear and the next weekly run
    dropped_at: datetime | None = None  # aware UTC -- on waivers via a drop
    played_at: datetime | None = None  # aware UTC -- on waivers via a kickoff since the last weekly run
    game_started: bool = False  # THIS week's game has kicked off: his this-week points are gone

    def label(self) -> str:
        if self.status == WAIVERS:
            if self.dropped_at is not None:
                why = f" (dropped {self.dropped_at.astimezone():%a})"
            elif self.played_at is not None:
                why = f" (played {self.played_at.astimezone():%a})"
            else:
                why = ""
            when = f", clears {local_clock(self.clears_at)}" if self.clears_at else ""
            return f"on waivers{why}{when}"
        return "free agent (already played this week)" if self.game_started else "free agent"


@dataclass
class Availability:
    now: datetime
    waivers: dict[str, PlayerAvailability] = field(default_factory=dict)  # normalized name -> entry (dropped)
    kickoffs: dict[str, datetime] = field(default_factory=dict)  # team -> aware UTC kickoff, THIS week only
    game_lock_hours: float = 3.5
    notes: list[str] = field(default_factory=list)
    # Last week's kickoffs -- the waiver-cycle rule only, never `game_states()`.
    prior_kickoffs: dict[str, datetime] = field(default_factory=dict)
    # The weekly runs bracketing `now` (aware UTC). None = the rule is off:
    # a daily-waiver league, settings without a run day, or a test that
    # builds an `Availability` by hand.
    cycle_start: datetime | None = None
    next_run: datetime | None = None

    @property
    def weekly_rule_on(self) -> bool:
        return self.cycle_start is not None and self.next_run is not None and self.now < self.next_run

    @staticmethod
    def team_key(name: str, team: str = "") -> str:
        """The schedule's key for this player's team. A defense routinely
        arrives with a blank `team` (the board's DST rows carry none), so his
        own name resolves it, the way `week._resolve_team` does for weather
        and Vegas -- the 2026-09-15 run read all four of its defenses as free
        agents for exactly this reason."""
        return canonical_team(defense_key(name, team) or team or "")

    def played_this_cycle(self, team: str, name: str = "") -> datetime | None:
        """The kickoff that put `team`'s unrostered players on waivers, or
        None: his most recent kickoff (this week's or last week's) if it
        falls since the last weekly run."""
        if not self.weekly_rule_on:
            return None
        team = self.team_key(name, team)
        past = [
            k for k in (self.kickoffs.get(team), self.prior_kickoffs.get(team))
            if k is not None and k <= self.now
        ]
        if not past:
            return None
        latest = max(past)
        return latest if latest >= self.cycle_start else None

    def status_for(self, name: str, team: str = "") -> PlayerAvailability:
        team = self.team_key(name, team)
        kickoff = self.kickoffs.get(team)
        started = kickoff is not None and kickoff <= self.now
        played = self.played_this_cycle(team, name)
        entry = self.waivers.get(normalize_name(name))
        if entry is not None:
            clears = entry.clears_at
            if played is not None and (clears is None or self.next_run > clears):
                clears = self.next_run
            return replace(entry, clears_at=clears, played_at=played, game_started=started)
        if played is not None:
            return PlayerAvailability(status=WAIVERS, clears_at=self.next_run, played_at=played, game_started=started)
        return PlayerAvailability(game_started=started)

    def claim_outcomes_since_run(
        self, transactions: Iterable[Mapping], roster_id: int | None, players: Mapping[str, Mapping],
    ) -> list["ClaimOutcome"]:
        """`claim_outcomes` for the run that opened this cycle -- what the
        free-agent check reports the morning after. Empty when the weekly
        rule is off: there is no run to ask about."""
        if self.cycle_start is None:
            return []
        return claim_outcomes(transactions, roster_id, self.cycle_start - timedelta(hours=1), players)

    def played_teams(self) -> list[str]:
        """Every team whose kickoff since the last weekly run has its
        unrostered players on waivers right now."""
        teams = set(self.kickoffs) | set(self.prior_kickoffs)
        return sorted(t for t in teams if self.played_this_cycle(t) is not None)

    def game_states(self) -> dict[str, str]:
        """`{team: "LIVE" | "FINAL"}` for every team whose game has kicked
        off THIS week; a team not yet playing is absent. FINAL is kickoff
        plus `game_lock_hours`, the same estimate the rostered lock uses.
        Reads `kickoffs` only -- see the module docstring on `prior_kickoffs`."""
        out: dict[str, str] = {}
        for team, kickoff in self.kickoffs.items():
            if kickoff > self.now:
                continue
            out[team] = "LIVE" if self.now < kickoff + timedelta(hours=self.game_lock_hours) else "FINAL"
        return out

    def in_progress(self) -> list[str]:
        return sorted(
            team for team, k in self.kickoffs.items()
            if k <= self.now < k + timedelta(hours=self.game_lock_hours)
        )

    def summary(self) -> str:
        parts: list[str] = []
        played = self.played_teams()
        if self.weekly_rule_on and played:
            parts.append(
                f"on waivers until {local_clock(self.next_run)} -- "
                f"{len(played)} team(s) have kicked off since the last run"
            )
            if self.waivers:
                parts.append(f"{len(self.waivers)} recently dropped")
            tail = "; everyone else (bye teams, cleared drops) is a free agent"
        elif self.weekly_rule_on:
            parts.append(f"free agency open since {local_clock(self.cycle_start)}")
            if self.waivers:
                parts.append(f"{len(self.waivers)} recently dropped player(s) still on waivers")
            tail = "; every other unrostered player is a free agent"
        else:
            parts.append(f"{len(self.waivers)} player(s) on waivers")
            tail = "; every other unrostered player is a free agent"
        live = self.in_progress()
        if live:
            parts.append(f"{len(live)} team(s) in progress")
        return "Availability: " + ", ".join(parts) + tail


def _from_ms(ms: object) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def _parse_kickoffs(kickoffs_et: Mapping[str, str] | None) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for team, iso in (kickoffs_et or {}).items():
        try:
            out[team.upper()] = eastern_to_utc(datetime.fromisoformat(iso))
        except (TypeError, ValueError):
            continue
    return out


def _parse_run_time(text: str) -> time | None:
    try:
        return time.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def weekly_run_window(now: datetime, run_weekday: int, run_time: time) -> tuple[datetime, datetime]:
    """`(cycle_start, next_run)`, aware UTC: the weekly runs bracketing
    `now`, computed on the US/Eastern calendar (`run_weekday` counts Monday
    as 0) so a daylight-time change moves the run with the clock."""
    now_et = utc_to_eastern(now)
    days_ahead = (run_weekday - now_et.weekday()) % 7
    candidate = datetime.combine(now_et.date() + timedelta(days=days_ahead), run_time)
    if candidate <= now_et:
        candidate += timedelta(days=7)
    return eastern_to_utc(candidate - timedelta(days=7)), eastern_to_utc(candidate)


def _split_processed_claims(complete: list[Mapping]) -> tuple[list[Mapping], list[Mapping]]:
    """`(weekly_runs, drop_clears)`: which processed claims were the weekly
    run and which a drop clearing. A claim on a player nobody dropped could
    only have processed at the weekly run (he was on waivers for having
    played); a claim that followed a drop processed when that drop cleared.
    The one week-1 claim (Thu 6:37pm) was the latter, and a single learner
    once applied its time to both."""
    drops_by_pid: dict[str, list[int]] = {}
    for t in complete:
        ts = int(t.get("status_updated") or t.get("created") or 0)
        for pid in (t.get("drops") or {}):
            drops_by_pid.setdefault(str(pid), []).append(ts)
    weekly_runs: list[Mapping] = []
    drop_clears: list[Mapping] = []
    for t in complete:
        if t.get("type") != "waiver" or not t.get("status_updated"):
            continue
        ts = int(t["status_updated"])
        followed_a_drop = any(
            any(d < ts for d in drops_by_pid.get(str(pid), []))
            for pid in (t.get("adds") or {})
        )
        (drop_clears if followed_a_drop else weekly_runs).append(t)
    return weekly_runs, drop_clears


def _time_of_latest(claims: list[Mapping]) -> tuple[time, datetime] | None:
    if not claims:
        return None
    latest = _from_ms(max(int(t["status_updated"]) for t in claims))
    return utc_to_eastern(latest).time().replace(second=0, microsecond=0), latest


def derive(
    settings: Mapping,
    transactions: Iterable[Mapping],
    players: Mapping[str, Mapping],
    kickoffs_et: Mapping[str, str],
    now: datetime,
    game_lock_hours: float = 3.5,
    *,
    prior_kickoffs_et: Mapping[str, str] | None = None,
    weekly_run_time_et: str = DEFAULT_WEEKLY_RUN_TIME_ET,
) -> Availability:
    """`settings` is the league's `settings` block, `transactions` every
    transaction from the rounds worth checking, `players` Sleeper's players
    dump (for id -> name), `kickoffs_et` this week's `{team: "YYYY-MM-DDTHH:MM"}`
    in US/Eastern (`week.kickoffs_by_team`) and `prior_kickoffs_et` last
    week's, in the same shape. `now` must be timezone-aware."""
    notes: list[str] = []
    clear_days = int(settings.get("waiver_clear_days") or 0)
    complete = [t for t in transactions if t.get("status") == "complete"]
    weekly_runs, drop_clears = _split_processed_claims(complete)

    # --- The weekly cycle: which run brackets `now`. ---------------------
    cycle_start: datetime | None = None
    next_run: datetime | None = None
    run_weekday: int | None = None
    if settings.get("daily_waivers"):
        notes.append("daily waivers are not modelled -- only a dropped player is shown on waivers")
    else:
        try:
            run_weekday = int(settings.get("waiver_day_of_week"))
        except (TypeError, ValueError):
            run_weekday = None
        if run_weekday is None or not 0 <= run_weekday <= 6:
            run_weekday = None
            notes.append(
                "league settings carry no waiver_day_of_week -- the weekly waiver run is not modelled "
                "and only a dropped player is shown on waivers"
            )
    if run_weekday is not None:
        run_time = _parse_run_time(weekly_run_time_et)
        if run_time is None:
            notes.append(
                f"waiver_status_source.weekly_run_time_et {weekly_run_time_et!r} is not HH:MM -- "
                f"using {DEFAULT_WEEKLY_RUN_TIME_ET}"
            )
            run_time = _parse_run_time(DEFAULT_WEEKLY_RUN_TIME_ET)
        on_day: list[Mapping] = []
        off_day: list[Mapping] = []
        for t in weekly_runs:
            same_day = utc_to_eastern(_from_ms(t["status_updated"])).weekday() == run_weekday
            (on_day if same_day else off_day).append(t)
        learned = _time_of_latest(on_day)
        if learned is not None:
            run_time = learned[0]
        stray = _time_of_latest(off_day)
        if stray is not None:
            notes.append(
                f"a claim on a player nobody dropped processed {local_clock(stray[1])}, not on "
                f"waiver_day_of_week {run_weekday} -- check the league's waiver day; the weekly run "
                f"is taken as {run_time:%H:%M} ET"
            )
        cycle_start, next_run = weekly_run_window(now, run_weekday, run_time)

    # --- Dropped players: on waivers until their clear date's run. -------
    drop_clear_time: time | None = None
    learned_drop = _time_of_latest(drop_clears)
    if learned_drop is not None:
        drop_clear_time = learned_drop[0]
    else:
        notes.append(
            "no processed waiver claim on a dropped player yet to learn that run's time from -- "
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
        clears = eastern_to_utc(datetime.combine(clear_date, drop_clear_time or time(23, 59)))
        if clears <= now:
            continue
        p = players.get(pid)
        name = sleeper_player_name(p) if p else ""
        if not name:
            notes.append(f"dropped Sleeper player_id {pid!r} has no name on file -- status unknown")
            continue
        waivers[normalize_name(name)] = PlayerAvailability(status=WAIVERS, clears_at=clears, dropped_at=dropped)

    kickoffs = _parse_kickoffs(kickoffs_et)
    if not kickoffs:
        notes.append("kickoff times unknown this run -- players whose game already started are not detected")

    return Availability(
        now=now, waivers=waivers, kickoffs=kickoffs, game_lock_hours=game_lock_hours, notes=notes,
        prior_kickoffs=_parse_kickoffs(prior_kickoffs_et), cycle_start=cycle_start, next_run=next_run,
    )


# --- What happened to MY claims at the run -----------------------------------


@dataclass(frozen=True)
class ClaimOutcome:
    add_name: str
    drop_name: str
    status: str  # Sleeper's own: "complete" | "failed"
    note: str  # Sleeper's `metadata.notes` on a failed claim, else ""
    processed_at: datetime

    def text(self) -> str:
        what = self.add_name + (f" (dropping {self.drop_name})" if self.drop_name else "")
        if self.status == "complete":
            return f"Your claim for {what}: processed"
        why = f" -- {self.note}" if self.note else ""
        return f"Your claim for {what}: {self.status}{why}"


def _name_for(players: Mapping[str, Mapping], pid: object) -> str:
    p = players.get(str(pid))
    return (sleeper_player_name(p) if p else "") or str(pid)


def claim_outcomes(
    transactions: Iterable[Mapping], roster_id: int | None, since: datetime | None, players: Mapping[str, Mapping],
) -> list[ClaimOutcome]:
    """Every waiver claim of `roster_id`'s that Sleeper has processed (won or
    lost) since `since`, oldest first -- what the Wednesday-morning check
    tells the manager about Tuesday's advice. Pure."""
    out: list[ClaimOutcome] = []
    for t in transactions:
        if t.get("type") != "waiver" or t.get("status") not in ("complete", "failed"):
            continue
        if roster_id is not None:
            try:
                mine = int(roster_id) in {int(r) for r in (t.get("roster_ids") or [])}
            except (TypeError, ValueError):
                mine = False
            if not mine:
                continue
        ts = t.get("status_updated") or t.get("created")
        if not ts:
            continue
        when = _from_ms(ts)
        if since is not None and when < since:
            continue
        out.append(ClaimOutcome(
            add_name=", ".join(_name_for(players, pid) for pid in (t.get("adds") or {})),
            drop_name=", ".join(_name_for(players, pid) for pid in (t.get("drops") or {})),
            status=str(t.get("status")),
            note=str((t.get("metadata") or {}).get("notes") or ""),
            processed_at=when,
        ))
    out.sort(key=lambda o: o.processed_at)
    return out
