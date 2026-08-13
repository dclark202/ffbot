"""Historical preseason draft board — the historical parallel to
`ffbot.board.load_board`, for B4's draft simulator (see docs/BACKTEST.md).

Two ingredients, both from already-cached sources:

- Positional rank, from the latest FantasyPros preseason cheatsheet ECR
  scrape strictly before that season's week 1 kickoff (`_PRESEASON_PAGE_POSITIONS`
  below) — the draft-season analog of `projections.ecr_projections`'s ROS
  pages, same `ecr_type == "rp"` archive.
- ADP + stdev, from Fantasy Football Calculator's free API (`ffc_adp`).

Rank -> points is a SEASON-total calibration curve (unlike
`projections._fit_rank_to_points_curve`'s per-game one), fit on seasons OTHER
than the one under test — the same prior-seasons-only guard,
`ecr_projections`'s reasoning: fitting on the season being drafted would let
the draft simulator "see" the outcome it's supposedly blind to.

Once points/ADP exist per player, this reuses `ffbot.board.derive_replacement`
and `ffbot.board.assign_tiers` completely unchanged — the same exact-optimizer
derivation and gap-based tiering the live board uses, never hand-assumed.
`historical_board`'s final assembly (sort by VOR desc, assign `rank`,
`_compute_tier_last`) mirrors `board.load_board`'s own, for the same reason:
this IS the historical version of that function.
"""

from __future__ import annotations

import datetime as _dt
import statistics
from collections import defaultdict
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Optional, Sequence

from ..board import Board, BoardPlayer, _board_key, _compute_tier_last, assign_tiers, derive_replacement
from ..config import Config, LeagueScoring
from ..names import normalize_position
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_json, fetch_rows
from .names import actuals_key, canonical_team
from .projections import ECR_CLEAN_SEASONS, _first_game_days, _game_log, _interp, _latest_scrape_before

# Preseason (draft-season) FantasyPros cheatsheet pages, `ecr_type == "rp"` —
# verified live during scoping alongside the ROS pages
# (`projections._ECR_PAGE_POSITIONS`); available every season back to 2020.
# Real preseason cheatsheets are published within weeks of kickoff, never
# months before it -- a chosen scrape further back than this from the
# target season's week 1 is not "the preseason cheatsheet a manager would
# have seen," it's an archive that has fallen behind entirely (see
# `historical_board`'s staleness check). Generous on purpose: FantasyPros'
# earliest preseason content typically starts mid-July for a September
# kickoff (~60 days), so 90 comfortably covers every legitimate case while
# still catching an archive that's stopped updating for a whole year.
_MAX_PRESEASON_SCRAPE_AGE_DAYS = 90

_PRESEASON_PAGE_POSITIONS: dict[str, str] = {
    "/nfl/rankings/qb-cheatsheets.php": "QB",
    "/nfl/rankings/ppr-rb-cheatsheets.php": "RB",
    "/nfl/rankings/ppr-wr-cheatsheets.php": "WR",
    "/nfl/rankings/ppr-te-cheatsheets.php": "TE",
    "/nfl/rankings/k-cheatsheets.php": "K",
    "/nfl/rankings/dst-cheatsheets.php": "DEF",
}

# Process-lifetime memoization, same reasoning and same identity-based key as
# `projections._ecr_by_date_cache`/`_curve_cache` — this module has no
# mutation path for a cached historical file mid-process.
_preseason_ecr_cache: dict[tuple[str, int], dict[str, list[dict]]] = {}
_season_curve_cache: dict[tuple, dict[str, list[tuple[float, float]]]] = {}


def _load_preseason_ecr_by_date(cache_dir: Path | str, opener: UrlOpener) -> dict[str, list[dict]]:
    cache_key = (str(cache_dir), id(opener))
    if cache_key in _preseason_ecr_cache:
        return _preseason_ecr_cache[cache_key]

    rows = fetch_rows("ff_ecr", cache_dir=cache_dir, opener=opener)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("ecr_type") != "rp":
            continue
        if row.get("fp_page") not in _PRESEASON_PAGE_POSITIONS:
            continue
        scrape_date = (row.get("scrape_date") or "")[:10]
        if not scrape_date:
            continue
        by_date[scrape_date].append(row)

    result = dict(by_date)
    _preseason_ecr_cache[cache_key] = result
    return result


def _preseason_snapshot(scrape_date: str, ecr_by_date: dict[str, list[dict]]) -> dict[str, tuple[float, str, str]]:
    """`{actuals_key: (preseason positional rank, display name, team)}` for
    one scrape date.

    Carries the ORIGINAL display name and the ECR row's own team alongside
    the rank — `actuals_key` itself is built from `normalize_name`
    (lowercased, punctuation-stripped) for matching, which is exactly wrong
    for `BoardPlayer.name`/`.team`, fields real UI code shows to a user; the
    team is also a fallback for a player Fantasy Football Calculator's
    (much shallower — ~200 players) ADP coverage never reaches. A small
    duplicate of `projections._ecr_snapshot` against the preseason page set
    instead of the ROS one — same convention this package already uses for
    single-purpose helpers (`_num`, `_game_row_for_team`) rather than
    parameterizing an already-tested function.
    """
    out: dict[str, tuple[float, str, str]] = {}
    for row in ecr_by_date.get(scrape_date, []):
        position = _PRESEASON_PAGE_POSITIONS.get(row.get("fp_page", ""))
        if position is None:
            continue
        name = row.get("player") or ""
        if not name:
            continue
        try:
            rank = float(row.get("ecr"))
        except (TypeError, ValueError):
            continue
        team = canonical_team(row.get("team"))
        if position == "DEF":
            display = team or name
            key = actuals_key(display, "DEF")
        else:
            display = name
            key = actuals_key(name, position)
        out[key] = (rank, display, team)
    return out


def _fit_season_rank_to_points_curve(
    fit_seasons: Sequence[int],
    cfg: Config,
    ecr_by_date: dict[str, list[dict]],
    game_days: dict[tuple[int, int], str],
    cache_dir: Path | str,
    opener: UrlOpener,
) -> dict[str, list[tuple[float, float]]]:
    """`{position: sorted [(rank, avg_season_points), ...]}`, fit by joining
    each fit season's preseason rank snapshot against that player's REALIZED
    SEASON TOTAL (summed league-scored points across every week, via
    `projections._game_log(..., before_week=None)`) — the season-total
    analog of `projections._fit_rank_to_points_curve`'s per-game version.
    """
    cache_key = (tuple(sorted(fit_seasons)), id(cfg), str(cache_dir), id(opener))
    if cache_key in _season_curve_cache:
        return _season_curve_cache[cache_key]

    scoring = cfg.league or LeagueScoring.fantasypros_default()
    available_dates = sorted(ecr_by_date)
    buckets: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    for fit_season in fit_seasons:
        day = game_days.get((fit_season, 1))
        if day is None:
            continue
        scrape = _latest_scrape_before(day, available_dates)
        if scrape is None:
            continue
        snapshot = _preseason_snapshot(scrape, ecr_by_date)

        game_log = _game_log(fit_season, scoring, cache_dir, opener, before_week=None)
        season_totals = {key: sum(pts for _w, pts in games) for key, games in game_log.items()}

        for key, (rank, _display, _team) in snapshot.items():
            total = season_totals.get(key)
            if total is None:
                continue
            position = key.rsplit(":", 1)[1]
            buckets[position][round(rank)].append(total)

    curve = {
        position: sorted((rank, statistics.fmean(pts)) for rank, pts in rank_buckets.items())
        for position, rank_buckets in buckets.items()
    }
    _season_curve_cache[cache_key] = curve
    return curve


def _adp_by_key(adp_raw: dict) -> dict[str, dict]:
    """FFC's `players` list -> `{actuals_key: raw FFC row}`. FFC already
    gives a clean team abbreviation for defenses (`{"name": "San Francisco
    Defense", "team": "SF", "position": "DEF"}`) — keying off `team` rather
    than parsing the display name sidesteps the city-name matching problem
    `names.defense_key` exists for entirely."""
    out: dict[str, dict] = {}
    for row in adp_raw.get("players", []):
        position = normalize_position(row.get("position", ""))
        name = row.get("name") or ""
        if not name:
            continue
        if position == "DEF":
            team = canonical_team(row.get("team"))
            key = actuals_key(team, "DEF")
        else:
            key = actuals_key(name, position)
        out[key] = row
    return out


def historical_board(
    season: int,
    cfg: Config,
    num_teams: int = 12,
    fit_seasons: Optional[Sequence[int]] = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> Board:
    """The historical preseason draft board for `season`.

    Raises `ValueError` if `fit_seasons` (default: every `ECR_CLEAN_SEASONS`
    entry except `season`) contains the target season — calibrating rank ->
    points on the season being drafted is look-ahead leakage, refused rather
    than silently permitted, same as `projections.ecr_projections`. Also
    raises if no cached week-1 schedule or no qualifying preseason scrape
    exists for `season` — there is nothing to build a board from. And raises
    if the newest qualifying scrape is more than
    `_MAX_PRESEASON_SCRAPE_AGE_DAYS` before `season`'s week 1 — a chosen
    scrape that far back means the DynastyProcess archive has simply stopped
    updating (found via `scripts/demo_season.py`'s coverage report: with the
    archive frozen at 2025-08-08, this function silently returned a full
    year-stale preseason cheatsheet for `season=2026` with no error at all).
    """
    if fit_seasons is None:
        fit_seasons = tuple(s for s in ECR_CLEAN_SEASONS if s != season)
    else:
        fit_seasons = tuple(fit_seasons)
    if season in fit_seasons:
        raise ValueError(
            f"fit_seasons must not include the draft season under test ({season}) — "
            "calibrating rank->points on the season being drafted is look-ahead leakage"
        )

    ecr_by_date = _load_preseason_ecr_by_date(cache_dir, opener)
    game_days = _first_game_days(cache_dir, opener)

    day = game_days.get((season, 1))
    if day is None:
        raise ValueError(f"no cached week-1 game schedule for season {season}")
    scrape = _latest_scrape_before(day, sorted(ecr_by_date))
    if scrape is None:
        raise ValueError(f"no preseason ECR scrape cached strictly before season {season}'s week 1")

    age_days = (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(scrape)).days
    if age_days > _MAX_PRESEASON_SCRAPE_AGE_DAYS:
        raise ValueError(
            f"newest preseason ECR scrape cached before season {season}'s week 1 ({day}) is "
            f"{scrape}, {age_days} days earlier — the DynastyProcess archive has likely stopped "
            f"updating (see docs/BACKTEST.md); refresh it via scripts/history_fetch.py "
            "--sources ff_ecr --refresh, or pass fit_seasons/an alternate cache_dir if this is intentional"
        )

    snapshot = _preseason_snapshot(scrape, ecr_by_date)
    curve = _fit_season_rank_to_points_curve(fit_seasons, cfg, ecr_by_date, game_days, cache_dir, opener)
    adp_by_key = _adp_by_key(fetch_json("ffc_adp", season=season, cache_dir=cache_dir, opener=opener))

    rows: list[dict] = []
    for key, (rank, display_name, ecr_team) in snapshot.items():
        position = key.rsplit(":", 1)[1]
        points = _interp(rank, curve.get(position, []))
        adp_row = adp_by_key.get(key)
        bye = adp_row.get("bye") if adp_row else None
        # ADP's team wins when present (FFC's own data, same source as the
        # ADP number itself); the ECR row's team is the fallback for the
        # ~75% of the board FFC's shallower coverage never reaches.
        team = (adp_row.get("team") if adp_row else "") or ecr_team
        rows.append({
            "name": display_name,
            "position": position,
            "team": team,
            "bye": int(bye) if bye not in (None, "") else None,
            "points": points,
            "adp": adp_row.get("adp") if adp_row else None,
            "adp_stdev": adp_row.get("stdev") if adp_row else None,
        })

    starters_per_pos, replacement = derive_replacement(rows, cfg.roster_positions, num_teams, cfg)
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
                team=row["team"],
                bye_week=row["bye"],
                points=row["points"],
                adp=row["adp"],
                adp_stdev=row["adp_stdev"],
                adp_spread=None,
                platform_id=None,
                tier=tiers.get(key, 1),
                vor=row["points"] - repl,
                rank=0,
                points_fp=row["points"],
                points_source="consensus",
            )
        )

    board_players.sort(key=lambda bp: (-bp.vor, bp.name))
    board_players = [dc_replace(bp, rank=i + 1) for i, bp in enumerate(board_players)]

    return Board(
        players=board_players,
        by_key={bp.key: bp for bp in board_players},
        replacement=replacement,
        starters_per_pos=starters_per_pos,
        tier_last=_compute_tier_last(board_players),
    )
