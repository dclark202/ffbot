"""Player name normalization and matching.

FantasyPros board data and Yahoo roster data name the same player two
different ways (suffixes, punctuation, defense naming). This module bridges
them. ID matching (`match_board_to_yahoo`) is a strict, human-reviewed
cascade meant to run once before the draft; TUI search (`search`) is a
separate, permissive prefix matcher meant to resolve a few keystokes to a
player while the clock runs. Conflating the two is the mistake to avoid:
`difflib` ratio scoring is a poor fit for "type 4 letters and hit enter", and
prefix scoring is a poor fit for pre-draft ID reconciliation, where a wrong
silent match is worse than an unmatched player.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol, Sequence

# --- Defense naming -----------------------------------------------------
#
# FantasyPros writes "Ravens D/ST"; Yahoo writes the city ("Baltimore") with
# position DEF. This table is a closed, stable set of 32 franchises — the
# rare legitimate case for hardcoding rather than deriving.

NFL_TEAMS: dict[str, str] = {
    "arizona": "ARI", "cardinals": "ARI", "arizona cardinals": "ARI",
    "atlanta": "ATL", "falcons": "ATL", "atlanta falcons": "ATL",
    "baltimore": "BAL", "ravens": "BAL", "baltimore ravens": "BAL",
    "buffalo": "BUF", "bills": "BUF", "buffalo bills": "BUF",
    "carolina": "CAR", "panthers": "CAR", "carolina panthers": "CAR",
    "chicago": "CHI", "bears": "CHI", "chicago bears": "CHI",
    "cincinnati": "CIN", "bengals": "CIN", "cincinnati bengals": "CIN",
    "cleveland": "CLE", "browns": "CLE", "cleveland browns": "CLE",
    "dallas": "DAL", "cowboys": "DAL", "dallas cowboys": "DAL",
    "denver": "DEN", "broncos": "DEN", "denver broncos": "DEN",
    "detroit": "DET", "lions": "DET", "detroit lions": "DET",
    "green bay": "GB", "packers": "GB", "green bay packers": "GB",
    "houston": "HOU", "texans": "HOU", "houston texans": "HOU",
    "indianapolis": "IND", "colts": "IND", "indianapolis colts": "IND",
    "jacksonville": "JAX", "jaguars": "JAX", "jacksonville jaguars": "JAX",
    "kansas city": "KC", "chiefs": "KC", "kansas city chiefs": "KC",
    "las vegas": "LV", "raiders": "LV", "las vegas raiders": "LV",
    "oakland": "LV", "oakland raiders": "LV",
    "los angeles chargers": "LAC", "la chargers": "LAC", "chargers": "LAC",
    "los angeles rams": "LAR", "la rams": "LAR", "rams": "LAR",
    "miami": "MIA", "dolphins": "MIA", "miami dolphins": "MIA",
    "minnesota": "MIN", "vikings": "MIN", "minnesota vikings": "MIN",
    "new england": "NE", "patriots": "NE", "new england patriots": "NE",
    "new orleans": "NO", "saints": "NO", "new orleans saints": "NO",
    "new york giants": "NYG", "ny giants": "NYG", "giants": "NYG",
    "new york jets": "NYJ", "ny jets": "NYJ", "jets": "NYJ",
    "philadelphia": "PHI", "eagles": "PHI", "philadelphia eagles": "PHI",
    "pittsburgh": "PIT", "steelers": "PIT", "pittsburgh steelers": "PIT",
    "san francisco": "SF", "49ers": "SF", "san francisco 49ers": "SF",
    "seattle": "SEA", "seahawks": "SEA", "seattle seahawks": "SEA",
    "tampa bay": "TB", "buccaneers": "TB", "tampa bay buccaneers": "TB",
    "tennessee": "TEN", "titans": "TEN", "tennessee titans": "TEN",
    "washington": "WAS", "commanders": "WAS", "washington commanders": "WAS",
    "football team": "WAS", "washington football team": "WAS",
    "redskins": "WAS",
}

POSITION_ALIASES: dict[str, str] = {
    "DST": "DEF", "D/ST": "DEF", "D-ST": "DEF", "DEF": "DEF",
    "PK": "K", "K": "K",
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
}

_SUFFIX_TOKENS = frozenset({"jr", "sr", "ii", "iii", "iv"})
_STRIP_CHARS = str.maketrans("", "", ".'’,-")


def normalize_name(raw: str) -> str:
    """Collapse a display name to a stable matching key.

    NFKD-fold accents, lowercase, drop `. ' , -`, collapse whitespace, then
    drop a trailing generational suffix token. Bare "V" is deliberately not
    stripped — too likely to collide with a real one-letter-ending surname.
    """
    s = unicodedata.normalize("NFKD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().translate(_STRIP_CHARS)
    tokens = [t for t in s.split() if t]
    if tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens = tokens[:-1]
    return " ".join(tokens)


def normalize_position(raw: str) -> str:
    """Map a position string to its canonical form, stripping rank digits."""
    s = raw.strip().upper()
    s = "".join(c for c in s if not c.isdigit())
    return POSITION_ALIASES.get(s, s)


def defense_key(raw: str, team: str | None = None) -> str | None:
    """Canonical team abbreviation for a defense, or None if unrecognized.

    Tries the team field first (usually already an abbreviation or close to
    one), then falls back to parsing the defense's own display name (e.g.
    "Ravens D/ST" -> "ravens" -> BAL).
    """
    for candidate in (team, raw):
        if not candidate:
            continue
        key = candidate.strip().lower()
        key = key.replace(" d/st", "").replace(" dst", "").strip()
        if key.upper() in NFL_TEAMS.values():
            return key.upper()
        if key in NFL_TEAMS:
            return NFL_TEAMS[key]
    return None


def initial_key(normalized: str) -> str:
    """'justin jefferson' -> 'j jefferson'. Catches Josh/Joshua, Cam/Cameron."""
    tokens = normalized.split()
    if len(tokens) < 2:
        return normalized
    return f"{tokens[0][0]} {tokens[-1]}"


# --- ID matching (pre-draft, human-reviewed) --------------------------------


@dataclass(frozen=True)
class MatchResult:
    query: str
    matched_id: int | None
    matched_name: str | None
    confidence: str  # "exact" | "position" | "initial" | "fuzzy" | "none"
    alternatives: list[str] = field(default_factory=list)


class _YahooRow(Protocol):
    player_id: int


def match_board_to_yahoo(
    board_rows: Sequence[dict],
    yahoo_players: Sequence[dict],
    aliases: dict[str, str] | None = None,
    fuzzy_threshold: float = 0.88,
    fuzzy_margin: float = 0.04,
) -> list[MatchResult]:
    """Match each board row to a Yahoo player id.

    `board_rows` need `name`, `position`, and optionally `team`. `yahoo_players`
    need `player_id`, `name`, `position` (or `eligible_positions`), `team`.
    `aliases` maps a normalized board name straight to a normalized Yahoo
    name for cases the cascade cannot resolve automatically (config-driven,
    per repo convention).

    Every tier past exact position+team match is intended for human review —
    callers should surface non-"exact"/"position" confidences before relying
    on the ids, never during a live draft.
    """
    aliases = aliases or {}

    def yahoo_pos(row: dict) -> str:
        if "position" in row:
            return normalize_position(row["position"])
        eligible = row.get("eligible_positions") or []
        return normalize_position(eligible[0]) if eligible else ""

    yahoo_index: list[dict] = []
    for row in yahoo_players:
        name = normalize_name(row.get("name", ""))
        pos = yahoo_pos(row)
        team = (row.get("team") or "").strip().upper()
        if pos == "DEF":
            team = defense_key(row.get("name", ""), row.get("team")) or team
            # Defense display names vary wildly (city, mascot, "D/ST" suffix)
            # while the team is a closed 32-value set — match on that instead.
            if team:
                name = team.lower()
        yahoo_index.append(
            {
                "player_id": row["player_id"],
                "name": name,
                "pos": pos,
                "team": team,
                "display": row.get("name", ""),
            }
        )

    by_name_pos_team: dict[tuple[str, str, str], list[dict]] = {}
    by_name_pos: dict[tuple[str, str], list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    by_initial_pos_team: dict[tuple[str, str, str], list[dict]] = {}
    for row in yahoo_index:
        by_name_pos_team.setdefault((row["name"], row["pos"], row["team"]), []).append(row)
        by_name_pos.setdefault((row["name"], row["pos"]), []).append(row)
        by_name.setdefault(row["name"], []).append(row)
        by_initial_pos_team.setdefault(
            (initial_key(row["name"]), row["pos"], row["team"]), []
        ).append(row)

    results: list[MatchResult] = []
    for board_row in board_rows:
        raw_name = board_row.get("name", "")
        pos = normalize_position(board_row.get("position", ""))
        team = (board_row.get("team") or "").strip().upper()
        name = normalize_name(raw_name)
        if pos == "DEF":
            team = defense_key(raw_name, board_row.get("team")) or team
            if team:
                name = team.lower()
        if name in aliases:
            name = normalize_name(aliases[name])

        result = _resolve_one(
            raw_name, name, pos, team,
            by_name_pos_team, by_name_pos, by_name, by_initial_pos_team,
            yahoo_index, fuzzy_threshold, fuzzy_margin,
        )
        results.append(result)

    return results


def _resolve_one(
    raw_name: str,
    name: str,
    pos: str,
    team: str,
    by_name_pos_team: dict,
    by_name_pos: dict,
    by_name: dict,
    by_initial_pos_team: dict,
    yahoo_index: list[dict],
    fuzzy_threshold: float,
    fuzzy_margin: float,
) -> MatchResult:
    # Tier 1: name + position + team.
    hits = by_name_pos_team.get((name, pos, team), [])
    if len(hits) == 1:
        return MatchResult(raw_name, hits[0]["player_id"], hits[0]["display"], "exact")

    # Tier 2: name + position.
    hits = by_name_pos.get((name, pos), [])
    if len(hits) == 1:
        return MatchResult(raw_name, hits[0]["player_id"], hits[0]["display"], "position")

    # Tier 3: name alone, only if unambiguous.
    hits = by_name.get(name, [])
    if len(hits) == 1:
        return MatchResult(raw_name, hits[0]["player_id"], hits[0]["display"], "position")

    # Tier 4: first-initial + last name + position + team.
    hits = by_initial_pos_team.get((initial_key(name), pos, team), [])
    if len(hits) == 1:
        return MatchResult(raw_name, hits[0]["player_id"], hits[0]["display"], "initial")

    # Tier 5: fuzzy, same position only, must beat runner-up by a margin.
    same_pos = [row for row in yahoo_index if row["pos"] == pos]
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, name, row["name"]).ratio(), row)
            for row in same_pos
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    if scored:
        best_score, best_row = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= fuzzy_threshold and (best_score - runner_up) >= fuzzy_margin:
            return MatchResult(
                raw_name, best_row["player_id"], best_row["display"], "fuzzy",
                alternatives=[r["display"] for _, r in scored[1:4]],
            )

    alternatives = [row["display"] for _, row in scored[:4]] if scored else []
    return MatchResult(raw_name, None, None, "none", alternatives=alternatives)


# --- TUI search (hot path, permissive) --------------------------------------


class Searchable(Protocol):
    key: str  # normalized name
    name: str  # display name


def _score(query_norm: str, entry_key: str) -> int:
    """Higher is better; 0 means no match. Cheap prefix-family scoring."""
    if not query_norm:
        return 0
    if entry_key == query_norm:
        return 100
    if entry_key.startswith(query_norm):
        return 90
    tokens = entry_key.split()
    if any(t.startswith(query_norm) for t in tokens):
        return 70
    if initial_key(entry_key).replace(" ", "").startswith(query_norm.replace(" ", "")):
        return 50
    # Subsequence: every character of the query appears in order.
    it = iter(entry_key)
    if all(c in it for c in query_norm):
        return 20
    return 0


def search_scored(query: str, entries: Sequence[Searchable]) -> list[tuple[int, Searchable]]:
    """Like `search`, but keeps each entry's match score for disambiguation.

    Scores against `normalize_name(entry.name)`, not `entry.key` — a key
    like `board.BoardPlayer.key` is often a composite
    (`"<name>:<position>"`), and scoring against that directly would let the
    position suffix contaminate token/initial/subsequence matching.
    """
    query_norm = normalize_name(query)
    scored = [
        (_score(query_norm, normalize_name(e.name)), i, e) for i, e in enumerate(entries)
    ]
    scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(score, e) for score, _, e in scored]


def search(query: str, entries: Sequence[Searchable]) -> list[Searchable]:
    """Rank `entries` by how well they match `query`, best first.

    Intended for a live TUI: a few characters should surface the intended
    player near the top. Ties are broken by the caller's input order, which
    should already be board rank (best players first) so a short, ambiguous
    query still surfaces the most relevant candidate.
    """
    return [e for _, e in search_scored(query, entries)]
