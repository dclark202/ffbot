"""The seam between "gather everything the weekly brief needs" and how it's
presented — `scripts/week_report.py`'s text renderers and the GUI's JSON
serializers (`ffbot/webapi.py`) both build on `load_everything` here.

Split out of `scripts/week_report.py` (which originally raised bare
`SystemExit` and returned an unlabeled 7-tuple) so the GUI can call the exact
same loading logic and get a catchable exception and named fields instead.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import projections
from . import roster_source as rs
from . import week
from .board import Board, _board_key, apply_league_scoring, load_board_from_config
from .config import Config
from .league_rosters import LeagueRosters, load_league_rosters
from .models import Player
from .projections.cache import ProjectionFetchError


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

    # Live-projection wiring (see ffbot/projections/) -- "board" (the
    # default) and "csv" (the pre-existing --proj hatch) leave these at
    # their inert defaults, bit-identical to before this feature existed.
    projection_source: str = "board"
    projection_alerts: list[str] = field(default_factory=list)
    # `{board key: real this-week points}` for the WHOLE candidate pool, not
    # just the roster -- one live-provider fetch already covers every
    # player, so this is free to build alongside the roster's own numbers.
    # Feed straight into `week.waiver_candidates(weekly_points=...)`.
    weekly_points: dict[str, float] = field(default_factory=dict)


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
    season: int | None = None,
    source_override: str | None = None,
) -> LoadedReport:
    """Load config, weekly intel, the draft board (if configured), and the
    roster, matched and ready for `week.build_week_brief`/`waiver_candidates`.

    Raises `ReportError` (never `SystemExit`) when the roster can't be
    loaded at all, or when there's neither a fresh weekly projection nor a
    board to fall back on — both cases where there is genuinely nothing to
    report on.

    `source_override` (`"board"`/`"sleeper"`/`"csv"`) overrides
    `cfg.projection_source.source` for this one call — the CLI's `--source`
    flag; the GUI leaves this `None` and always follows config, exactly like
    every other GUI-relevant setting. `season` overrides
    `ffbot.projections.current_nfl_season()`'s calendar-based guess — needed
    only when `source` resolves to `"sleeper"`, which is the only live
    provider that needs a season year at all.

    A live-provider fetch failure NEVER raises out of this function and
    never silently degrades either: it falls back to the season board (the
    `"board"`-source behavior) for this run, and the reason lands in the
    returned `LoadedReport.projection_alerts`, which every caller must
    surface to the user (see `scripts/week_report.py`/`ffbot/webapi.py`).
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
        # A FIXED season length, not a shrinking "weeks remaining" -- see
        # `season_board_rows`'s own docstring for why, and why this must
        # match whatever `waiver_candidates` callers pass for the same
        # fallback-pricing convention (webapi.py, week_report.py).
        fallback_rows = rs.season_board_rows(board, weeks_in_season)

    resolved_source = source_override or cfg.projection_source.source
    projection_alerts: list[str] = []
    weekly_points: dict[str, float] = {}
    provider_rows: list[dict] = []

    if resolved_source == "sleeper":
        resolved_season = season if season is not None else projections.current_nfl_season()
        # `source_override` (the CLI's --source) may disagree with
        # `cfg.projection_source.source` -- resolve_provider must see the
        # OVERRIDDEN source, not the raw config value, or an override to
        # "sleeper" over a config default of "board" silently resolves to
        # no provider at all.
        effective_source_cfg = dataclasses.replace(cfg.projection_source, source=resolved_source)
        provider = projections.resolve_provider(effective_source_cfg)
        assert provider is not None  # "sleeper" always resolves to a real provider
        try:
            provider_rows = projections.weekly_rows(resolved_season, week_num, provider)
        except ProjectionFetchError as exc:
            projection_alerts.append(
                f"Live projections (sleeper) unavailable this run ({exc}) — "
                "falling back to the season board."
            )
            provider_rows = []
        else:
            # A second, independent league-scoring pass (load_roster below
            # does its own for the roster's rows) -- deliberately not shared,
            # since this one covers the WHOLE candidate pool, not just the
            # roster, and apply_league_scoring is cheap per row.
            scored_rows = [dict(r) for r in provider_rows]
            apply_league_scoring(scored_rows, cfg.league)
            weekly_points = {_board_key(r["name"], r["position"]): r["points"] for r in scored_rows}

    csv_paths = list(proj_csv_paths or [])
    try:
        players, unmatched = rs.load_roster(
            csv_paths, roster_path, fallback_rows=fallback_rows, league=cfg.league,
            provider_rows=provider_rows,
        )
    except rs.RosterError as exc:
        raise ReportError(str(exc)) from exc

    if not csv_paths and not provider_rows and not fallback_rows:
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
        projection_source=resolved_source,
        projection_alerts=projection_alerts,
        weekly_points=weekly_points,
    )
