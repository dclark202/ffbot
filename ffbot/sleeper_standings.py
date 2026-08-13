"""Live league standings from Sleeper — the sibling of `sleeper_roster.py`,
applied to `denial.py`'s data requirement instead of roster identity.

`denial_weight`/`denial_opponent_boost`/`denial_seed_window` and
`season.matchup_variance_weight` are all exact no-ops without
`league.yml`'s `teams:`/`my_team`/`my_opponent`/`week` block populated,
regardless of their own weight — this module is what feeds it when nobody
has hand-transcribed it. `league.yml`'s SCORING rules (passing/rushing/
receiving/kicking/defense) are never touched here — only the standings
block. A hand-curated `teams:` entry in `league.yml` always wins outright
over the live-derived one for that team, the same precedence
`weekly/week-NN.yml` has over live projections.

Two things Sleeper's API does not expose directly, both approximated
honestly rather than silently invented:

- **Seed** is not a field Sleeper returns — it is derived by ranking every
  roster by `(wins desc, points-for desc)`, the standard tiebreak most
  redraft leagues use absent a division/head-to-head system this module has
  no way to see. A league with divisions or unusual tiebreak rules should
  keep hand-curating `teams:` for anyone this misranks.
- **Eliminated** is always `False` — computing genuine playoff elimination
  needs a season-long simulation (remaining schedule, magic number) this
  single live snapshot cannot supply. `lock_eliminated_teams`-gated logic
  that depends on it stays exactly as conservative as before this module
  existed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .config import LeagueScoring, TeamStanding
from .sleeper.client import SleeperClient


class StandingsSourceError(ValueError):
    """Live standings could not be resolved."""


def _team_name(roster: dict, team_name_by_owner: dict[str, str]) -> str:
    owner_id = roster.get("owner_id")
    return team_name_by_owner.get(owner_id) or f"roster {roster.get('roster_id')}"


def fetch_standings(
    client: SleeperClient,
    league_id: str,
    week: int,
    my_roster_id: Optional[int] = None,
) -> tuple[list[TeamStanding], str, str]:
    """`(teams, my_team_name, my_opponent_name)` derived live from Sleeper's
    rosters/users/matchups endpoints. `my_team_name`/`my_opponent_name` are
    `""` when `my_roster_id` is `None` or unresolvable against this week's
    matchups — the same "no-op when unknown" contract `league.yml`'s own
    `my_team`/`my_opponent` defaults already have.
    """
    rosters = client.rosters(league_id)
    users = client.league_users(league_id)

    team_name_by_owner: dict[str, str] = {}
    for u in users:
        meta = u.get("metadata") or {}
        owner_id = u.get("user_id")
        if owner_id:
            team_name_by_owner[owner_id] = meta.get("team_name") or u.get("display_name") or owner_id

    def _wins(r: dict) -> int:
        return int((r.get("settings") or {}).get("wins") or 0)

    def _points(r: dict) -> float:
        return float((r.get("settings") or {}).get("fpts") or 0)

    ranked = sorted(rosters, key=lambda r: (-_wins(r), -_points(r)))
    seed_by_roster_id = {r.get("roster_id"): i + 1 for i, r in enumerate(ranked)}
    team_name_by_roster_id = {r.get("roster_id"): _team_name(r, team_name_by_owner) for r in rosters}

    teams: list[TeamStanding] = []
    for r in rosters:
        settings = r.get("settings") or {}
        wins = int(settings.get("wins") or 0)
        losses = int(settings.get("losses") or 0)
        ties = int(settings.get("ties") or 0)
        record = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
        teams.append(
            TeamStanding(
                name=team_name_by_roster_id[r.get("roster_id")],
                record=record,
                seed=seed_by_roster_id.get(r.get("roster_id")),
                waiver_priority=settings.get("waiver_position"),
                eliminated=False,  # not computable from a single live snapshot -- see module docstring
            )
        )

    my_team_name = ""
    my_opponent_name = ""
    if my_roster_id is not None:
        my_team_name = team_name_by_roster_id.get(my_roster_id, "")
        matchups = client.matchups(league_id, week)
        my_matchup_id = next(
            (m.get("matchup_id") for m in matchups if m.get("roster_id") == my_roster_id), None,
        )
        if my_matchup_id is not None:
            opp_roster_id = next(
                (
                    m.get("roster_id") for m in matchups
                    if m.get("matchup_id") == my_matchup_id and m.get("roster_id") != my_roster_id
                ),
                None,
            )
            if opp_roster_id is not None:
                my_opponent_name = team_name_by_roster_id.get(opp_roster_id, "")

    return teams, my_team_name, my_opponent_name


def merge_standings(
    league: LeagueScoring,
    teams: list[TeamStanding],
    my_team_name: str,
    my_opponent_name: str,
    week: int,
) -> LeagueScoring:
    """A copy of `league` with live-derived standings merged UNDER whatever
    it already has. Whole-entry precedence per team name (a hand-curated
    `TeamStanding` is small and easy to fully re-type — `denial.py`'s own
    docstring calls it "curated, not generated" — so a partial per-field
    merge would just invite silent drift between the two sources for one
    team). `my_team`/`my_opponent`/`week` are filled only when `league.yml`
    left them unset (`""`/`None`), never overriding a hand-typed value.
    """
    existing_names = {t.name for t in league.teams}
    merged_teams = list(league.teams) + [t for t in teams if t.name not in existing_names]
    return replace(
        league,
        teams=merged_teams,
        my_team=league.my_team or my_team_name,
        my_opponent=league.my_opponent or my_opponent_name,
        week=league.week if league.week is not None else week,
    )
