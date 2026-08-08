"""The frozen pre-draft board: CSV loading, replacement level, VOR, tiers.

Everything here runs once, before the clock starts, and is then treated as
read-only for the rest of the draft — see `ffbot.draft` for the live,
roster-aware valuation built on top of it.

Replacement level is *derived*, not assumed. Scale the league's starting
slots by `num_teams` and run `lineup.optimize()` over the whole pool: the set
it returns is the exact aggregate optimal starting lineup across all teams
(a transversal matroid on a `T`-scaled slot multiset decomposes into `T`
per-team optima, so the aggregate optimum equals their sum). Counting
players per position in that result gives the replacement rank with no
hand-tuned "flex share" table — superflex and other exotic slots fall out of
the same math for free.
"""

from __future__ import annotations

import csv
import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Sequence

from .config import Config
from .lineup import optimize
from .models import BENCH, Player, slot_accepts, starting_slots
from .names import normalize_name, normalize_position

# --- CSV loading -------------------------------------------------------
#
# FantasyPros exports differ by product (rankings / ADP / projections).
# Sniff headers rather than assuming one shape.

COLUMN_ALIASES: dict[str, frozenset[str]] = {
    "name": frozenset({"PLAYER NAME", "PLAYER", "NAME"}),
    "team": frozenset({"TEAM", "TM"}),
    "position": frozenset({"POS", "POSITION"}),
    "bye": frozenset({"BYE WEEK", "BYE"}),
    "points": frozenset({"FPTS", "POINTS", "PROJECTED POINTS", "FANTASY POINTS"}),
    "adp": frozenset({"AVG", "ADP"}),
    "adp_stdev": frozenset({"STD DEV", "STDEV"}),
    "rank": frozenset({"RK", "RANK"}),
    "tier": frozenset({"TIERS", "TIER"}),
}

_NUMERIC_FLOAT_FIELDS = frozenset({"points", "adp", "adp_stdev"})
_NUMERIC_INT_FIELDS = frozenset({"bye", "rank", "tier"})


def _normalize_header(h: str) -> str:
    return " ".join(h.strip().upper().replace(".", "").split())


def _parse_field(field_name: str, raw: str | None) -> float | int | str | None:
    if raw is None:
        return None
    val = raw.strip()
    if val == "":
        return None
    if field_name in _NUMERIC_FLOAT_FIELDS:
        try:
            return float(val.replace(",", ""))
        except ValueError:
            return None
    if field_name in _NUMERIC_INT_FIELDS:
        try:
            return int(float(val.replace(",", "")))
        except ValueError:
            return None
    return val


def read_fantasypros(path: str | Path) -> list[dict]:
    """Parse one FantasyPros CSV export into rows with canonical field names.

    Handles the UTF-8 BOM FantasyPros downloads carry, thousands separators
    in point totals, and the differing header sets across their rankings,
    ADP, and projections exports. Rows without a name are dropped; anything
    with an unrecognized header is silently ignored rather than raising, so
    an export with extra columns still loads.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_map: dict[str, str] = {}
        for h in reader.fieldnames or []:
            norm = _normalize_header(h)
            for canon, variants in COLUMN_ALIASES.items():
                if norm in variants:
                    header_map[h] = canon
                    break
        for raw_row in reader:
            row: dict = {}
            for h, val in raw_row.items():
                canon = header_map.get(h)
                if canon is None:
                    continue
                row[canon] = _parse_field(canon, val)
            if not row.get("name"):
                continue
            if "position" in row and row["position"]:
                row["position"] = normalize_position(str(row["position"]))
            rows.append(row)
    return rows


def _merge_csv_rows(sources: Sequence[list[dict]]) -> list[dict]:
    """Merge rows across CSVs by (normalized name, position), filling gaps.

    The first source to report a field wins; later sources only fill fields
    still missing. This lets a projections export supply `points` and a
    separate ADP export supply `adp`/`adp_stdev` for the same player.
    """
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for rows in sources:
        for row in rows:
            pos = row.get("position") or ""
            mkey = (normalize_name(row["name"]), pos)
            if mkey not in merged:
                merged[mkey] = dict(row)
                order.append(mkey)
            else:
                existing = merged[mkey]
                for k, v in row.items():
                    if v is not None and existing.get(k) is None:
                        existing[k] = v
    return [merged[k] for k in order]


# --- Replacement level ---------------------------------------------------


def _s_p(position: str, slots: list[str]) -> int:
    """Number of starting-slot instances that accept `position`."""
    dummy = Player(player_id=-1, name="", eligible_positions=[position])
    return sum(1 for slot in slots if slot_accepts(slot, dummy))


def derive_replacement(
    rows: Sequence[dict],
    roster_positions: dict[str, int],
    num_teams: int,
    cfg: Config,
) -> tuple[dict[str, int], dict[str, float]]:
    """Derive starter counts and replacement-level points per position.

    Returns `(starters_per_pos, replacement)`. `starters_per_pos[p]` is the
    exact number of position-`p` players in the aggregate optimal lineup
    across `num_teams` identical teams — the optimizer-derived replacement
    rank, before `cfg.draft.replacement_depth` is applied. `replacement[p]`
    is the points total of the best player at `p` who does *not* crack that
    aggregate lineup, i.e. what you actually get if you pass on the position.
    """
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        pos = r.get("position")
        if pos and r.get("points") is not None:
            by_pos[pos].append(r)
    for plist in by_pos.values():
        plist.sort(key=lambda r: -r["points"])

    scaled_layout = {slot: count * num_teams for slot, count in roster_positions.items()}
    slots = starting_slots(scaled_layout)

    # Truncate each position to the top S_p candidates before calling
    # optimize(): no player past S_p can ever start (it would need S_p+1
    # distinct accepting slots), and the matroid exchange property means the
    # survivors are exactly the top S_p by points — this is exact, not an
    # approximation, and cuts the optimize() call by roughly 8x.
    truncated: list[Player] = []
    uid_to_pos: dict[int, str] = {}
    uid = 1
    for pos, plist in by_pos.items():
        s_p = _s_p(pos, slots)
        for r in plist[:s_p]:
            truncated.append(
                Player(
                    player_id=uid,
                    name=r["name"],
                    eligible_positions=[pos],
                    selected_position=BENCH,
                    projected_points=r["points"],
                )
            )
            uid_to_pos[uid] = pos
            uid += 1

    plan = optimize(truncated, scaled_layout, None, cfg)

    raw_counts: dict[str, int] = defaultdict(int)
    for _slot, p in plan.assignments:
        raw_counts[uid_to_pos[p.player_id]] += 1
    starters_per_pos = {pos: raw_counts.get(pos, 0) for pos in by_pos}

    depth = cfg.draft.replacement_depth
    replacement: dict[str, float] = {}
    for pos, plist in by_pos.items():
        idx = max(0, round(starters_per_pos[pos] * depth))
        if idx < len(plist):
            replacement[pos] = plist[idx]["points"]
        elif plist:
            replacement[pos] = plist[-1]["points"]

    return starters_per_pos, replacement


# --- Tiering ---------------------------------------------------------------


def _board_key(name: str, position: str) -> str:
    return f"{normalize_name(name)}:{position}"


def assign_tiers(rows: Sequence[dict], cfg: Config) -> dict[str, int]:
    """Deterministic tier breaks, frozen once at load time.

    Within each position, sorted by points descending, a new tier starts
    whenever the gap to the next player exceeds
    `max(tier_min_gap, tier_gap_multiplier * that position's median gap)`.
    Normalizing by the position's own median gap keeps this scale-free
    across positions with very different point spreads (QB vs. K) without a
    per-position tuning table.
    """
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        pos = r.get("position")
        if pos and r.get("points") is not None:
            by_pos[pos].append(r)

    tiers: dict[str, int] = {}
    for pos, plist in by_pos.items():
        ordered = sorted(plist, key=lambda r: -r["points"])
        gaps = [ordered[i]["points"] - ordered[i + 1]["points"] for i in range(len(ordered) - 1)]
        med = median(gaps) if gaps else 0.0
        threshold = max(cfg.draft.tier_min_gap, cfg.draft.tier_gap_multiplier * med)

        tier = 1
        for i, r in enumerate(ordered):
            tiers[_board_key(r["name"], pos)] = tier
            if i < len(ordered) - 1:
                gap = ordered[i]["points"] - ordered[i + 1]["points"]
                if gap > threshold:
                    tier += 1
    return tiers


# --- Board -------------------------------------------------------------


@dataclass(frozen=True)
class BoardPlayer:
    key: str  # f"{normalized name}:{position}" — the board's primary key
    name: str
    position: str
    team: str
    bye_week: int | None
    points: float  # season projection
    adp: float | None
    adp_stdev: float | None
    yahoo_id: int | None
    tier: int
    vor: float
    rank: int  # 1-indexed by vor desc, stable


@dataclass
class Board:
    players: list[BoardPlayer] = field(default_factory=list)
    by_key: dict[str, BoardPlayer] = field(default_factory=dict)
    replacement: dict[str, float] = field(default_factory=dict)
    starters_per_pos: dict[str, int] = field(default_factory=dict)
    tier_last: dict[tuple[str, int], str] = field(default_factory=dict)

    def with_yahoo_ids(self, id_map: dict[str, int]) -> "Board":
        """Return a copy with `yahoo_id` filled in from `{board_key: yahoo_id}`."""
        new_players = [
            dataclasses.replace(bp, yahoo_id=id_map.get(bp.key, bp.yahoo_id))
            for bp in self.players
        ]
        return Board(
            players=new_players,
            by_key={bp.key: bp for bp in new_players},
            replacement=self.replacement,
            starters_per_pos=self.starters_per_pos,
            tier_last=self.tier_last,
        )


def _compute_tier_last(board_players: Sequence[BoardPlayer]) -> dict[tuple[str, int], str]:
    by_pos: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in board_players:
        by_pos[bp.position].append(bp)

    tier_last: dict[tuple[str, int], str] = {}
    for pos, plist in by_pos.items():
        by_tier: dict[int, list[BoardPlayer]] = defaultdict(list)
        for bp in plist:
            by_tier[bp.tier].append(bp)
        for tier, members in by_tier.items():
            last = min(members, key=lambda b: b.points)
            tier_last[(pos, tier)] = last.key
    return tier_last


def load_board_from_config(cfg: Config, csv_paths: Sequence[str | Path] | None = None) -> Board:
    """Resolve board CSVs from `csv_paths` or `cfg.draft.board_csv` and load.

    Shared by scripts/draft.py and scripts/draft_export.py so both apply the
    same "no board configured" rule. Raises `ValueError` (not `SystemExit`)
    so each script can turn it into its own CLI-appropriate error message.
    """
    paths = list(csv_paths) if csv_paths else list(cfg.draft.board_csv)
    if not paths:
        raise ValueError("no board CSV configured (cfg.draft.board_csv is empty)")
    return load_board(paths, cfg.roster_positions, cfg.draft.num_teams, cfg)


def load_board(
    csv_paths: Sequence[str | Path],
    roster_positions: dict[str, int],
    num_teams: int,
    cfg: Config,
) -> Board:
    """Load, merge, and value a pre-draft board from one or more CSVs."""
    sources = [read_fantasypros(p) for p in csv_paths]
    rows = _merge_csv_rows(sources)
    rows = [r for r in rows if r.get("points") is not None and r.get("position")]

    starters_per_pos, replacement = derive_replacement(rows, roster_positions, num_teams, cfg)
    tiers = assign_tiers(rows, cfg)

    board_players: list[BoardPlayer] = []
    for row in rows:
        pos = row["position"]
        key = _board_key(row["name"], pos)
        repl = replacement.get(pos, row["points"])
        board_players.append(
            BoardPlayer(
                key=key,
                name=row["name"],
                position=pos,
                team=row.get("team") or "",
                bye_week=row.get("bye"),
                points=row["points"],
                adp=row.get("adp"),
                adp_stdev=row.get("adp_stdev"),
                yahoo_id=None,
                tier=tiers.get(key, 1),
                vor=row["points"] - repl,
                rank=0,
            )
        )

    board_players.sort(key=lambda bp: (-bp.vor, bp.name))
    board_players = [dataclasses.replace(bp, rank=i + 1) for i, bp in enumerate(board_players)]

    return Board(
        players=board_players,
        by_key={bp.key: bp for bp in board_players},
        replacement=replacement,
        starters_per_pos=starters_per_pos,
        tier_last=_compute_tier_last(board_players),
    )


def to_player(bp: BoardPlayer, uid: int) -> Player:
    """Adapter into `models.Player` for feeding `lineup.optimize()`.

    `projected_points` is season-scale here, weekly on the in-season lineup
    path — never persist the result, construct and discard within a single
    valuation call.
    """
    return Player(
        player_id=uid,
        name=bp.name,
        eligible_positions=[bp.position],
        selected_position=BENCH,
        bye_week=bp.bye_week,
        projected_points=bp.points,
    )


def export_rankings(board: Board, cfg: Config) -> list[dict]:
    """Ranking order for Yahoo's custom pre-draft rankings / autopick list.

    Ordered by static VOR — Yahoo's autopick has no idea what your roster
    looks like, so a roster-aware order would be actively wrong here.
    `export_defer_positions` (K, DEF by default) are held back until after
    rank `num_teams * (rounds - 2)` so autopick can't spend a mid-round pick
    on a kicker if it ever takes over.
    """
    deferred_positions = set(cfg.draft.export_defer_positions)
    non_deferred = [bp for bp in board.players if bp.position not in deferred_positions]
    deferred = [bp for bp in board.players if bp.position in deferred_positions]

    threshold = min(
        len(non_deferred),
        cfg.draft.num_teams * max(0, cfg.draft.rounds - 2),
    )
    ordered = non_deferred[:threshold] + deferred + non_deferred[threshold:]

    return [
        {
            "rank": i + 1,
            "name": bp.name,
            "team": bp.team,
            "position": bp.position,
            "bye": bp.bye_week,
            "points": bp.points,
            "vor": bp.vor,
            "tier": bp.tier,
            "adp": bp.adp,
        }
        for i, bp in enumerate(ordered)
    ]
