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

`fetch_season_points_rows` (below) is a real `StatLine` reconstruction for
QB/RB/WR/TE — the season endpoint's cumulative passing/rushing/receiving
totals ARE reliably present, unlike its K/DEF fields (bucketed FG-by-distance
and cumulative points-allowed-by-bucket counts, verified live during
scoping), which stay off this path entirely — see that function's and
`_kdef_season_ratio`'s own docstrings for how K/DEF are handled instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from ..scoring import StatLine, score_statline
from .cache import DEFAULT_CACHE_DIR, ProjectionFetchError, UrlOpener, _default_opener, fetch_projection_json

if TYPE_CHECKING:
    from ..config import LeagueScoring

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
            # Same real-per-game shape as points allowed -- only scored by
            # `score_statline` when a league actually configures
            # `defense.yards_allowed` (most don't).
            yards_allowed_game=_num(stats, "yds_allow"),
        )
    return StatLine(
        pass_att=_num(stats, "pass_att"),
        pass_cmp=_num(stats, "pass_cmp"),
        pass_yds=_num(stats, "pass_yd"),
        pass_td=_num(stats, "pass_td"),
        pass_int=_num(stats, "pass_int"),
        pass_2pt=_num(stats, "pass_2pt"),
        # 40+ air-yard completions -- a real projected count, unlike the
        # historical-replay-only proxy this field's docstring describes.
        pass_completion_40plus=_num(stats, "pass_cmp_40p"),
        rush_att=_num(stats, "rush_att"),
        rush_yds=_num(stats, "rush_yd"),
        rush_td=_num(stats, "rush_td"),
        rush_2pt=_num(stats, "rush_2pt"),
        rush_40plus=_num(stats, "rush_40p"),
        rec=_num(stats, "rec"),
        rec_yds=_num(stats, "rec_yd"),
        rec_td=_num(stats, "rec_td"),
        rec_2pt=_num(stats, "rec_2pt"),
        rec_40plus=_num(stats, "rec_40p"),
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


def _season_offense_stat_line(stats: dict) -> StatLine:
    """Season-cumulative `StatLine` for QB/RB/WR/TE, built from whichever of
    the season endpoint's offensive fields are actually present.

    Verified live: the core passing/rushing/receiving totals always are; the
    40+-play bonus counts (`pass_cmp_40p`/`rush_40p`/`rec_40p`) are present
    inconsistently across positions at this endpoint (unlike the weekly
    endpoint, where all three always are — see `_stat_line`). Missing ones
    stay `None` here and therefore contribute nothing, never silently zeroed
    — the same "absent means unknown, not zero" contract every other
    `StatLine` field already follows.
    """
    return StatLine(
        pass_att=_num(stats, "pass_att"),
        pass_cmp=_num(stats, "pass_cmp"),
        pass_yds=_num(stats, "pass_yd"),
        pass_td=_num(stats, "pass_td"),
        pass_int=_num(stats, "pass_int"),
        pass_2pt=_num(stats, "pass_2pt"),
        pass_completion_40plus=_num(stats, "pass_cmp_40p"),
        rush_att=_num(stats, "rush_att"),
        rush_yds=_num(stats, "rush_yd"),
        rush_td=_num(stats, "rush_td"),
        rush_2pt=_num(stats, "rush_2pt"),
        rush_40plus=_num(stats, "rush_40p"),
        rec=_num(stats, "rec"),
        rec_yds=_num(stats, "rec_yd"),
        rec_td=_num(stats, "rec_td"),
        rec_2pt=_num(stats, "rec_2pt"),
        rec_40plus=_num(stats, "rec_40p"),
        fumbles_lost=_num(stats, "fum_lost"),
    )


def _kdef_season_ratio(
    season: int,
    league: "LeagueScoring",
    cache_dir: Path | str,
    opener: UrlOpener,
    now: float | None,
    sample_weeks: Sequence[int],
) -> dict[str, float]:
    """Per-position (K/DEF) ratio of league-scored to consensus points,
    pooled across `sample_weeks` of real WEEKLY projections.

    The season endpoint's own K/DEF shapes are exactly the ones this
    module's docstring already warns are unsafe to reconstruct into a
    `StatLine` (K's flat `fgm`/`fga` with no distance split, DEF's bucketed
    game-count fields) — so rather than guess at those, this reuses the
    weekly path's exact per-game `StatLine`s (`fetch_weekly_rows`, already
    proven safe for K/DEF) and turns the result into a single expected-value
    ratio, applied to the season consensus total by the caller. Accurate
    whenever a kicker/defense's week-to-week shape stays roughly stable
    across the sample; off only for one whose distance mix or opponent slate
    happens to be unusually skewed in exactly the sampled weeks.

    A week that fails to fetch is skipped, not fatal — a partial sample is
    still a better ratio than none. An entirely-failed sample returns `{}`,
    and the caller falls back to plain consensus for K/DEF — the same
    degrade-never-crash contract every live seam in this repo follows.
    """
    league_sum: dict[str, float] = {"K": 0.0, "DEF": 0.0}
    consensus_sum: dict[str, float] = {"K": 0.0, "DEF": 0.0}
    for wk in sample_weeks:
        try:
            rows = fetch_weekly_rows(season, wk, cache_dir=cache_dir, opener=opener, now=now)
        except ProjectionFetchError:
            continue
        for row in rows:
            position = row.get("position")
            if position not in ("K", "DEF"):
                continue
            stats = row.get("stats")
            consensus = row.get("points")
            if stats is None or consensus is None:
                continue
            league_points, _flags = score_statline(stats, position, league)
            league_sum[position] += league_points
            consensus_sum[position] += consensus
    return {
        pos: league_sum[pos] / consensus_sum[pos]
        for pos in ("K", "DEF")
        if consensus_sum[pos] > 0
    }


def fetch_season_points_rows(
    season: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = 360.0,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
    league: "LeagueScoring | None" = None,
    kdef_sample_weeks: Sequence[int] = (1, 2, 3, 4),
) -> list[dict]:
    """Season-total points for the draft-relevant pool, from Sleeper's same
    (undocumented) projections endpoint the weekly path uses, grouped by
    season instead of week. Feeds `ffbot.board.load_board`'s
    `extra_points_rows` — the hybrid draft-board design (CLAUDE.md): Sleeper
    supplies live season POINTS, `draft.board_csv`'s FantasyPros exports
    still supply ADP, bye weeks, and cross-site ADP spread, none of which
    this endpoint carries at all.

    `league is None` (the default) is an EXACT no-op, bit-identical to
    before this function could league-score anything: every row keeps
    Sleeper's own `pts_ppr` as a plain consensus number, `stats: None`, and
    no `points_fp`/`points_source`/`points_flags` keys at all — the same
    shape FantasyPros' own points already sit on, so `apply_league_scoring`
    downstream leaves it untouched.

    `league` set league-scores what it safely can and is honest about the
    rest: QB/RB/WR/TE get a real `StatLine` reconstruction (season
    cumulative stats ARE reliably present for these — see
    `_season_offense_stat_line`), each row gaining `points_fp` (the original
    consensus number, restoring `edge.scoring_edge`'s ability to see a
    nonzero gap), `points_source: "league"`, and `points_flags` from
    `score_statline`. K/DEF cannot be reconstructed this way (see
    `_kdef_season_ratio`'s docstring for why) — they're instead corrected by
    a pooled league/consensus ratio sampled from real weekly projections,
    flagged `season_ratio_estimated` so a caller can tell the difference
    from an exact recompute. A K/DEF row is left on plain consensus (still
    labeled `points_source: "consensus"` for anyone downstream who reads it)
    only if the ratio sample itself came back empty (a fetch failure on
    every sampled week).

    Delegates the actual HTTP+cache work to
    `ffbot.sleeper.client.SleeperClient.season_projections` rather than
    building a second URL/cache path here — this function only owns the
    row-shape translation (and, when `league` is set, the K/DEF ratio
    sample's weekly fetches, via the same `fetch_weekly_rows` the in-season
    path already uses).
    """
    from ..sleeper.client import SleeperClient  # local import: keeps this package's own import graph one-directional until a season overlay is actually requested

    client = SleeperClient(cache_dir=cache_dir, opener=opener, now=now)
    entries = client.season_projections(season, ttl_minutes=ttl_minutes)

    kdef_ratio: dict[str, float] = {}
    if league is not None:
        kdef_ratio = _kdef_season_ratio(season, league, cache_dir, opener, now, kdef_sample_weeks)

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

        row = {
            "name": name,
            "team": (player.get("team") or "").strip().upper(),
            "position": position,
            "points": points,
            "bye": None,  # Sleeper carries no bye field on this endpoint either -- the board CSV source fills it in
            "stats": None,  # never a StatLine here -- see this function's and _kdef_season_ratio's docstrings for why
        }
        if league is not None:
            if position in ("QB", "RB", "WR", "TE"):
                stat_line = _season_offense_stat_line(stats)
                league_points, flags = score_statline(stat_line, position, league)
                row["points"] = league_points
                row["points_fp"] = points
                row["points_source"] = "league"
                row["points_flags"] = flags
            elif position in kdef_ratio:
                row["points"] = points * kdef_ratio[position]
                row["points_fp"] = points
                row["points_source"] = "league"
                row["points_flags"] = ("season_ratio_estimated",)
            else:
                row["points_fp"] = points
                row["points_source"] = "consensus"
                row["points_flags"] = ()
        rows.append(row)
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
