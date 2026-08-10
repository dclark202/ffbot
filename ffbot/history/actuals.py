"""Turn one nflverse stats row into league-scored fantasy points.

Two source shapes feed this: `stats_player_week` rows (QB/RB/WR/TE/K, one row
per player per game) and `stats_team_week` + `games.csv` rows (DEF, scored as
a team unit the way Yahoo fantasy scores it — nflverse has no per-player
"defense" concept, so there is no per-player row to read for DEF at all).
Both funnel through `ffbot.scoring.StatLine`/`score_statline` unchanged —
deliberately reused rather than reimplemented, so `league.yml`'s rules are
applied exactly once, the same way, on live boards and on historical replay.

Real box scores let several fields FantasyPros' CSV exports can never carry
get scored exactly instead of estimated or left unmodeled entirely — see the
"Historical-replay-only fields" block on `ffbot.scoring.StatLine`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..config import LeagueScoring
from ..scoring import StatLine, score_statline
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_rows
from .names import actuals_key, canonical_team

# nflverse's fixed field-goal distance bands -> the same band-key convention
# as `KickingScoring.fg_by_distance`/`fg_missed_by_distance` ("0-19", ...,
# "60-") — see `scoring._FG_BAND_MIDPOINTS`.
_FG_MADE_COLUMNS = {
    "0-19": "fg_made_0_19", "20-29": "fg_made_20_29", "30-39": "fg_made_30_39",
    "40-49": "fg_made_40_49", "50-59": "fg_made_50_59", "60-": "fg_made_60_",
}
_FG_MISSED_COLUMNS = {
    "0-19": "fg_missed_0_19", "20-29": "fg_missed_20_29", "30-39": "fg_missed_30_39",
    "40-49": "fg_missed_40_49", "50-59": "fg_missed_50_59", "60-": "fg_missed_60_",
}


def _num(row: dict, field: str) -> Optional[float]:
    """CSV cells are strings; a blank cell means "not attempted," not zero —
    the same "None means we genuinely don't know" contract `StatLine`
    documents for a FantasyPros row."""
    val = row.get(field)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def player_statline(row: dict) -> StatLine:
    """`stats_player_week` row -> `StatLine`. Position-agnostic, like a
    FantasyPros row: every field the row's own position never produced stays
    None rather than 0.0."""
    fg_made_bands = {k: _num(row, col) or 0.0 for k, col in _FG_MADE_COLUMNS.items()}
    fg_missed_bands = {k: _num(row, col) or 0.0 for k, col in _FG_MISSED_COLUMNS.items()}
    has_fg_bands = any(fg_made_bands.values()) or any(fg_missed_bands.values())

    pat_missed_raw = _num(row, "pat_missed")
    pat_blocked = _num(row, "pat_blocked")
    pat_missed = None
    if pat_missed_raw is not None or pat_blocked is not None:
        pat_missed = (pat_missed_raw or 0.0) + (pat_blocked or 0.0)

    return StatLine(
        pass_att=_num(row, "attempts"),
        pass_cmp=_num(row, "completions"),
        pass_yds=_num(row, "passing_yards"),
        pass_td=_num(row, "passing_tds"),
        pass_int=_num(row, "passing_interceptions"),
        rush_att=_num(row, "carries"),
        rush_yds=_num(row, "rushing_yards"),
        rush_td=_num(row, "rushing_tds"),
        rec=_num(row, "receptions"),
        rec_yds=_num(row, "receiving_yards"),
        rec_td=_num(row, "receiving_tds"),
        fumbles_lost=_num(row, "fumbles_lost_total"),
        fg_made=_num(row, "fg_made"),
        fg_att=_num(row, "fg_att"),
        pat_made=_num(row, "pat_made"),
        pat_missed=pat_missed,
        fg_made_bands=fg_made_bands if has_fg_bands else None,
        fg_missed_bands=fg_missed_bands if has_fg_bands else None,
        pass_2pt=_num(row, "passing_2pt_conversions"),
        rush_2pt=_num(row, "rushing_2pt_conversions"),
        rec_2pt=_num(row, "receiving_2pt_conversions"),
        # `passing_40` (nflfastR: count of 40+ air/total-yard completions) is
        # a proxy for `BonusScoring.pass_completion_40plus`, not an exact
        # match to how any one league defines "40+ yard completion" — see
        # docs/BACKTEST.md's leakage/approximation register.
        pass_completion_40plus=_num(row, "passing_40"),
    )


def defense_statline(team_row: dict, points_allowed_game: Optional[float]) -> StatLine:
    """`stats_team_week` row (+ that game's real points-allowed, from
    `games.csv` via `points_allowed_for`) -> `StatLine` for DEF/D-ST.

    `def_td` sums nflverse's `def_tds` (interception/fumble-return TDs) and
    `special_teams_tds` (kick/punt-return, blocked-kick-return TDs) — Yahoo's
    `DefenseScoring.touchdown` pays either kind, while nflverse splits them
    into two columns where a FantasyPros DEF export just has one `TD` total.
    """
    def_tds = _num(team_row, "def_tds") or 0.0
    st_tds = _num(team_row, "special_teams_tds") or 0.0

    return StatLine(
        sack=_num(team_row, "def_sacks"),
        interception=_num(team_row, "def_interceptions"),
        fumble_recovery=_num(team_row, "def_fumbles"),
        forced_fumble=_num(team_row, "def_fumbles_forced"),
        def_td=def_tds + st_tds,
        safety=_num(team_row, "def_safeties"),
        points_allowed_game=points_allowed_game,
    )


def points_allowed_for(team: str, game_row: dict) -> Optional[float]:
    """`team`'s opponent's score in one `games.csv` row, or None if `team`
    didn't play in that game at all (a mismatched call, not "team shut them
    out" — that case is `0.0`, not `None`)."""
    home = (game_row.get("home_team") or "").strip().upper()
    away = (game_row.get("away_team") or "").strip().upper()
    team = (team or "").strip().upper()
    if team == home:
        return _num(game_row, "away_score")
    if team == away:
        return _num(game_row, "home_score")
    return None


def score_player_row(row: dict, scoring: LeagueScoring) -> tuple[float, tuple[str, ...]]:
    """`stats_player_week` row -> league-scored `(points, flags)`."""
    position = (row.get("position") or "").strip().upper()
    return score_statline(player_statline(row), position, scoring)


def score_defense_row(
    team_row: dict, points_allowed_game: Optional[float], scoring: LeagueScoring
) -> tuple[float, tuple[str, ...]]:
    """`stats_team_week` row -> league-scored `(points, flags)` for DEF."""
    return score_statline(defense_statline(team_row, points_allowed_game), "DEF", scoring)


# --- The grading key -------------------------------------------------------
#
# `week_actuals` is the ONE function `ffbot.backtest` may call to find out
# what really happened — see docs/BACKTEST.md's leakage register. It pulls
# straight from `stats_player_week`/`stats_team_week`/`games` (all
# results-bearing sources `ffbot.history.index.as_of` deliberately never
# touches) and must never be called by anything that also feeds a decision.

# Positions a real roster ever starts. `stats_player_week` also carries rows
# for OL/DL/LB/etc. (special-teams and misc box-score participants) with
# irrelevant or empty stat lines — filtering here is cheap and avoids
# emitting nonsense keys nothing will ever look up.
_SCORABLE_PLAYER_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})


def _rows_for_week(rows: Sequence[dict], season: int, week: int) -> list[dict]:
    out = []
    for row in rows:
        try:
            if int(row.get("season", -1)) != season or int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue
        out.append(row)
    return out


def _game_row_for_team(team: str, week_games: Sequence[dict]) -> Optional[dict]:
    for row in week_games:
        if team in (canonical_team(row.get("home_team")), canonical_team(row.get("away_team"))):
            return row
    return None


def week_actuals(
    season: int,
    week: int,
    scoring: LeagueScoring,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[str, float]:
    """Realized league-scored points for every player and defense that
    played in `(season, week)`, keyed by `ffbot.history.names.actuals_key`
    (the same `f"{normalized_name}:{position}"` convention as
    `board.BoardPlayer.key`) — a replay's decision-time roster keys join
    onto this with a plain dict lookup.

    This is the grading key, built entirely from ground truth (cache-first,
    same as every other historical source). A key absent from the returned
    dict means the player did not appear in that week's box score at all —
    callers should treat that as "scored 0" only after confirming that's the
    right read (a bye, an inactive, or a name-match miss all look the same
    here and are not distinguished).
    """
    out: dict[str, float] = {}

    player_rows = _rows_for_week(
        fetch_rows("stats_player_week", season=season, cache_dir=cache_dir, opener=opener),
        season, week,
    )
    for row in player_rows:
        position = (row.get("position") or "").strip().upper()
        if position not in _SCORABLE_PLAYER_POSITIONS:
            continue
        name = row.get("player_display_name") or ""
        if not name:
            continue
        pts, _flags = score_player_row(row, scoring)
        out[actuals_key(name, position)] = pts

    team_rows = _rows_for_week(
        fetch_rows("stats_team_week", season=season, cache_dir=cache_dir, opener=opener),
        season, week,
    )
    week_games = _rows_for_week(
        fetch_rows("games", cache_dir=cache_dir, opener=opener), season, week,
    )
    for row in team_rows:
        team = canonical_team(row.get("team"))
        if not team:
            continue
        game_row = _game_row_for_team(team, week_games)
        points_allowed = points_allowed_for(team, game_row) if game_row else None
        pts, _flags = score_defense_row(row, points_allowed, scoring)
        out[actuals_key(team, "DEF")] = pts

    return out
