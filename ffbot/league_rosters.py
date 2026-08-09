"""Other teams' rosters — league-wide free-agent-pool correctness and,
eventually, tactical denial.

`week.waiver_candidates` has always built its free-agent pool as *board
minus your own roster* — it has no idea the other 11 teams exist, so it
will happily recommend adding a player who is already on someone else's
roster. This module is what fixes that: `LeagueRosters.rostered_names()`
gives the full-league exclusion set, imported via
`scripts/import_league_rosters.py`.

Deliberately a separate file from `league.yml` (small, curated standings,
hand-edited): this one is generated wholesale by the importer and should
never be hand-edited, same reasoning that keeps `draft/intel.yml` and
`weekly/week-NN.yml` apart from `config.yml`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
