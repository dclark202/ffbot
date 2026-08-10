"""B3's runner: iterates `(season, week, sampled roster)`, builds all five
baselines (`ffbot.backtest.baselines`), grades each against the real result
(`ffbot.history.actuals.week_actuals` — the one permitted ground-truth read,
see this package's `__init__` docstring), and returns `metrics.Decision`
records ready for `scripts/backtest_lineup.py` to summarize. Pure and
deterministic given a seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..config import Config, LeagueScoring
from ..history.actuals import week_actuals
from ..history.fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener
from ..history.index import as_of
from ..history.openmeteo import GameProvider
from ..history.projections import ecr_projections, naive_projections, player_pool
from ..history.signals import SignalProvider
from .baselines import Lineup, build_baselines, key_by_player_id
from .metrics import Decision, score_lineup
from .rosters import sample_rosters

BASELINE_NAMES = ("oracle", "control", "agent", "consensus", "random_legal")


@dataclass(frozen=True)
class ReplayResult:
    decisions: list[Decision]
    lineups: list[dict[str, Lineup]]  # parallel to `decisions` — `lineups[i]` built `decisions[i]`


def _projections_for(
    source: str,
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str,
    opener: UrlOpener,
) -> dict[str, float]:
    if source == "naive":
        return naive_projections(season, week, cfg, cache_dir=cache_dir, opener=opener)
    if source == "ecr":
        return ecr_projections(season, week, cfg, cache_dir=cache_dir, opener=opener)
    raise ValueError(f"unknown projection source {source!r} (expected 'naive' or 'ecr')")


def replay_week(
    season: int,
    week: int,
    cfg: Config,
    source: str,
    rosters_per_week: int,
    seed: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    signal_provider: Optional[SignalProvider] = None,
    game_provider: Optional[GameProvider] = None,
) -> ReplayResult:
    """Replay one `(season, week)`: sample `rosters_per_week` rosters, build
    all five baselines for each, and grade every one against real results.

    `cache_dir`/`opener` are threaded through to every historical-data call
    this function makes (`player_pool`, the chosen projection engine,
    `week_actuals`, `as_of`) — the same convention every function in
    `ffbot.history` already follows, and what makes this function testable
    with zero network access (a bound default like `opener: UrlOpener =
    _default_opener` captures a fixed function reference at import time;
    threading it explicitly is what lets a test substitute a fake one).

    `signal_provider`, when given, is called once for this `(season, week)`
    and merged onto the snapshot via `WeekSnapshot.with_signals()` BEFORE any
    roster is built — see `ffbot.history.signals`'s module docstring for why
    this happens outside `as_of()` rather than as an argument to it.
    `None` (the default) reproduces B3's behavior exactly: `volatility_weight`/
    `upside_lean_weight` stay structurally inert, same as before this
    parameter existed.

    `game_provider` is the GAME-level analog, merged via
    `WeekSnapshot.with_game_weather()` — see `ffbot.history.openmeteo`.
    `None` (the default) leaves `games.csv`'s own wind/roof-derived weather
    exactly as `as_of()` already produces it.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    pool = player_pool(season, week, cache_dir=cache_dir, opener=opener)
    projections = _projections_for(source, season, week, cfg, cache_dir, opener)
    actuals_by_key = week_actuals(season, week, scoring, cache_dir=cache_dir, opener=opener)
    snapshot = as_of(season, week, cache_dir=cache_dir, opener=opener)
    if signal_provider is not None:
        signals = signal_provider(season, week, cfg, cache_dir=cache_dir, opener=opener)
        snapshot = snapshot.with_signals(signals)
    if game_provider is not None:
        weather = game_provider(season, week, cfg, cache_dir=cache_dir, opener=opener)
        snapshot = snapshot.with_game_weather(weather)

    rosters = sample_rosters(pool, projections, cfg.roster_positions, n=rosters_per_week, seed=seed)

    decisions: list[Decision] = []
    lineups: list[dict[str, Lineup]] = []
    for i, roster_rows in enumerate(rosters):
        baselines = build_baselines(
            roster_rows, projections, actuals_by_key, snapshot, cfg, week, seed=seed + i
        )
        key_by_id = key_by_player_id(roster_rows)
        points = {name: score_lineup(lineup, actuals_by_key, key_by_id) for name, lineup in baselines.items()}
        decisions.append(Decision(season=season, week=week, roster_index=i, points=points))
        lineups.append(baselines)

    return ReplayResult(decisions=decisions, lineups=lineups)


def replay(
    seasons: Sequence[int],
    weeks: Sequence[int],
    cfg: Config,
    source: str,
    rosters_per_week: int,
    seed: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    signal_provider: Optional[SignalProvider] = None,
    game_provider: Optional[GameProvider] = None,
) -> ReplayResult:
    """Replay every `(season, week)` in `seasons x weeks`, concatenating the
    results — the unit `scripts/backtest_lineup.py` reports over. Each
    `(season, week)` gets its own seed derived from the base `seed` so two
    different weeks never draw the same roster sample by accident.
    """
    all_decisions: list[Decision] = []
    all_lineups: list[dict[str, Lineup]] = []
    for season in seasons:
        for week in weeks:
            week_seed = seed + season * 1000 + week
            result = replay_week(
                season, week, cfg, source, rosters_per_week, seed=week_seed,
                cache_dir=cache_dir, opener=opener, signal_provider=signal_provider,
                game_provider=game_provider,
            )
            all_decisions.extend(result.decisions)
            all_lineups.extend(result.lineups)
    return ReplayResult(decisions=all_decisions, lineups=all_lineups)
