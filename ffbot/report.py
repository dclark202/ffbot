"""The seam between "gather everything the weekly brief needs" and how it's
presented — `scripts/week_report.py`'s text renderers and the GUI's JSON
serializers (`ffbot/webapi.py`) both build on `load_everything` here.

Split out of `scripts/week_report.py` (which originally raised bare
`SystemExit` and returned an unlabeled 7-tuple) so the GUI can call the exact
same loading logic and get a catchable exception and named fields instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import roster_source as rs
from . import week
from .board import Board, load_board_from_config
from .config import Config
from .league_rosters import LeagueRosters, load_league_rosters
from .models import Player


class ReportError(ValueError):
    """Everything needed to build the weekly report could not be loaded."""


@dataclass
class LoadedReport:
    cfg: Config
    weekly: week.WeeklyIntel
    board: Board | None
    players: list[Player]
    unmatched: list  # rs.UnmatchedRosterName
    stadiums: dict[str, week.StadiumInfo]
    league_rosters: LeagueRosters


def default_weekly_path(week_num: int) -> Path:
    return Path("weekly") / f"week-{week_num:02d}.yml"


def load_everything(
    config_path: str = "config.yml",
    roster_path: str = "roster.yml",
    week_num: int = 1,
    proj_csv_paths: Sequence[str] | None = None,
    weekly_path: str | None = None,
    weeks_in_season: int = 17,
    league_rosters_path: str = "league_rosters.yml",
) -> LoadedReport:
    """Load config, weekly intel, the draft board (if configured), and the
    roster, matched and ready for `week.build_week_brief`/`waiver_candidates`.

    Raises `ReportError` (never `SystemExit`) when the roster can't be
    loaded at all, or when there's neither a fresh weekly projection nor a
    board to fall back on — both cases where there is genuinely nothing to
    report on.
    """
    cfg = Config.load(config_path)

    weekly_p = Path(weekly_path) if weekly_path else default_weekly_path(week_num)
    weekly = week.load_weekly_intel(weekly_p)

    board = None
    try:
        board = load_board_from_config(cfg)
    except ValueError:
        pass  # no board configured -- season-board fallback and waivers just won't be available

    fallback_rows = []
    if board is not None:
        weeks_remaining = max(1, weeks_in_season - week_num + 1)
        fallback_rows = rs.season_board_rows(board, weeks_remaining)

    csv_paths = list(proj_csv_paths or [])
    try:
        players, unmatched = rs.load_roster(
            csv_paths, roster_path, fallback_rows=fallback_rows, league=cfg.league
        )
    except rs.RosterError as exc:
        raise ReportError(str(exc)) from exc

    if not csv_paths and not fallback_rows:
        raise ReportError(
            "No weekly projections and no draft board to fall back on — pass "
            "a weekly projection CSV, or set draft.board_csv in config.yml."
        )

    league_rosters = load_league_rosters(league_rosters_path)
    stadiums = week.load_stadiums()

    return LoadedReport(
        cfg=cfg,
        weekly=weekly,
        board=board,
        players=players,
        unmatched=unmatched,
        stadiums=stadiums,
        league_rosters=league_rosters,
    )
