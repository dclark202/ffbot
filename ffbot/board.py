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
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median, stdev
from typing import Sequence

from .config import Config, LeagueScoring
from .lineup import optimize
from .models import BENCH, Player, slot_accepts, starting_slots
from .names import normalize_name, normalize_position
from .scoring import StatLine, score_statline, unmodeled_rules

# --- CSV loading -------------------------------------------------------
#
# FantasyPros exports differ by product (rankings / ADP / projections).
# Sniff headers rather than assuming one shape.

COLUMN_ALIASES: dict[str, frozenset[str]] = {
    # "PLAYER (BYE)" is the ADP export's header — one column carrying name,
    # team, and bye week together. See `_split_player_field`.
    "name": frozenset({"PLAYER NAME", "PLAYER", "NAME", "PLAYER (BYE)"}),
    "team": frozenset({"TEAM", "TM"}),
    "position": frozenset({"POS", "POSITION"}),
    "bye": frozenset({"BYE WEEK", "BYE"}),
    # "PROJ FPTS" (from "PROJ. FPTS" after `_normalize_header` strips the
    # period) is FantasyPros' WEEKLY rankings export's points column —
    # distinct from the season/draft export's plain "FPTS". Missing this
    # meant every row of that export shape parsed to `points=None` and was
    # silently dropped wholesale (`load_weekly_projection_rows`), which is
    # why `--proj`/`week_report.py`'s hand-fed-CSV route never actually
    # worked end to end even when someone supplied a file.
    "points": frozenset({"FPTS", "POINTS", "PROJECTED POINTS", "FANTASY POINTS", "PROJ FPTS"}),
    "adp": frozenset({"AVG", "ADP"}),
    "adp_stdev": frozenset({"STD DEV", "STDEV"}),
    "rank": frozenset({"RK", "RANK"}),
    "tier": frozenset({"TIERS", "TIER"}),
}

# Per-site ADP columns on FantasyPros' ADP export.
#
# These give `adp_spread`, which is deliberately NOT `adp_stdev`. A real STD
# DEV column measures how much a player's draft slot moves from draft to
# draft, and that is what `draft.sigma_for` wants. The spread across these
# columns measures something much narrower — how much six *consensus
# estimates* disagree — and each is already a mean over many drafts, so it is
# smaller than draft-to-draft variance by roughly a factor of sqrt(n).
# Feeding it to sigma_for would make survival wildly overconfident (a top
# pick shows a spread of 0.0-0.4 picks). It is kept as its own signal because
# market disagreement is a genuine indicator of an uncertain, interesting
# player. A closed, slowly-changing set, like NFL_TEAMS.
ADP_SOURCE_COLUMNS = frozenset(
    {"ESPN", "SLEEPER", "CBS", "NFL", "RTSPORTS", "FANTRAX", "YAHOO", "MFL", "FFC", "NFFC"}
)

_NUMERIC_FLOAT_FIELDS = frozenset({"points", "adp", "adp_stdev", "adp_spread"})
_NUMERIC_INT_FIELDS = frozenset({"bye", "rank", "tier"})

# Per-stat columns FantasyPros' projection exports carry, keyed by export
# shape rather than position — shape is what actually varies file to file.
# Each tuple is `(StatLine field name, expected normalized header)`, in the
# exact contiguous column order the export uses. These headers repeat within
# one file (e.g. "YDS" appears for both rushing and receiving), which is
# exactly why this has to be read positionally (`csv.reader`, column index)
# rather than by name (`csv.DictReader`, which silently collapses duplicate
# header names and drops every column but the last).
STAT_LAYOUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "FLEX": (
        ("rush_att", "ATT"), ("rush_yds", "YDS"), ("rush_td", "TDS"),
        ("rec", "REC"), ("rec_yds", "YDS"), ("rec_td", "TDS"),
        ("fumbles_lost", "FL"),
    ),
    "QB": (
        ("pass_att", "ATT"), ("pass_cmp", "CMP"), ("pass_yds", "YDS"),
        ("pass_td", "TDS"), ("pass_int", "INTS"),
        ("rush_att", "ATT"), ("rush_yds", "YDS"), ("rush_td", "TDS"),
        ("fumbles_lost", "FL"),
    ),
    "K": (
        ("fg_made", "FG"), ("fg_att", "FGA"), ("pat_made", "XPT"),
    ),
    "DEF": (
        ("sack", "SACK"), ("interception", "INT"), ("fumble_recovery", "FR"),
        ("forced_fumble", "FF"), ("def_td", "TD"), ("safety", "SAFETY"),
        ("points_allowed_season", "PA"), ("yards_allowed_season", "YDS_AGN"),
    ),
}


def _detect_stat_layout(norm_headers: list[str]) -> tuple[str, int] | None:
    """Find `(layout name, start index)` where a full `STAT_LAYOUTS` entry
    matches contiguously in `norm_headers`, immediately followed by a
    recognized points column.

    Deliberately never infers by position or file name alone: FantasyPros
    reorders and adds columns between seasons, and a half-matching layout
    read as if it were the real one would silently score receiving yards as
    rushing yards. A non-match returns `None`, which degrades to the
    unchanged FPTS-only path — the same board this always produced before
    per-stat scoring existed.
    """
    for layout_name, seq in STAT_LAYOUTS.items():
        n = len(seq)
        for start in range(0, len(norm_headers) - n + 1):
            if all(norm_headers[start + i] == seq[i][1] for i in range(n)):
                fpts_idx = start + n
                if fpts_idx < len(norm_headers) and norm_headers[fpts_idx] in COLUMN_ALIASES["points"]:
                    return layout_name, start
    return None


def _normalize_header(h: str) -> str:
    return " ".join(h.strip().upper().replace(".", "").split())


# The ADP export packs identity into a single column — "Jahmyr Gibbs   DET (6)",
# "Houston Texans DST   (8)", or bare "Tyreek Hill" for players with no current
# team. The projections exports keep these in separate columns, so this has to
# be unpacked or the two sources will never merge onto the same key. It is also
# the *only* place bye weeks appear in any of these files.
_BYE_RE = re.compile(r"\((\d+)\)\s*$")
_DEF_MARKER_RE = re.compile(r"\s+D/?ST\s*$", re.IGNORECASE)


def _split_player_field(raw: str) -> tuple[str, str | None, int | None]:
    """'Jahmyr Gibbs   DET (6)' -> ('Jahmyr Gibbs', 'DET', 6).

    Returns `(name, team, bye)` with team/bye None when absent. A trailing
    all-caps 2-3 letter token is read as the team; defenses ("Houston Texans
    DST") carry a marker instead of a team, and keep their full name so they
    merge with the DST projections export, which spells them the same way.
    """
    s = " ".join(raw.split())

    bye: int | None = None
    m = _BYE_RE.search(s)
    if m:
        bye = int(m.group(1))
        s = s[: m.start()].strip()

    s = _DEF_MARKER_RE.sub("", s).strip()

    team: str | None = None
    tokens = s.split()
    if len(tokens) > 1 and tokens[-1].isalpha() and tokens[-1].isupper() and 2 <= len(tokens[-1]) <= 3:
        team = tokens[-1]
        s = " ".join(tokens[:-1])

    return s, team, bye


def _parse_numeric_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    val = raw.strip()
    if val == "":
        return None
    try:
        return float(val.replace(",", ""))
    except ValueError:
        return None


def _spread_from_sources(
    raw_values: list[str], norm_headers: list[str], canon_by_index: dict[int, str]
) -> float | None:
    """Sample stdev *across sites* of the per-site ADP columns, or None if <2.

    This is `adp_spread`, not `adp_stdev` — see ADP_SOURCE_COLUMNS for why
    the distinction matters and why this must not reach `draft.sigma_for`.

    `canon_by_index` covers only the canonical columns, so any other index is
    a candidate site column; intersecting with ADP_SOURCE_COLUMNS keeps stray
    numeric columns (e.g. "Real-Time") out of the estimate.
    """
    values: list[float] = []
    for i, val in enumerate(raw_values):
        if i in canon_by_index:
            continue
        if i >= len(norm_headers) or norm_headers[i] not in ADP_SOURCE_COLUMNS:
            continue
        parsed = _parse_numeric_float(val)
        if parsed is not None:
            values.append(parsed)
    if len(values) < 2:
        return None
    return stdev(values)


def _parse_field(field_name: str, raw: str | None) -> float | int | str | None:
    if raw is None:
        return None
    val = raw.strip()
    if val == "":
        return None
    if field_name in _NUMERIC_FLOAT_FIELDS:
        return _parse_numeric_float(raw)
    if field_name in _NUMERIC_INT_FIELDS:
        parsed = _parse_numeric_float(raw)
        return int(parsed) if parsed is not None else None
    return val


def read_fantasypros(path: str | Path, default_position: str | None = None) -> list[dict]:
    """Parse one FantasyPros CSV export into rows with canonical field names.

    Handles the UTF-8 BOM FantasyPros downloads carry, thousands separators
    in point totals, and the differing header sets across their rankings,
    ADP, and projections exports. Rows without a name are dropped; anything
    with an unrecognized header is silently ignored rather than raising, so
    an export with extra columns still loads.

    `default_position` supplies the position for single-position exports
    (qb.php, k.php, dst.php), which omit the column entirely because the page
    itself implies it. Only the mixed flex export carries its own POS, and a
    row with no position is dropped downstream, so without this those files
    contribute nothing at all.

    Reads positionally (`csv.reader` + column index), not by header name
    (`csv.DictReader`): these exports repeat header text within one file
    (rushing YDS/TDS, then receiving YDS/TDS), and `DictReader` silently
    collapses duplicate keys, keeping only the last column's value. Reading
    by index is what lets `row["stats"]` (see `STAT_LAYOUTS`) recover the
    rushing and receiving lines separately instead of losing one to the
    other.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None) or []
        norm_headers = [_normalize_header(h) for h in header]

        canon_by_index: dict[int, str] = {}
        for i, norm in enumerate(norm_headers):
            for canon, variants in COLUMN_ALIASES.items():
                if norm in variants and canon not in canon_by_index.values():
                    canon_by_index[i] = canon
                    break

        layout = _detect_stat_layout(norm_headers)

        for raw_values in reader:
            if len(raw_values) < len(header):
                # A short row (FantasyPros ships at least one blank
                # sub-header row per export) — pad rather than raise. It
                # ends up with no name below and is dropped before it ever
                # reaches stat extraction.
                raw_values = raw_values + [""] * (len(header) - len(raw_values))

            row: dict = {}
            for i, canon in canon_by_index.items():
                if i < len(raw_values):
                    row[canon] = _parse_field(canon, raw_values[i])
            if not row.get("name"):
                continue

            # Unpack "Name TEAM (Bye)" when the export packs them together. A
            # dedicated column always wins, so this only ever fills gaps.
            name, team, bye = _split_player_field(str(row["name"]))
            if name:
                row["name"] = name
            if team and not row.get("team"):
                row["team"] = team
            if bye is not None and row.get("bye") is None:
                row["bye"] = bye

            if row.get("adp") is not None and row.get("adp_spread") is None:
                row["adp_spread"] = _spread_from_sources(raw_values, norm_headers, canon_by_index)

            if row.get("position"):
                row["position"] = normalize_position(str(row["position"]))
            elif default_position:
                row["position"] = normalize_position(default_position)

            if layout is not None:
                layout_name, start = layout
                seq = STAT_LAYOUTS[layout_name]
                if start + len(seq) <= len(raw_values):
                    stat_kwargs = {
                        field_name: _parse_numeric_float(raw_values[start + offset])
                        for offset, (field_name, _header) in enumerate(seq)
                    }
                    if any(v is not None for v in stat_kwargs.values()):
                        row["stats"] = StatLine(**stat_kwargs)

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


# --- League scoring --------------------------------------------------------
#
# Placement matters: this must run after `_merge_csv_rows` (so a stat line
# merged in from a separate ADP/points export is still present) and *before*
# `derive_replacement`/`assign_tiers` (both of which are functions of
# `points` — recomputing after them would leave replacement level and tiers
# on the PPR scale while candidate points moved to the league scale, a
# split-brain invisible from the output). `load_board` and
# `roster_source.load_weekly_projection_rows` both call this — missing
# either one leaves that path silently on the wrong scale.


def apply_league_scoring(rows: list[dict], league: LeagueScoring | None) -> None:
    """Recompute `points` under `league`'s rules, in place.

    `league is None` (the default — no `league.yml`) is an exact no-op on
    `points` itself; every row still gets `points_fp`/`points_source`/
    `points_flags` set so downstream code can label how a number was
    produced either way. A row with no parsed `stats` (an ADP-only row, a
    rankings export with no per-stat columns) always keeps its consensus
    `points` — there's nothing to recompute it from.
    """
    for row in rows:
        row["points_fp"] = row.get("points")
        stats: StatLine | None = row.get("stats")
        if league is None or stats is None:
            row["points_source"] = "consensus"
            row["points_flags"] = ()
            continue
        league_points, flags = score_statline(stats, row.get("position", ""), league)
        row["points"] = league_points
        row["points_source"] = "league"
        row["points_flags"] = flags


_RESIDUAL_WARN_THRESHOLD = 1.5  # season points; see `_scoring_residuals`


def _scoring_residuals(rows: Sequence[dict]) -> dict[str, float]:
    """Per-position mean `|points_fp - model(stats under FantasyPros' own
    default rules)|` — a self-check on `STAT_LAYOUTS`/`score_statline`
    itself, independent of whatever `league.yml` says.

    `points_fp` is FantasyPros' own published number; feeding the same stat
    line through this codebase's model of FantasyPros' *own* scoring should
    reproduce it closely regardless of what league is configured. A residual
    over `_RESIDUAL_WARN_THRESHOLD` for a whole position means the column
    layout or the arithmetic is wrong, not that the league differs from
    FantasyPros — that difference is `scoring_edge`, a different, expected
    number.
    """
    fp_default = LeagueScoring.fantasypros_default()
    by_pos: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        stats = row.get("stats")
        fp_points = row.get("points_fp")
        pos = row.get("position")
        if stats is None or fp_points is None or not pos:
            continue
        modeled, _flags = score_statline(stats, pos, fp_default)
        by_pos[pos].append(abs(fp_points - modeled))
    return {pos: sum(vals) / len(vals) for pos, vals in by_pos.items() if vals}


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
    adp_stdev: float | None  # draft-to-draft variance; drives survival
    adp_spread: float | None  # cross-site disagreement; an upside signal only
    platform_id: str | None  # Sleeper player id, once reconciled (see names.match_board_to_platform); None until then
    tier: int
    vor: float
    rank: int  # 1-indexed by vor desc, stable

    # Researched intel, merged in at load from draft/intel.yml. Defaulted so
    # that appending them breaks no existing constructor, and so a board built
    # without an intel file is exactly the board we had before.
    upside: float | None = None  # 0-100 breakout potential vs consensus
    availability_risk: float | None = None  # 0-100 "will he play?" — factual only
    intel_note: str = ""  # plain-English "why", shown in recommendations
    intel_flags: tuple[str, ...] = ()

    # League scoring (see `apply_league_scoring`). `points_fp` is always the
    # raw FantasyPros consensus number, kept alongside `points` (the league
    # number, once a `league.yml` is configured) specifically so the delta
    # between them — the scoring arbitrage the consensus market cannot
    # correct, since it isn't playing your league — is available as an edge
    # signal (`DraftConfig.scoring_arbitrage_weight`) without recomputing
    # anything. `points_source` is "league" or "consensus"; `points_flags`
    # names anything in `points` that is estimated rather than exact
    # (`fg_distance_estimated`, `pa_distribution_estimated`) — see
    # `ffbot/scoring.py`. All three default so a board built without a
    # `league.yml` is bit-identical to one built before this feature existed.
    points_fp: float = 0.0
    points_source: str = "consensus"
    points_flags: tuple[str, ...] = ()


@dataclass
class Board:
    players: list[BoardPlayer] = field(default_factory=list)
    by_key: dict[str, BoardPlayer] = field(default_factory=dict)
    replacement: dict[str, float] = field(default_factory=dict)
    starters_per_pos: dict[str, int] = field(default_factory=dict)
    tier_last: dict[tuple[str, int], str] = field(default_factory=dict)

    # Per-position mean absolute residual from `_scoring_residuals` — a
    # health check on the stat-line parser, not on the league's own rules.
    # Empty whenever no row had a parsed `stats` line to check.
    scoring_residual: dict[str, float] = field(default_factory=dict)

    def scoring_summary(self) -> dict[str, dict[str, int]]:
        """`{position: {"league": n, "consensus": n}}` — how many players at
        each position actually got league-scored vs. stayed on consensus
        FantasyPros points. A whole position showing 0 "league" usually means
        that export has no per-stat columns this build recognizes (or no
        `league.yml` is configured at all) — worth a startup warning, since
        it is otherwise invisible that e.g. every kicker is still flat-3
        PPR-scored in a league that pays by distance.
        """
        out: dict[str, dict[str, int]] = defaultdict(lambda: {"league": 0, "consensus": 0})
        for bp in self.players:
            out[bp.position][bp.points_source] += 1
        return dict(out)


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


@dataclass(frozen=True)
class BoardSource:
    """One board CSV, plus the position to assume when the file omits it."""

    path: str | Path
    position: str | None = None


def _normalize_sources(entries: Sequence) -> list[BoardSource]:
    """Accept plain paths or `{path: ..., position: ...}` mappings from YAML."""
    out: list[BoardSource] = []
    for entry in entries:
        if isinstance(entry, BoardSource):
            out.append(entry)
        elif isinstance(entry, dict):
            path = entry.get("path")
            if not path:
                raise ValueError(f"board_csv entry has no 'path': {entry!r}")
            out.append(BoardSource(path, entry.get("position")))
        else:
            out.append(BoardSource(entry))
    return out


def load_board_from_config(cfg: Config, csv_paths: Sequence[str | Path] | None = None) -> Board:
    """Resolve board CSVs from `csv_paths` or `cfg.draft.board_csv` and load.

    Shared by scripts/draft.py and scripts/draft_export.py so both apply the
    same "no board configured" rule. Raises `ValueError` (not `SystemExit`)
    so each script can turn it into its own CLI-appropriate error message.

    `cfg.draft.board_points_source == "sleeper"` (off by default) fetches a
    live Sleeper season-points overlay first — see `load_board`'s
    `extra_points_rows` and CLAUDE.md's hybrid draft-board design. `cfg.league`
    (when set) is passed straight through to `fetch_season_points_rows`, so
    this overlay is league-scored the same as the weekly/ROS paths, not
    frozen at Sleeper's own consensus PPR — see that function's docstring
    for exactly what "league-scored" means for K/DEF, and
    `_apply_points_overlay` for how the resulting `points_fp`/
    `points_source`/`points_flags` survive onto the board. A failed
    fetch never raises out of this function: it warns and falls back to
    `board_csv`-only points, same contract `ffbot.report.load_everything`
    uses for the weekly path. This is the one place `ffbot.sleeper` is
    imported from this module, and only when the config flag is actually on
    — `scripts/draft.py --board ...` with the default config stays fully
    offline.
    """
    paths = list(csv_paths) if csv_paths else list(cfg.draft.board_csv)
    if not paths:
        raise ValueError("no board CSV configured (cfg.draft.board_csv is empty)")

    extra_points_rows: list[dict] | None = None
    if cfg.draft.board_points_source == "sleeper":
        from .projections import current_nfl_season
        from .projections.sleeper import fetch_season_points_rows
        from .sleeper.cache import SleeperFetchError

        try:
            extra_points_rows = fetch_season_points_rows(
                current_nfl_season(), ttl_minutes=cfg.draft.board_points_cache_ttl_minutes,
                league=cfg.league,
            )
        except SleeperFetchError as exc:
            warnings.warn(
                f"draft.board_points_source=sleeper: live fetch failed ({exc}) — "
                "falling back to board_csv's own points.",
                stacklevel=2,
            )

    board = load_board(paths, cfg.roster_positions, cfg.draft.num_teams, cfg, extra_points_rows=extra_points_rows)

    # Imported here rather than at module scope: intel imports Board from this
    # module, and the dependency only runs in this direction at call time.
    from .intel import apply_intel, load_intel

    return apply_intel(board, load_intel(cfg.draft.intel_file))


def _apply_points_overlay(rows: list[dict], overlay_rows: list[dict]) -> list[dict]:
    """Overwrite `points`/`points_fp`/`points_source`/`points_flags`/`stats`
    on any row `overlay_rows` covers (matched by normalized name+position),
    and append any overlay player the CSV pool doesn't have at all.

    Deliberately a separate pass run AFTER `apply_league_scoring`, not
    folded into `_merge_csv_rows`'s generic field-level gap-fill (an earlier
    version of this function did exactly that, prepending the overlay as
    just another merge source — it silently discarded every overlaid
    player's live points the moment `cfg.league` was configured: a CSV
    row's `stats` field, not being None, would merge in on top of the
    overlay row's `stats: None`, and `apply_league_scoring`'s stats-present
    branch would then recompute `points` from that CSV stat line, clobbering
    the overlay's number right back to a rescoring of the frozen CSV data —
    exactly the split-brain bug `apply_league_scoring`'s own docstring warns
    about, just arrived at from the overlay side rather than an ordering
    mistake). Running this strictly after `apply_league_scoring` sidesteps
    that interaction entirely: nothing recomputes `points` from a stat line
    after this point.

    Overlaid rows lose their `stats` line too, not just gain new `points` —
    otherwise `_scoring_residuals`' "does this stat line reproduce
    FantasyPros' own FPTS" check would compare the CSV's stat line against
    the overlay's now-substituted `points_fp` and raise a false-positive
    residual warning for every overlaid player.

    `points_fp`/`points_source`/`points_flags` are taken from the overlay
    row itself when it supplies them (e.g. `fetch_season_points_rows(...,
    league=cfg.league)`, `ros_rows`, both league-scored) — an overlay row
    that supplies its own `points` but not those (the older, still-valid
    plain-consensus shape) falls back to `points_fp = points`,
    `points_source = "consensus"`, `points_flags = ()`, exactly today's
    behavior. This is what lets `edge.scoring_edge` (league points minus
    consensus) see a real, nonzero gap for an overlaid player instead of
    being structurally zero for the whole pool the overlay covers.
    """
    by_key = {
        (normalize_name(r["name"]), r.get("position", "")): r
        for r in overlay_rows if r.get("points") is not None
    }
    covered: set[tuple[str, str]] = set()
    for row in rows:
        key = (normalize_name(row["name"]), row.get("position", ""))
        ov = by_key.get(key)
        if ov is None:
            continue
        covered.add(key)
        row["points"] = ov["points"]
        row["points_fp"] = ov.get("points_fp", ov["points"])
        row["points_source"] = ov.get("points_source", "consensus")
        row["points_flags"] = ov.get("points_flags", ())
        row["stats"] = None
    for key, ov in by_key.items():
        if key in covered:
            continue
        rows.append({
            "name": ov["name"], "team": ov.get("team") or "", "position": ov["position"],
            "points": ov["points"], "points_fp": ov.get("points_fp", ov["points"]),
            "points_source": ov.get("points_source", "consensus"),
            "points_flags": ov.get("points_flags", ()), "stats": None, "bye": ov.get("bye"),
            "adp": None, "adp_stdev": None, "adp_spread": None,
        })
    return rows


def load_board(
    csv_paths: Sequence,
    roster_positions: dict[str, int],
    num_teams: int,
    cfg: Config,
    extra_points_rows: list[dict] | None = None,
) -> Board:
    """Load, merge, and value a pre-draft board from one or more CSVs.

    Entries may be plain paths or `{path, position}` mappings — see
    `read_fantasypros` for why single-position exports need the latter.

    `extra_points_rows`, if given, overlays `points` for any player it
    covers (via `_apply_points_overlay`, run after `apply_league_scoring` —
    see that function's docstring for why the ordering matters) while the
    CSVs still supply everything the overlay doesn't (adp/bye/adp_spread).
    `ffbot.projections.sleeper.fetch_season_points_rows` is the one producer
    of this shape today.

    A configured path that doesn't exist on disk is skipped with a surfaced
    warning rather than raising — this is the common state for a fresh
    clone (the FantasyPros CSVs are gitignored, manual downloads) and the
    GUI/CLIs must still start in that state, just without a board. `raise
    ValueError` (below, once every configured source is unusable) stays the
    single "no board" signal every caller of `load_board`/
    `load_board_from_config` already catches — see that function's own
    docstring for the full list of callers relying on it.
    """
    sources: list[list[dict]] = []
    missing: list[str] = []
    for src in _normalize_sources(csv_paths):
        if not Path(src.path).exists():
            missing.append(str(src.path))
            warnings.warn(
                f"{src.path}: board CSV not found — skipping. Download it "
                "(README, step 2) to enable the draft board and waiver-add "
                "valuation.",
                stacklevel=2,
            )
            continue
        parsed = read_fantasypros(src.path, default_position=src.position)
        usable = [r for r in parsed if r.get("position")]
        if parsed and not usable:
            # Every row unusable means the file has no POS column and no
            # override — silently yielding an empty board here is how you
            # discover the problem mid-draft instead of now.
            raise ValueError(
                f"{src.path}: no position on any of its {len(parsed)} rows. "
                "Single-position exports (qb/k/dst) need an explicit position, "
                f"e.g. {{path: {src.path}, position: QB}} in draft.board_csv."
            )
        if len(usable) < len(parsed):
            warnings.warn(
                f"{src.path}: dropped {len(parsed) - len(usable)} of {len(parsed)} "
                "rows with no position",
                stacklevel=2,
            )
        sources.append(usable)

    if not sources:
        raise ValueError(
            "none of the configured board CSVs exist yet "
            f"({', '.join(missing)}) — download the FantasyPros exports "
            "(README, step 2), or pass --board / set draft.board_csv."
        )

    rows = _merge_csv_rows(sources)
    rows = [r for r in rows if r.get("points") is not None and r.get("position")]

    # Must run after the merge (so a stat line from one source survives onto
    # a row whose points came from another) and before replacement/tiers
    # (both are functions of `points` — see `apply_league_scoring`'s
    # docstring for why recomputing after them is a real bug, not a style
    # choice).
    apply_league_scoring(rows, cfg.league)

    # After league scoring, before replacement/tiers -- see
    # _apply_points_overlay's docstring for why an earlier position (folded
    # into the merge above, ahead of apply_league_scoring) was a real bug.
    if extra_points_rows:
        rows = _apply_points_overlay(rows, extra_points_rows)

    residuals = _scoring_residuals(rows)
    for pos, resid in residuals.items():
        if resid > _RESIDUAL_WARN_THRESHOLD:
            warnings.warn(
                f"{pos}: average {resid:.1f}-point residual between FantasyPros' own "
                "FPTS and this codebase's model of FantasyPros' own scoring — the "
                "stat-column layout for this export may not match STAT_LAYOUTS "
                "anymore; league-scored points for this position may be wrong",
                stacklevel=2,
            )
    if cfg.league is not None:
        for rule in unmodeled_rules(cfg.league):
            warnings.warn(f"league.yml: {rule}", stacklevel=2)

    return _finalize_board(rows, roster_positions, num_teams, cfg, scoring_residual=residuals)


def _finalize_board(
    rows: list[dict],
    roster_positions: dict[str, int],
    num_teams: int,
    cfg: Config,
    scoring_residual: dict[str, float] | None = None,
) -> Board:
    """Rows (already merged, league-scored, and any overlay applied) ->
    replacement level, tiers, `BoardPlayer` construction, VOR-sorted
    ranking. The shared tail of `load_board` and `rescale_board_points` —
    both need this identical pipeline over their own row set; only what
    produces `rows` differs (CSV ingestion vs. rebuilding rows from an
    existing `Board`'s players).

    `upside`/`availability_risk`/`intel_note`/`intel_flags` are read from
    `row` if present (a rescale carries these forward from the board being
    rescaled — see `rescale_board_points`) and default like `BoardPlayer`'s
    own dataclass defaults otherwise (`load_board`'s CSV rows never carry
    them; `load_board_from_config`'s `apply_intel` sets them in a later,
    separate pass, same as it always has).
    """
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
                adp_spread=row.get("adp_spread"),
                platform_id=None,
                tier=tiers.get(key, 1),
                vor=row["points"] - repl,
                rank=0,
                points_fp=row.get("points_fp") if row.get("points_fp") is not None else row["points"],
                points_source=row.get("points_source", "consensus"),
                points_flags=tuple(row.get("points_flags") or ()),
                upside=row.get("upside"),
                availability_risk=row.get("availability_risk"),
                intel_note=row.get("intel_note", "") or "",
                intel_flags=tuple(row.get("intel_flags") or ()),
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
        scoring_residual=scoring_residual or {},
    )


def rescale_board_points(
    board: Board,
    roster_positions: dict[str, int],
    num_teams: int,
    cfg: Config,
    overlay_rows: list[dict],
) -> Board:
    """Rebuild `board` with `overlay_rows`' points substituted in wherever
    they cover a player (via `_apply_points_overlay` — see that function's
    docstring for why `points`/`points_fp`/`stats` all move together), then
    re-run replacement level and tiers so every downstream valuation number
    (`draft.need`/`value`, `week.hold_margin`/`drop_cost`/`ros_gain`) is
    consistent with the new points, not a stale mix of old replacement level
    against new points.

    This is the seam that makes rest-of-season waiver/hold/drop valuation
    genuinely live: `ffbot.projections.ros_rows` already computes a real
    ROS total by summing correctly-scored weekly numbers, but `week.py`'s
    valuation functions only ever read `Board.by_key`/`Board.replacement` —
    they have no live-data override hook of their own, and were never meant
    to need one. Overlaying ROS points onto a `Board` and re-deriving
    replacement/tiers here means `_season_score` (in `ffbot/draft.py`,
    unchanged) reads real rest-of-season numbers automatically, the same
    way `load_board`'s own `extra_points_rows` overlay makes the DRAFT board
    live — same mechanism, applied a second time to a different board.

    A player `board` has but `overlay_rows` doesn't (no live number for them
    yet) keeps their frozen board points, same partial-coverage philosophy
    every other optional live input in this codebase already follows.
    """
    rows = [
        {
            "name": bp.name, "team": bp.team, "position": bp.position, "bye": bp.bye_week,
            "points": bp.points, "adp": bp.adp, "adp_stdev": bp.adp_stdev, "adp_spread": bp.adp_spread,
            "points_fp": bp.points_fp, "points_source": bp.points_source, "points_flags": bp.points_flags,
            "upside": bp.upside, "availability_risk": bp.availability_risk,
            "intel_note": bp.intel_note, "intel_flags": bp.intel_flags,
        }
        for bp in board.players
    ]
    rows = _apply_points_overlay(rows, overlay_rows)
    return _finalize_board(rows, roster_positions, num_teams, cfg, scoring_residual=board.scoring_residual)


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
        team=bp.team,
        bye_week=bp.bye_week,
        projected_points=bp.points,
    )


def _export_score(bp: BoardPlayer, scale: float) -> float:
    """Static ranking score: VOR plus a fixed-unit intel adjustment.

    The live draft layer scales its bonuses to what each pick is worth; a
    static list has no "current pick", so the adjustment is a flat
    `export_intel_scale` season points at a full 100 upside/risk score. Kept
    roster-blind on purpose — Yahoo's autopick has no idea what your roster
    looks like, so a roster-aware order would be actively wrong here — but
    intel is a property of the player, not the roster, and the safety net
    should draft with the same researched edge you would.
    """
    if scale == 0.0:
        return bp.vor
    upside = (bp.upside or 0.0) / 100.0
    risk = (bp.availability_risk or 0.0) / 100.0
    return bp.vor + scale * (upside - risk)


def export_rankings(board: Board, cfg: Config) -> list[dict]:
    """Ranking order for Yahoo's custom pre-draft rankings / autopick list.

    Ordered by `_export_score` (static VOR, intel-adjusted when
    `export_intel_scale` is set). `export_defer_positions` (K, DEF by default)
    are held back until after rank `num_teams * (rounds - 2)` so autopick
    can't spend a mid-round pick on a kicker if it ever takes over.
    """
    scale = cfg.draft.export_intel_scale
    ordered_all = sorted(
        board.players, key=lambda bp: (-_export_score(bp, scale), bp.name)
    )
    deferred_positions = set(cfg.draft.export_defer_positions)
    non_deferred = [bp for bp in ordered_all if bp.position not in deferred_positions]
    deferred = [bp for bp in ordered_all if bp.position in deferred_positions]

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
