"""Point-in-time projections — "what a manager would have believed before
kickoff" — for historical replay (see docs/dev/BACKTEST.md, milestone B2).

Two engines, both returning `{actuals_key: projected_points}` for one
`(season, week)`, interchangeable at every call site:

- `naive_projections` — a recency-weighted mean of the player's own
  league-scored prior weeks *this season only* (never the week under test),
  blended toward last season's per-game average for players with little
  in-season sample, and regressed toward a positional replacement level
  (derived via `board.derive_replacement`, the same exact-optimizer
  derivation the live board uses — never hand-assumed) for anyone with none
  at all. Fully mechanical; usable back to 2009 (bounded by `injuries`, not
  by `stats_player_week`, which itself goes to 1999).

- `ecr_projections` — FantasyPros' rest-of-season Expert Consensus Rank,
  the latest scrape strictly before the target week's first game day,
  converted rank -> points through a calibration curve fit on OTHER seasons
  only. Clean window: 2021-2024 (see docs/dev/BACKTEST.md's research — 2020 is
  partial-season, 2025's archive is preseason-only). Fitting the calibration
  on the season being graded is look-ahead leakage of the worst kind (it
  would let a backtest "predict" using information from the outcome it is
  supposedly forecasting), so `ecr_projections` refuses a `fit_seasons` that
  includes `season` rather than silently doing the wrong thing.

`player_pool` + `players_asof` turn either engine's output, plus a
`WeekSnapshot`, into `list[ffbot.models.Player]` — the historical parallel to
`ffbot.roster_source._row_to_player` — so `week.adjusted_players` and
`lineup.optimize` are callable completely unchanged against real history.
"""

from __future__ import annotations

import datetime as _dt
import statistics
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from ..board import derive_replacement
from ..config import Config, LeagueScoring
from ..models import BENCH, Player
from ..names import NFL_TEAMS, normalize_name
from .actuals import points_allowed_for, score_defense_row, score_player_row
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_rows
from .names import actuals_key, canonical_team

if TYPE_CHECKING:
    from .index import WeekSnapshot

_SCORABLE_PLAYER_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})
_ALL_TEAMS = sorted(set(NFL_TEAMS.values()))


def _game_row_for_team(team: str, week_games: Sequence[dict]) -> Optional[dict]:
    """Duplicated from `ffbot.history.actuals` (a private helper there too)
    rather than imported — same small-helper-duplication convention this
    package already uses for `_num` between `actuals.py`/`index.py`."""
    for row in week_games:
        if team in (canonical_team(row.get("home_team")), canonical_team(row.get("away_team"))):
            return row
    return None


# --- Shared season/week game-log plumbing -----------------------------------
#
# Both engines need "this player's real, league-scored points in week W of
# season S" — `naive_projections` for its own history, `ecr_projections` for
# fitting the rank->points curve. One shared builder, not two, so the two
# engines can never quietly disagree about how a historical score is derived.


def _game_log(
    season: int,
    scoring: LeagueScoring,
    cache_dir: Path | str,
    opener: UrlOpener,
    before_week: Optional[int] = None,
) -> dict[str, list[tuple[int, float]]]:
    """`{actuals_key: [(week, points), ...]}` (sorted by week) for one
    season. `before_week=None` returns the whole season (used for a
    completed prior season's per-game average); `before_week=W` returns only
    weeks `< W` — the leakage boundary `naive_projections` relies on.
    """
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)

    player_rows = fetch_rows("stats_player_week", season=season, cache_dir=cache_dir, opener=opener)
    for row in player_rows:
        try:
            w = int(row.get("week", -1))
        except (TypeError, ValueError):
            continue
        if before_week is not None and w >= before_week:
            continue
        position = (row.get("position") or "").strip().upper()
        if position not in _SCORABLE_PLAYER_POSITIONS:
            continue
        name = row.get("player_display_name") or ""
        if not name:
            continue
        pts, _flags = score_player_row(row, scoring)
        out[actuals_key(name, position)].append((w, pts))

    team_rows = fetch_rows("stats_team_week", season=season, cache_dir=cache_dir, opener=opener)
    game_rows = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    games_by_week: dict[int, list[dict]] = defaultdict(list)
    for g in game_rows:
        try:
            if int(g.get("season", -1)) != season:
                continue
            gw = int(g.get("week", -1))
        except (TypeError, ValueError):
            continue
        games_by_week[gw].append(g)

    for row in team_rows:
        try:
            w = int(row.get("week", -1))
        except (TypeError, ValueError):
            continue
        if before_week is not None and w >= before_week:
            continue
        team = canonical_team(row.get("team"))
        if not team:
            continue
        game_row = _game_row_for_team(team, games_by_week.get(w, []))
        points_allowed = points_allowed_for(team, game_row) if game_row else None
        pts, _flags = score_defense_row(row, points_allowed, scoring)
        out[actuals_key(team, "DEF")].append((w, pts))

    for v in out.values():
        v.sort()
    return out


# --- Engine 1: naive (recency + prior-season + replacement regression) ------


def naive_projections(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[str, float]:
    """Recency-weighted, regressed projection for every player with at least
    one prior-season or in-season game logged before `(season, week)`.

    Reuses `cfg.projection.recency_window`/`recency_weight` (the live
    `lineup._fallback_points` blend) rather than inventing new knobs. For a
    player with `n` games logged so far this season:

      blended = recency_weight * mean(last `recency_window` games)
              + (1 - recency_weight) * mean(all `n` games so far)

    shrunk toward a floor value by `w = min(1, n / recency_window)`:

      projected = w * blended + (1 - w) * floor

    where `floor` is the player's own prior-season per-game average when one
    exists, else the position's in-progress replacement level (derived via
    `board.derive_replacement` from every other player's season-to-date
    average at that position — never hand-assumed, same invariant the live
    board holds). A player with zero games anywhere (a true unknown, e.g. a
    week-1 rookie) gets the floor value outright.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()

    this_season = _game_log(season, scoring, cache_dir, opener, before_week=week)
    prior_season = _game_log(season - 1, scoring, cache_dir, opener, before_week=None) if season > 1999 else {}

    replacement_rows = [
        {
            "name": key.rsplit(":", 1)[0],
            "position": key.rsplit(":", 1)[1],
            "points": statistics.fmean(pts for _w, pts in games),
        }
        for key, games in this_season.items()
        if games
    ]
    _, replacement = derive_replacement(replacement_rows, cfg.roster_positions, cfg.draft.num_teams, cfg)

    recency_window = max(1, cfg.projection.recency_window)
    recency_weight = cfg.projection.recency_weight

    out: dict[str, float] = {}
    for key in set(this_season) | set(prior_season):
        position = key.rsplit(":", 1)[1]
        games = this_season.get(key, [])
        n = len(games)

        prior_games = prior_season.get(key) or []
        prior_avg = statistics.fmean(p for _w, p in prior_games) if prior_games else None

        if n == 0:
            out[key] = prior_avg if prior_avg is not None else replacement.get(position, 0.0)
            continue

        points_only = [p for _w, p in games]
        recent_avg = statistics.fmean(points_only[-recency_window:])
        season_avg = statistics.fmean(points_only)
        blended = recency_weight * recent_avg + (1.0 - recency_weight) * season_avg

        floor_value = prior_avg if prior_avg is not None else replacement.get(position, blended)
        w_reg = min(1.0, n / recency_window)
        out[key] = w_reg * blended + (1.0 - w_reg) * floor_value

    return out


# --- Engine 2: ecr (FantasyPros ROS rank -> points, prior-seasons-only fit) -


# nflverse's per-position ROS ("rest of season") redraft-PPR page paths,
# `ecr_type == "rp"` — verified live against the DynastyProcess archive
# during scoping (see docs/dev/BACKTEST.md). No weekly matchup-aware rankings
# exist in the free archive; ROS is matchup-agnostic by construction, which
# is exactly the right frozen-projection control for testing whether
# `week.adjusted_players`' matchup layer adds anything over it.
_ECR_PAGE_POSITIONS: dict[str, str] = {
    "/nfl/rankings/ros-qb.php": "QB",
    "/nfl/rankings/ros-ppr-rb.php": "RB",
    "/nfl/rankings/ros-ppr-wr.php": "WR",
    "/nfl/rankings/ros-ppr-te.php": "TE",
    "/nfl/rankings/ros-k.php": "K",
    "/nfl/rankings/ros-dst.php": "DEF",
}

# 2020 is partial-season (scrapes start mid-October); 2025's archive is
# preseason-only (stops in August, before week 1 kicks off). 2021-2024 is
# the clean window with a full in-season scrape cadence — see
# docs/dev/BACKTEST.md's research table.
ECR_CLEAN_SEASONS: tuple[int, ...] = (2021, 2022, 2023, 2024)

# Process-lifetime memoization — see the module note above `ecr_projections`.
# Keyed by object identity (cache_dir string + id(opener)/id(cfg)) rather
# than a proper cache-invalidation scheme: this module has no mutation path
# for cached historical files mid-process, so identity is a sufficient key
# and a fresh test opener naturally gets a fresh cache entry.
_ecr_by_date_cache: dict[tuple[str, int], dict[str, list[dict]]] = {}
_game_days_cache: dict[tuple[str, int], dict[tuple[int, int], str]] = {}
_curve_cache: dict[tuple, dict[str, list[tuple[float, float]]]] = {}


def _load_ros_ecr_by_date(cache_dir: Path | str, opener: UrlOpener) -> dict[str, list[dict]]:
    cache_key = (str(cache_dir), id(opener))
    if cache_key in _ecr_by_date_cache:
        return _ecr_by_date_cache[cache_key]

    rows = fetch_rows("ff_ecr", cache_dir=cache_dir, opener=opener)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("ecr_type") != "rp":
            continue
        if row.get("fp_page") not in _ECR_PAGE_POSITIONS:
            continue
        scrape_date = (row.get("scrape_date") or "")[:10]
        if not scrape_date:
            continue
        by_date[scrape_date].append(row)

    result = dict(by_date)
    _ecr_by_date_cache[cache_key] = result
    return result


def _first_game_days(cache_dir: Path | str, opener: UrlOpener) -> dict[tuple[int, int], str]:
    cache_key = (str(cache_dir), id(opener))
    if cache_key in _game_days_cache:
        return _game_days_cache[cache_key]

    rows = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    out: dict[tuple[int, int], str] = {}
    for row in rows:
        try:
            season = int(row.get("season", -1))
            week = int(row.get("week", -1))
        except (TypeError, ValueError):
            continue
        day = (row.get("gameday") or "").strip()
        if not day:
            continue
        key = (season, week)
        if key not in out or day < out[key]:
            out[key] = day

    _game_days_cache[cache_key] = out
    return out


def _latest_scrape_before(target_day: str, available_dates: Sequence[str]) -> Optional[str]:
    """The latest `scrape_date` that is strictly earlier than `target_day`
    (a calendar day, e.g. `"2023-10-08"`). Comparing whole calendar days
    rather than a timestamp is a deliberate conservatism: `scrape_date` has
    no time-of-day resolution, so treating a same-day scrape as "before
    kickoff" could leak past an early Thursday-night game — see
    docs/dev/BACKTEST.md's leakage register."""
    candidates = [d for d in available_dates if d < target_day]
    return max(candidates) if candidates else None


def _ecr_snapshot(scrape_date: str, ecr_by_date: dict[str, list[dict]]) -> dict[str, float]:
    """`{actuals_key: positional rank}` for one scrape date, across every
    ROS-rp position page."""
    out: dict[str, float] = {}
    for row in ecr_by_date.get(scrape_date, []):
        position = _ECR_PAGE_POSITIONS.get(row.get("fp_page", ""))
        if position is None:
            continue
        name = row.get("player") or ""
        if not name:
            continue
        try:
            rank = float(row.get("ecr"))
        except (TypeError, ValueError):
            continue
        key = actuals_key(name, position) if position != "DEF" else actuals_key(row.get("team") or name, "DEF")
        out[key] = rank
    return out


def _interp(rank: float, curve: list[tuple[float, float]]) -> float:
    """Piecewise-linear rank -> points, flat beyond either end of the
    fitted curve (a reasonable, documented approximation for a rank the fit
    seasons never observed at that position — e.g. a QB2 buried past
    everyone the fit data ever ranked that low)."""
    if not curve:
        return 0.0
    if rank <= curve[0][0]:
        return curve[0][1]
    if rank >= curve[-1][0]:
        return curve[-1][1]
    for (r0, p0), (r1, p1) in zip(curve, curve[1:]):
        if r0 <= rank <= r1:
            if r1 == r0:
                return p0
            frac = (rank - r0) / (r1 - r0)
            return p0 + frac * (p1 - p0)
    return curve[-1][1]


def _fit_rank_to_points_curve(
    fit_seasons: Sequence[int],
    cfg: Config,
    ecr_by_date: dict[str, list[dict]],
    game_days: dict[tuple[int, int], str],
    cache_dir: Path | str,
    opener: UrlOpener,
) -> dict[str, list[tuple[float, float]]]:
    """`{position: sorted [(rank, avg_points), ...]}`, fit by joining, for
    every `(season, week)` among `fit_seasons`, the latest ROS-rp ECR
    snapshot strictly before that week's first game day against that
    player's real league-scored points that same week. Ranks are bucketed
    to the nearest integer and averaged across every observation."""
    cache_key = (tuple(sorted(fit_seasons)), id(cfg), str(cache_dir), id(opener))
    if cache_key in _curve_cache:
        return _curve_cache[cache_key]

    scoring = cfg.league or LeagueScoring.fantasypros_default()
    available_dates = sorted(ecr_by_date)
    buckets: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    for fit_season in fit_seasons:
        game_log = _game_log(fit_season, scoring, cache_dir, opener, before_week=None)
        points_by_week: dict[str, dict[int, float]] = defaultdict(dict)
        weeks_played: set[int] = set()
        for key, games in game_log.items():
            for w, pts in games:
                points_by_week[key][w] = pts
                weeks_played.add(w)

        for week in sorted(weeks_played):
            day = game_days.get((fit_season, week))
            if day is None:
                continue
            scrape = _latest_scrape_before(day, available_dates)
            if scrape is None:
                continue
            snapshot = _ecr_snapshot(scrape, ecr_by_date)
            for key, rank in snapshot.items():
                pts = points_by_week.get(key, {}).get(week)
                if pts is None:
                    continue
                position = key.rsplit(":", 1)[1]
                buckets[position][round(rank)].append(pts)

    curve = {
        position: sorted((rank, statistics.fmean(pts)) for rank, pts in rank_buckets.items())
        for position, rank_buckets in buckets.items()
    }
    _curve_cache[cache_key] = curve
    return curve


# A per-week "how stale is the scrape we found" check does NOT work here --
# EVERY ECR_CLEAN_SEASONS year shows a genuine 250+ day gap at week 1 (the
# archive's freshest scrape is still December of the PRIOR season, since
# this season's own ROS coverage hasn't started publishing yet) that
# collapses to a normal ~6-day cadence by week 2-3. A flat per-week age
# threshold tight enough to catch a truly frozen archive (see below) also
# fires on that completely normal early-season cold start, in every season,
# every year -- verified against the cached archive:
#   week 1: 2021=259d, 2022=251d, 2023=251d, 2024=251d (all "normal")
#   week 3: 2021=6d,   2022=6d,   2023=6d,   2024=265d (still ramping up)
#   week 5: every year already down to ~6d
# So instead this checks SEASON-LEVEL coverage once: does the archive ever
# publish a scrape within `_IN_SEASON_COVERAGE_WINDOW_DAYS` of this season's
# own week-1 kickoff? A season with real in-season coverage always clears
# that (worst observed: 22 days, 2024) regardless of how slow the very
# first week or two looks. A season with NO in-season coverage at all --
# the archive frozen at 2025-08-08, `--source ecr --seasons 2025` querying
# any week and silently reusing the 2024-12-27 scrape forever, with
# clean-looking numbers and no error -- fails this check for every week of
# that season. See docs/dev/BACKTEST.md's data-freshness notes for the incident.
_IN_SEASON_COVERAGE_WINDOW_DAYS = 45

_season_coverage_cache: dict[tuple[str, int], bool] = {}


def _season_has_in_season_ecr_coverage(
    season: int,
    ecr_by_date: dict[str, list[dict]],
    game_days: dict[tuple[int, int], str],
    cache_dir: Path | str,
) -> bool:
    """Whether the archive published ANY ROS-ECR scrape within
    `_IN_SEASON_COVERAGE_WINDOW_DAYS` of `season`'s own week-1 kickoff --
    see the module comment above `_IN_SEASON_COVERAGE_WINDOW_DAYS` for why
    this is the right question to ask, not "is the latest pre-kickoff
    scrape recent." Process-lifetime cached, same reasoning as
    `_curve_cache`."""
    cache_key = (str(cache_dir), season)
    if cache_key in _season_coverage_cache:
        return _season_coverage_cache[cache_key]

    day1 = game_days.get((season, 1))
    if day1 is None:
        _season_coverage_cache[cache_key] = False
        return False
    window_end = (_dt.date.fromisoformat(day1) + _dt.timedelta(days=_IN_SEASON_COVERAGE_WINDOW_DAYS)).isoformat()
    covered = any(day1 <= d <= window_end for d in ecr_by_date)
    _season_coverage_cache[cache_key] = covered
    return covered


def ecr_projections(
    season: int,
    week: int,
    cfg: Config,
    fit_seasons: Optional[Sequence[int]] = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[str, float]:
    """FantasyPros ROS-ECR, converted to points via a rank->points curve fit
    on `fit_seasons` (default: every season in `ECR_CLEAN_SEASONS` other
    than `season` itself). Raises `ValueError` if `season in fit_seasons` —
    calibrating on the season being graded is look-ahead leakage, so this is
    refused rather than silently permitted (see the module docstring). Also
    raises if `season` never got a single in-season ROS-ECR scrape at all
    (`_season_has_in_season_ecr_coverage` — see that function's own comment
    for the incident this closes) — every week of a season with no genuine
    coverage would otherwise silently reuse a prior season's stale scrape
    forever, with clean-looking numbers and no error.

    Returns `{}` for a week with no qualifying pre-kickoff scrape at all
    (before the archive's coverage begins, or a week with no cached game
    schedule) rather than raising — a caller replaying a season should treat
    an empty dict as "no ecr projections available this week," the same
    contract `week.load_weekly_intel` uses for a missing file.
    """
    if fit_seasons is None:
        fit_seasons = tuple(s for s in ECR_CLEAN_SEASONS if s != season)
    else:
        fit_seasons = tuple(fit_seasons)
    if season in fit_seasons:
        raise ValueError(
            f"fit_seasons must not include the season under test ({season}) — "
            "calibrating rank->points on the season being graded is look-ahead leakage"
        )

    ecr_by_date = _load_ros_ecr_by_date(cache_dir, opener)
    game_days = _first_game_days(cache_dir, opener)

    day = game_days.get((season, week))
    if day is None:
        return {}
    scrape = _latest_scrape_before(day, sorted(ecr_by_date))
    if scrape is None:
        return {}

    if not _season_has_in_season_ecr_coverage(season, ecr_by_date, game_days, cache_dir):
        raise ValueError(
            f"season {season} has no ROS-ECR scrape anywhere within "
            f"{_IN_SEASON_COVERAGE_WINDOW_DAYS} days of its own week-1 kickoff — the DynastyProcess "
            f"archive has likely stopped updating before this season started (see docs/dev/BACKTEST.md's "
            f"data-freshness notes); every week of this season would silently reuse a prior season's "
            f"stale scrape. Refresh it via scripts/history_fetch.py --sources ff_ecr --refresh, use "
            "--source naive for this season instead, or pass fit_seasons/an alternate cache_dir if "
            "this is intentional"
        )

    snapshot = _ecr_snapshot(scrape, ecr_by_date)

    curve = _fit_rank_to_points_curve(fit_seasons, cfg, ecr_by_date, game_days, cache_dir, opener)
    return {key: _interp(rank, curve.get(key.rsplit(":", 1)[1], [])) for key, rank in snapshot.items()}


# --- Assembling Players -----------------------------------------------------


def player_pool(
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> list[dict]:
    """Every offense/kicker player rostered anywhere in `(season, week)`,
    plus all 32 defenses — the identity universe `ffbot.backtest.rosters`
    samples from, and what `players_asof` builds `Player`s out of.

    Sourced from `roster_weekly` rather than `stats_player_week`: the stats
    file only lists players who actually recorded a stat line that week,
    which silently excludes a healthy backup or an inactive — exactly the
    wrong thing for a roster-sampling universe, which needs to include
    players a real manager could have rostered whether or not they scored.
    """
    rows = fetch_rows("roster_weekly", season=season, cache_dir=cache_dir, opener=opener)
    out: dict[str, dict] = {}
    for row in rows:
        try:
            if int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue
        position = (row.get("position") or "").strip().upper()
        if position not in _SCORABLE_PLAYER_POSITIONS:
            continue
        name = row.get("full_name") or ""
        if not name:
            continue
        team = canonical_team(row.get("team"))
        key = actuals_key(name, position)
        out[key] = {"key": key, "name": name, "position": position, "team": team}

    for team in _ALL_TEAMS:
        key = actuals_key(team, "DEF")
        out[key] = {"key": key, "name": team, "position": "DEF", "team": team}

    return list(out.values())


def players_asof(
    pool: Sequence[dict],
    projections: dict[str, float],
    snapshot: "WeekSnapshot",
) -> list[Player]:
    """`pool` rows (from `player_pool`) + a projection dict + a
    `WeekSnapshot` -> `list[Player]` — the historical parallel to
    `ffbot.roster_source._row_to_player`. `week.adjusted_players` and
    `lineup.optimize` take the result completely unchanged; this is the
    load-bearing property that lets B3's replayer run those two functions
    against real history with zero changes to either.

    A pool row with no entry in `projections` (a true unknown — e.g. a
    rookie's first career game under the `naive` engine before any
    replacement-level data exists for them) gets `projected_points=None`,
    which `lineup.score_player`/`_fallback_points` already handle correctly;
    nothing here needs to special-case it.
    """
    out: list[Player] = []
    for uid, row in enumerate(pool, start=1):
        name = row["name"]
        entry = snapshot.player_status.get(normalize_name(name))
        out.append(
            Player(
                player_id=uid,
                name=name,
                eligible_positions=[row["position"]],
                selected_position=BENCH,
                team=row["team"],
                status=entry.status if entry else "",
                projected_points=projections.get(row["key"]),
            )
        )
    return out
