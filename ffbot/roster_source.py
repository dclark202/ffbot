"""Where the in-season roster comes from — the M1 baseline route.

`roster.yml` is deliberately dumb: a list of names, nothing else. Slots
aren't tracked because `lineup.optimize()` decides them fresh every run —
the whole point of the optimizer existing is that you never have to think
about which bench spot a player is in. This is what keeps weekly upkeep to
"add a name after a trade," not "keep a lineup file in sync."

The harder problem is turning those bare names into `models.Player` objects
with real projections, byes, and teams — that data has to come from
somewhere, and the FantasyPros weekly export is that somewhere for M1, using
`board.read_fantasypros` exactly as-is (it was never draft-specific; it
parses one CSV of name/team/position/points, whether the URL says
`week=draft` or `week=7`).

A roster name that fails to match anything is the dangerous silent failure —
a typo that makes a real rostered player simply vanish from every
recommendation, which reads as "no news this week" rather than "this is
broken." So a miss here always surfaces, never disappears quietly. Compare
`ffbot.intel`'s unmatched-name handling — same shape, same reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import yaml

from .board import Board, _normalize_sources, apply_league_scoring, read_fantasypros
from .config import LeagueScoring
from .models import BENCH, Player
from .names import normalize_name, search_scored


class RosterError(ValueError):
    """roster.yml exists but could not be understood."""


@dataclass(frozen=True)
class RosterMatch:
    """One roster.yml name, resolved (or not) against a weekly projections source."""

    query: str
    player: Player | None
    suggestion: str | None = None  # closest available name, when unmatched


@dataclass(frozen=True)
class RosterEntry:
    """One roster.yml entry.

    A bare name is still the common case — see the module docstring on why
    low weekly upkeep matters — so every field but `name` is optional and
    defaults to a no-op. The mapping form exists only for the one or two
    players who need it (undroppable, keeper protection, a denial hold).
    """

    name: str
    undroppable: bool = False  # Yahoo's Can't Cut List, or any hard "never drop"
    keeper_round: int | None = None  # feeds Player.draft_round -> protect_draft_rounds
    acquired: str = ""  # "draft" | "waiver" | "trade" | "" -- informational
    note: str = ""  # your own reminder; not shown anywhere yet
    blocking: bool = False  # held to deny a rival, not to start -- see Player.blocking


def parse_roster_entry(raw) -> RosterEntry:
    """Bare string -> `RosterEntry(name=...)`. Mapping -> the full entry.

    The mapping spelling is `- name: Travis Kelce`, not `- Travis Kelce:
    {...}` — the single-key-mapping form breaks on any name containing a
    colon and produces a worse error when it does, for a purely cosmetic
    gain.
    """
    if isinstance(raw, RosterEntry):
        return raw
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            raise RosterError("roster.yml: a 'players' entry is blank")
        return RosterEntry(name=name)
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RosterError(f"roster.yml: a mapping entry needs a 'name' key, got {raw!r}")
        keeper_round = raw.get("keeper_round")
        return RosterEntry(
            name=name,
            undroppable=bool(raw.get("undroppable", False)),
            keeper_round=int(keeper_round) if keeper_round is not None else None,
            acquired=str(raw.get("acquired") or ""),
            note=str(raw.get("note") or ""),
            blocking=bool(raw.get("blocking", False)),
        )
    raise RosterError(f"roster.yml: a 'players' entry must be a name or a mapping, got {raw!r}")


def load_roster_entries(path: str | Path = "roster.yml") -> list[RosterEntry]:
    """Parse roster.yml into `RosterEntry` objects, in file order.

    Order is preserved (not sorted) so a rendered report can match the order
    you keep your own mental roster in, if that matters to you. A blank
    bare-string entry is silently skipped (the YAML equivalent of a stray
    blank line); a mapping entry with no `name:` is a real error and raises.
    """
    p = Path(path)
    if not p.exists():
        raise RosterError(
            f"{p} not found. Copy roster.example.yml to {p} and fill in your "
            "drafted/current roster — see QUICKSTART.md."
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RosterError(f"{p}: expected a mapping at the top level")

    raw_entries = raw.get("players")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RosterError(f"{p}: 'players' must be a non-empty list of names")

    entries: list[RosterEntry] = []
    for item in raw_entries:
        if isinstance(item, str) and not item.strip():
            continue
        entries.append(parse_roster_entry(item))
    return entries


def load_roster_names(path: str | Path = "roster.yml") -> list[str]:
    """Names only, in file order — a thin wrapper for callers that don't need
    the mapping-form metadata. See `load_roster_entries`."""
    return [e.name for e in load_roster_entries(path)]


def load_weekly_projection_rows(
    csv_paths: Sequence, league: LeagueScoring | None = None
) -> list[dict]:
    """Load one or more weekly FantasyPros exports into name/team/position/points rows.

    Entries may be plain paths or `{path, position}` mappings, exactly like
    `draft.board_csv` — the weekly single-position exports (qb/k/dst for a
    given week) have the identical missing-POS-column problem the draft
    board already solved, so this reuses that resolution rather than
    re-deriving it. Each source is read independently and simply
    concatenated (not merged by name like the draft board does) — weekly
    exports are typically one file per position already split cleanly, and
    duplicate rows across sources are resolved at match time by taking the
    first hit, same as everywhere else in this codebase that layers CSVs.

    `league` gets the identical treatment `board.load_board` gives it — see
    `board.apply_league_scoring`. Skipping this on the weekly path while the
    season board is scored under `league.yml` would leave two different
    scales for the same player, resolved by whichever source happens to
    match first in `match_roster`; that's the bug this parameter exists to
    prevent, not an optional nicety.
    """
    rows: list[dict] = []
    for src in _normalize_sources(csv_paths):
        rows.extend(read_fantasypros(src.path, default_position=src.position))
    rows = [r for r in rows if r.get("points") is not None and r.get("position")]
    apply_league_scoring(rows, league)
    return rows


def load_lineup_state(path: str | Path) -> dict[str, str]:
    """Last known `{normalized name: selected_position}`, or {} if never saved.

    This is what makes "no changes needed" a real, common answer instead of
    a fiction. Without Yahoo, nothing tells this tool what's currently
    started — every `Player` roster_source builds defaults to BENCH — so
    without a remembered state, `lineup.optimize()`'s move list would show
    every single starter as a fresh "BN -> QB" move, every single week, even
    when nothing has actually changed since the last run. Seeding
    `selected_position` from the previous run's own output is what lets the
    minimal-move machinery in `lineup.py` (which exists exactly for this) do
    its job across runs, not just within one.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return {normalize_name(str(k)): str(v) for k, v in raw.items()}


def save_lineup_state(path: str | Path, plan) -> None:
    """Persist each player's slot after a run, for `load_lineup_state` to
    seed next time.

    Takes the `LineupPlan` itself, not a flat player list — `optimize()`
    never mutates a `Player`'s `.selected_position` to reflect where it just
    placed them (the new slot only exists as the first element of each
    `plan.assignments` tuple, by design: `optimize()` is a pure function and
    writing the change is the caller's job). Reading `.selected_position`
    off the post-optimize players would silently save everyone's OLD
    position and defeat the entire point of this function.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = {normalize_name(pl.name): slot for slot, pl in plan.assignments}
    state.update({normalize_name(pl.name): BENCH for pl in plan.bench})
    p.write_text(yaml.safe_dump(state, sort_keys=True), encoding="utf-8")


def apply_lineup_state(players: Sequence[Player], state: dict[str, str]) -> list[Player]:
    """Seed each player's `selected_position` from remembered state, where any
    exists. A name with no remembered state (a new add) stays at the `Player`
    default (BENCH) — the only case where "everything moves" is honest,
    because it genuinely is the first time this tool has seen them.
    """
    out = []
    for p in players:
        remembered = state.get(normalize_name(p.name))
        out.append(replace(p, selected_position=remembered) if remembered else p)
    return out


def season_board_rows(board: Board, weeks_remaining: int) -> list[dict]:
    """Season-long board entries, rescaled to a rough per-week baseline.

    The fallback source when no fresh weekly projection CSV has been
    downloaded — the season board is already kept current by the normal
    `/intel-refresh` cadence, so this is what lets a weekly report run every
    single week without requiring a brand-new download each time. It is a
    crude approximation (flat division ignores bye weeks already used up,
    that week's specific matchup, everything `week.py`'s own status/weather/
    vegas/spice adjustments exist to layer on top of) — real weekly
    projections, when available, are always the better source; `load_roster`
    treats them as authoritative and only falls back to this for names they
    don't cover.
    """
    weeks_remaining = max(1, weeks_remaining)
    return [
        {
            "name": bp.name,
            "team": bp.team,
            "position": bp.position,
            "points": bp.points / weeks_remaining,
            "bye": bp.bye_week,
        }
        for bp in board.players
    ]


def match_roster(
    names: Sequence[str | RosterEntry], rows: Sequence[dict]
) -> list[RosterMatch]:
    """Resolve each roster name (or `RosterEntry`) against the loaded projection rows.

    Matching is exact-on-normalized-name first (cheap, and right the vast
    majority of the time), falling back to the same permissive search the
    live draft TUI uses for a suggestion when nothing matches exactly —
    deliberately a suggestion, not an auto-match: silently guessing which
    of two same-surname players you meant is exactly the kind of wrong-player
    substitution this module exists to prevent.

    `names` accepts bare strings (wrapped into a no-metadata `RosterEntry`)
    or `RosterEntry` objects directly — `load_roster` passes the latter so
    `undroppable`/`keeper_round`/`blocking` reach the resulting `Player`.
    """
    by_name: dict[str, dict] = {}
    for row in rows:
        key = normalize_name(str(row["name"]))
        by_name.setdefault(key, row)  # first source wins, matching board.py's convention

    class _Row:
        """Adapts a projection row to names.search_scored's Searchable protocol."""

        __slots__ = ("key", "name", "row")

        def __init__(self, row: dict):
            self.name = str(row["name"])
            self.key = normalize_name(self.name)
            self.row = row

    searchable = [_Row(r) for r in rows]

    out: list[RosterMatch] = []
    for uid, item in enumerate(names, start=1):
        entry = item if isinstance(item, RosterEntry) else RosterEntry(name=str(item))
        row = by_name.get(normalize_name(entry.name))
        if row is not None:
            out.append(RosterMatch(query=entry.name, player=_row_to_player(uid, row, entry)))
            continue

        hits = search_scored(entry.name, searchable)
        suggestion = hits[0][1].name if hits else None
        out.append(RosterMatch(query=entry.name, player=None, suggestion=suggestion))
    return out


def _row_to_player(uid: int, row: dict, entry: RosterEntry | None = None) -> Player:
    entry = entry if entry is not None else RosterEntry(name=str(row["name"]))
    return Player(
        player_id=uid,
        name=str(row["name"]),
        eligible_positions=[str(row["position"])],
        team=str(row.get("team") or ""),
        bye_week=row.get("bye"),
        projected_points=row.get("points"),
        is_undroppable=entry.undroppable,
        draft_round=entry.keeper_round,
        blocking=entry.blocking,
    )


def load_roster(
    csv_paths: Sequence[str | Path],
    roster_path: str | Path = "roster.yml",
    fallback_rows: Sequence[dict] = (),
    league: LeagueScoring | None = None,
) -> tuple[list[Player], list[RosterMatch]]:
    """The one call `scripts/week_report.py` needs: names -> resolved Players.

    `fallback_rows` (typically `season_board_rows`, already league-scored via
    `load_board`) fills in any name the weekly CSVs don't cover —
    `match_roster` already resolves same-name conflicts first-source-wins, so
    listing weekly rows before the fallback here is what makes "prefer real
    weekly data, degrade to the season board" happen for free, with no extra
    merge logic. `league` scores the weekly CSVs to match — see
    `load_weekly_projection_rows`.

    Returns `(players, unmatched)` — `players` covers only names that
    actually resolved, so a caller can run with a partial roster rather than
    failing outright, but `unmatched` must always be surfaced to the user;
    a silently short roster is the exact failure mode this module exists to
    avoid.
    """
    entries = load_roster_entries(roster_path)
    rows = load_weekly_projection_rows(csv_paths, league) if csv_paths else []
    rows = list(rows) + list(fallback_rows)
    matches = match_roster(entries, rows)
    players = [m.player for m in matches if m.player is not None]
    unmatched = [m for m in matches if m.player is None]
    return players, unmatched
