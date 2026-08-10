"""nflverse <-> board/roster name crosswalk for historical replay.

Two join problems, same as the live paths this mirrors: a fast, exact
per-player key for the common case (`actuals_key`/`index_by_key`, the
`ffbot.board._board_key` convention applied to nflverse's own name/position
fields), and a strict, human-reviewable fallback cascade for validating
coverage (`match_actuals`, a thin wrapper around `names.match_board_to_yahoo`
— NOT the permissive TUI `search`/`search_scored`, since a wrong silent
match here poisons ground truth rather than just costing a few keystrokes).

This module also owns team-relocation identity: nflverse's historical rows
use the abbreviation that was correct *at the time* (OAK, SD, STL), while
`data/stadiums.yml` and `league_rosters.yml` only know the current one.
"""

from __future__ import annotations

from typing import Sequence

from ..names import MatchResult, match_board_to_yahoo, normalize_name, normalize_position

# Franchises that changed city/abbreviation within nflverse's data range,
# mapped old -> current. Applied before any team-keyed lookup so e.g. a 2015
# Chargers box score (SD) and a 2025 one (LAC) resolve to one identity.
# Deliberately narrow (see `ffbot.names.NFL_TEAMS` for the full closed set of
# 32 franchises) — this table only needs the abbreviations that actually
# changed, not every team.
TEAM_RELOCATIONS: dict[str, str] = {
    "OAK": "LV",    # Raiders: Oakland -> Las Vegas, 2020
    "SD": "LAC",    # Chargers: San Diego -> Los Angeles, 2017
    "STL": "LAR",   # Rams: St. Louis -> Los Angeles, 2016
    "LA": "LAR",    # nflverse has briefly used bare "LA" for the Rams
    "WSH": "WAS",   # Washington's own historical abbreviation is inconsistent
}


def canonical_team(raw: str | None) -> str:
    """Map a (possibly historical) team abbreviation onto its current
    franchise identity. Empty/unrecognized input passes through unchanged —
    this is a relocation table, not a validator."""
    t = (raw or "").strip().upper()
    return TEAM_RELOCATIONS.get(t, t)


def actuals_key(name: str, position: str) -> str:
    """`f"{normalized_name}:{position}"` — the same convention as
    `board._board_key`/`BoardPlayer.key`, so a historical stats row and a
    board row join with a plain dict lookup in the common case."""
    return f"{normalize_name(name)}:{normalize_position(position)}"


def index_by_key(
    rows: Sequence[dict],
    name_field: str = "player_display_name",
    position_field: str = "position",
) -> dict[str, dict]:
    """`{actuals_key: row}` for a list of nflverse player rows.

    A later row with a colliding key overwrites an earlier one — within one
    season+week nflverse shouldn't produce duplicate (name, position) pairs,
    but this doesn't raise if it ever does; callers that care should dedupe
    upstream and check the input length against the output length.
    """
    out: dict[str, dict] = {}
    for row in rows:
        name = row.get(name_field) or ""
        pos = row.get(position_field) or ""
        if not name or not pos:
            continue
        out[actuals_key(name, pos)] = row
    return out


def match_actuals(
    actual_rows: Sequence[dict],
    target_rows: Sequence[dict],
    name_field: str = "player_display_name",
    position_field: str = "position",
    team_field: str = "team",
    aliases: dict[str, str] | None = None,
) -> list[MatchResult]:
    """Strict-cascade match of `target_rows` (board/roster-shaped: `name`,
    `position`, optional `team`) against `actual_rows` (nflverse-shaped).

    Used by `scripts/history_check.py` to report per-season match coverage,
    and as a fallback wherever `index_by_key`'s exact join misses (a
    normalization mismatch, not a real absence). Reuses
    `names.match_board_to_yahoo`'s cascade rather than reimplementing it —
    same reasoning as that function's own docstring: a wrong silent match on
    ground-truth data is worse than an unmatched player, so this is
    deliberately not the permissive TUI matcher.
    """
    synthetic: list[dict] = []
    for i, row in enumerate(actual_rows):
        name = row.get(name_field) or ""
        if not name:
            continue
        synthetic.append(
            {
                "player_id": i,
                "name": name,
                "position": row.get(position_field) or "",
                "team": canonical_team(row.get(team_field)),
            }
        )

    board_like = [
        {
            "name": row.get("name", ""),
            "position": row.get("position", ""),
            "team": canonical_team(row.get("team")),
        }
        for row in target_rows
    ]
    return match_board_to_yahoo(board_like, synthetic, aliases=aliases)


def coverage_summary(matches: Sequence[MatchResult]) -> dict[str, float]:
    """`{"matched": n, "total": n, "pct": 0-100}` — the number
    `scripts/history_check.py` prints per season; a season below ~98% is a
    real problem to investigate, not a number to accept (see
    docs/BACKTEST.md's acceptance criteria)."""
    total = len(matches)
    matched = sum(1 for m in matches if m.matched_id is not None)
    pct = (100.0 * matched / total) if total else 0.0
    return {"matched": matched, "total": total, "pct": pct}
