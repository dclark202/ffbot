"""Other teams' rosters — league-wide free-agent-pool correctness and,
eventually, tactical denial.

`week.waiver_candidates` has always built its free-agent pool as *board
minus your own roster* — it has no idea the other 11 teams exist, so it
will happily recommend adding a player who is already on someone else's
roster. This module is what fixes that: `LeagueRosters.rostered_names()`
gives the full-league exclusion set, populated either by
`scripts/import_league_rosters.py` (a one-off snapshot, `--paste`/`--live`,
written to `league_rosters.yml`) or, when `league_rosters_source.source ==
"sleeper"`, fetched fresh by `fetch_league_rosters` on every
`report.load_everything` call — see that config block's docstring.

Deliberately a separate file from `league.yml` (small, curated standings,
hand-edited): a file written by either route is generated wholesale and
should never be hand-edited, same reasoning that keeps `draft/intel.yml` and
`weekly/week-NN.yml` apart from `config.yml`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .names import normalize_name


@dataclass
class LeagueRosters:
    week: int | None = None
    generated: str = ""
    source: str = ""  # "paste" | "chrome" | "api"
    teams: dict[str, list[str]] = field(default_factory=dict)  # team name -> display names
    unmatched: list[str] = field(default_factory=list)  # "Team: 'Name' (did you mean ...?)"

    def rostered_names(self) -> set[str]:
        """Every normalized name rostered by ANY team in this file."""
        return {normalize_name(n) for names in self.teams.values() for n in names}


def load_league_rosters(path: str | Path = "league_rosters.yml") -> LeagueRosters:
    """Missing file -> an empty, inert `LeagueRosters` — same
    missing-file-is-a-no-op contract as `league.yml`/`intel.yml`: nothing
    downstream fails, the free-agent pool just isn't corrected yet.
    """
    p = Path(path)
    if not p.exists():
        return LeagueRosters()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return LeagueRosters()
    teams_raw = raw.get("teams") or {}
    teams = {
        str(team): [str(n) for n in (names or [])]
        for team, names in teams_raw.items()
    }
    return LeagueRosters(
        week=raw.get("week"),
        generated=str(raw.get("generated") or ""),
        source=str(raw.get("source") or ""),
        teams=teams,
        unmatched=[str(u) for u in (raw.get("unmatched") or [])],
    )


def build_teams_from_sleeper(
    rosters: list[dict], league_users: list[dict], players: dict[str, dict]
) -> tuple[dict[str, list[str]], list[str]]:
    """Sleeper's own `rosters`/`users`/`players` endpoints -> `{team:
    [display names]}`, via an exact `player_id` join instead of fuzzy name
    matching against a pasted dump — there is no realistic "unmatched" case
    here, only a `player_id` the players dump doesn't (yet) recognize, which
    is rare and reported the same way as a paste-route miss (never silently
    dropped).

    Pure (no network) — moved here from `scripts/import_league_rosters.py`
    so both that one-off script and `fetch_league_rosters` below (the
    per-run live path) share one join, rather than two copies drifting
    apart.
    """
    team_name_by_owner: dict[str, str] = {}
    for u in league_users:
        meta = u.get("metadata") or {}
        owner_id = u.get("user_id")
        if owner_id:
            team_name_by_owner[owner_id] = meta.get("team_name") or u.get("display_name") or owner_id

    teams: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for roster in rosters:
        owner_id = roster.get("owner_id")
        team = team_name_by_owner.get(owner_id) or f"roster {roster.get('roster_id')}"
        names: list[str] = []
        for player_id in roster.get("players") or []:
            p = players.get(player_id)
            if p is None:
                unmatched.append(f"{team}: unknown Sleeper player_id {player_id!r}")
                continue
            name = p.get("full_name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
            if not name:
                unmatched.append(f"{team}: Sleeper player_id {player_id!r} has no name on file")
                continue
            names.append(name)
        teams[team] = names
    return teams, unmatched


def fetch_league_rosters(
    client,
    league_id: str,
    players_dump: dict[str, dict],
    week: Optional[int] = None,
    ttl_minutes: Optional[float] = None,
) -> LeagueRosters:
    """The live analog of `load_league_rosters` — every team's real roster,
    fetched fresh via `client.rosters()`/`client.league_users()` and joined
    through `build_teams_from_sleeper`. `client` is duck-typed (no
    `SleeperClient` import here, matching every other Sleeper-touching
    module in this repo that keeps its pure logic importable offline); the
    caller (`report.load_everything`) supplies a real one behind a lazy
    import and a `SleeperFetchError` try/except.

    `players_dump` (`SleeperClient.players()`) is passed in rather than
    fetched here — the caller usually already holds a fresh copy from the
    roster-identity branch of the same run, and this join must never pay
    for a second ~5MB download in the same `load_everything` call.
    """
    kwargs = {} if ttl_minutes is None else {"ttl_minutes": ttl_minutes}
    rosters = client.rosters(league_id, **kwargs)
    league_users = client.league_users(league_id)
    teams, unmatched = build_teams_from_sleeper(rosters, league_users, players_dump)
    return LeagueRosters(
        week=week,
        generated=datetime.date.today().isoformat(),
        source="api",
        teams=teams,
        unmatched=unmatched,
    )
