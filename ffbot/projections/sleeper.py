"""Sleeper (`api.sleeper.app`) weekly fantasy projections — free, no auth,
plain JSON. Verified live for the 2026 season during scoping (unlike the
FantasyPros ECR archive `ffbot/history/board.py` depends on for the draft
board, whose scrape pipeline stopped running 2026-05-29).

`fetch_weekly_rows` returns rows in the EXACT shape
`ffbot.board.read_fantasypros` produces (`name`/`team`/`position`/`points`/
`bye`/`stats`) — that is what lets a Sleeper week drop straight into
`ffbot.roster_source.load_weekly_projection_rows`'s existing rows path with
nothing downstream needing to know the source changed. `stats` is a real
`ffbot.scoring.StatLine`, not just a points total, so
`board.apply_league_scoring` can recompute every projection under
`league.yml`'s actual rules (this league's −2 INT, distance-tiered FGs, DEF
points-allowed ladder) — the identical treatment board CSVs already get.

Field-goal distance bands (`fgm_0_19`/`fgm_20_29`/...) are deliberately NOT
mapped to `StatLine.fg_made_bands` here, even though Sleeper exposes them.
Spot-checking real kickers showed the bands don't reliably sum to `fgm` (a
residual of unaccounted makes — most likely uncaptured 50+ yard kicks, since
Sleeper's band set tops out at "40-49"), and `score_statline`'s bands branch
is all-or-nothing: any bands present replace the flat-rate estimate entirely,
so an incomplete band set would silently UNDERCOUNT points for exactly the
kickers who attempt long field goals. Plain `fg_made`/`fg_att` route through
the existing `_fg_value_per_kick` league-wide-mix estimate instead — the same
rigor a bands-less FantasyPros export already gets. Revisit with a season of
real data to confirm reconciliation before trusting the bands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..scoring import StatLine
from .cache import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_projection_json

# Sleeper's projections endpoint covers every fantasy-relevant position in
# one call; DST is spelled "DEF" here, matching this codebase's convention
# (`models.py`) rather than Sleeper's own player.position value, which
# already happens to agree.
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

_BASE_URL = "https://api.sleeper.app/projections/nfl"

# The only projection provider this endpoint has ever returned during
# scoping. Filtered explicitly (rather than assumed) so a future second
# provider on the same endpoint can't silently produce duplicate rows per
# player — see `_row_from_entry`.
_COMPANY = "rotowire"


def _projection_url(season: int, week: int) -> str:
    positions = "&".join(f"position[]={p}" for p in POSITIONS)
    return f"{_BASE_URL}/{season}/{week}?season_type=regular&{positions}&order_by=pts_ppr"


def _num(stats: dict, key: str) -> Optional[float]:
    val = stats.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stat_line(stats: dict, position: str) -> StatLine:
    if position == "K":
        return StatLine(
            fg_made=_num(stats, "fgm"),
            fg_att=_num(stats, "fga"),
            pat_made=_num(stats, "xpm"),
            # A real enhancement over FantasyPros exports, which carry no PAT
            # attempts/misses at all (see `scoring.unmodeled_rules`).
            pat_missed=_num(stats, "xpmiss"),
        )
    if position == "DEF":
        return StatLine(
            sack=_num(stats, "sack"),
            interception=_num(stats, "int"),
            fumble_recovery=_num(stats, "fum_rec"),
            forced_fumble=_num(stats, "ff"),
            def_td=_num(stats, "def_td"),
            safety=_num(stats, "safe"),
            # Real per-game points allowed, not a season total needing
            # `_points_allowed_per_game`'s distribution estimate.
            points_allowed_game=_num(stats, "pts_allow"),
        )
    return StatLine(
        pass_att=_num(stats, "pass_att"),
        pass_cmp=_num(stats, "pass_cmp"),
        pass_yds=_num(stats, "pass_yd"),
        pass_td=_num(stats, "pass_td"),
        pass_int=_num(stats, "pass_int"),
        pass_2pt=_num(stats, "pass_2pt"),
        rush_att=_num(stats, "rush_att"),
        rush_yds=_num(stats, "rush_yd"),
        rush_td=_num(stats, "rush_td"),
        rush_2pt=_num(stats, "rush_2pt"),
        rec=_num(stats, "rec"),
        rec_yds=_num(stats, "rec_yd"),
        rec_td=_num(stats, "rec_td"),
        rec_2pt=_num(stats, "rec_2pt"),
        fumbles_lost=_num(stats, "fum_lost"),
    )


def _row_from_entry(entry: dict) -> Optional[dict]:
    if entry.get("company") != _COMPANY:
        return None

    player = entry.get("player") or {}
    position = (player.get("position") or "").strip().upper()
    if position not in POSITIONS:
        return None

    stats = entry.get("stats") or {}
    points = _num(stats, "pts_ppr")
    if points is None:
        # No projection at all -- an inactive/retired/practice-squad entry
        # Sleeper's player database still carries. Filtering on this is also
        # what naturally drops the rare duplicate-name entries seen during
        # scoping (every duplicate had no points either).
        return None

    name = f"{player.get('first_name') or ''} {player.get('last_name') or ''}".strip()
    if not name:
        return None

    return {
        "name": name,
        "team": (player.get("team") or "").strip().upper(),
        "position": position,
        "points": points,  # the pre-league-scoring fallback -- see apply_league_scoring
        "bye": None,  # Sleeper carries no bye field; the board fallback fills this in
        "stats": _stat_line(stats, position),
    }


def fetch_season_points_rows(
    season: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = 360.0,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
) -> list[dict]:
    """Season-total points for the draft-relevant pool, from Sleeper's same
    (undocumented) projections endpoint the weekly path uses, grouped by
    season instead of week. Feeds `ffbot.board.load_board`'s
    `extra_points_rows` — the hybrid draft-board design (CLAUDE.md): Sleeper
    supplies live season POINTS, `draft.board_csv`'s FantasyPros exports
    still supply ADP, bye weeks, and cross-site ADP spread, none of which
    this endpoint carries at all.

    Deliberately NOT a `StatLine` reconstruction, unlike `fetch_weekly_rows`
    above: season-grouped entries use a materially different field shape
    than the weekly ones `_stat_line` was built for (bucketed FG-by-distance
    and cumulative points-allowed-by-bucket counts rather than per-kick/
    per-game figures — verified live during scoping) — reusing that mapping
    here risks silently wrong scoring for exactly the two positions
    (K, DEF) it's hardest to catch a mistake on. Returns Sleeper's own
    `pts_ppr` as a plain CONSENSUS number (`stats: None`) instead — the same
    footing FantasyPros' own points already sit on. `apply_league_scoring`
    already leaves a `stats`-less row on its consensus points untouched, so
    this degrades exactly like an ADP-only board row does today, not a new
    code path.

    Delegates the actual HTTP+cache work to
    `ffbot.sleeper.client.SleeperClient.season_projections` rather than
    building a second URL/cache path here — this function only owns the
    row-shape translation.
    """
    from ..sleeper.client import SleeperClient  # local import: keeps this package's own import graph one-directional until a season overlay is actually requested

    client = SleeperClient(cache_dir=cache_dir, opener=opener, now=now)
    entries = client.season_projections(season, ttl_minutes=ttl_minutes)

    rows = []
    for entry in entries:
        if entry.get("company") != _COMPANY:
            continue
        player = entry.get("player") or {}
        position = (player.get("position") or "").strip().upper()
        if position not in POSITIONS:
            continue
        stats = entry.get("stats") or {}
        points = _num(stats, "pts_ppr")
        if points is None:
            continue
        name = f"{player.get('first_name') or ''} {player.get('last_name') or ''}".strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "team": (player.get("team") or "").strip().upper(),
            "position": position,
            "points": points,
            "bye": None,  # Sleeper carries no bye field on this endpoint either -- the board CSV source fills it in
            "stats": None,  # consensus points only -- see the docstring above for why
        })
    return rows


def fetch_weekly_rows(
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = None,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
) -> list[dict]:
    """One week's Sleeper projections as `read_fantasypros`-shaped rows.

    Cache-first via `ffbot.projections.cache` — see that module for the
    `ttl_minutes` contract. Raises `ffbot.projections.cache.ProjectionFetchError`
    on a network failure; callers decide the fallback (see
    `ffbot/projections/__init__.py`).
    """
    url = _projection_url(season, week)
    data = fetch_projection_json(
        "sleeper", url, season, week, cache_dir=cache_dir, ttl_minutes=ttl_minutes, opener=opener, now=now,
    )
    rows = []
    for entry in data:
        row = _row_from_entry(entry)
        if row is not None:
            rows.append(row)
    return rows
