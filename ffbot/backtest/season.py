"""Full-season replay: draft, then week-by-week lineup + waiver management
for one designated manager under either the AGENT or CONTROL policy — the
season-long extension of B3's per-week paired baselines (see
docs/BACKTEST.md, milestone B4).

Only the designated manager (`agent_slot`)'s roster mutates across the
season, via `week.waiver_candidates` — the exact production function,
unchanged. The other `num_teams - 1` managers are static reference
opponents: frozen at their drafted roster for the whole season, managed by
the same "start the best healthy player, never reconsider" greedy policy
B3's `consensus` baseline uses. That is enough to give `week.waiver_candidates`
a real `LeagueRosters` exclusion set and give `schedule.py` real opponents to
play, without needing 12 independently-tuned agents to answer the one
question this module exists for: does the agent's SEASON-LONG management
(spice-adjusted lineups + spice-influenced waiver adds) come out ahead of
the frozen-projection control, over a real schedule, in real win rate.

The agent-vs-control pairing works exactly like B3's: both policies start
from the IDENTICAL drafted roster (one shared `DraftResult`) and see the
IDENTICAL weekly data; they diverge only in whether `week.adjusted_players`
runs before the lineup is set and before the roster it hands to
`waiver_candidates` — matching `_week_score`'s own documented contract that
it scores whatever `projected_points` the caller's `Player` objects already
carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .. import week as weekmod
from ..board import Board
from ..config import Config, LeagueScoring
from ..history.actuals import week_actuals
from ..history.fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener
from ..history.index import as_of
from ..history.openmeteo import GameProvider
from ..history.projections import ecr_projections, naive_projections, players_asof
from ..history.signals import SignalProvider
from ..lineup import optimize
from ..league_rosters import LeagueRosters
from ..names import normalize_name
from ..history.names import actuals_key
from .baselines import _greedy_consensus_lineup, key_by_player_id
from .draft_sim import DraftResult, agent_roster

_POLICIES = ("agent", "control")


def _projections_for(source: str, season: int, week: int, cfg: Config, cache_dir, opener) -> dict[str, float]:
    if source == "naive":
        return naive_projections(season, week, cfg, cache_dir=cache_dir, opener=opener)
    if source == "ecr":
        return ecr_projections(season, week, cfg, cache_dir=cache_dir, opener=opener)
    raise ValueError(f"unknown projection source {source!r} (expected 'naive' or 'ecr')")


def _roster_rows_from_keys(keys: Sequence[str], board: Board) -> list[dict]:
    """Board keys (as drafted) -> the `{key, name, position, team}` row
    shape `players_asof` consumes, resolved against `board.by_key` (the
    SAME season-long board the draft itself came from)."""
    rows = []
    for key in keys:
        bp = board.by_key.get(key)
        if bp is None:
            continue
        rows.append({"key": key, "name": bp.name, "position": bp.position, "team": bp.team})
    return rows


def _league_rosters_from_draft(draft: DraftResult, board: Board, agent_slot: int) -> LeagueRosters:
    """The other `num_teams - 1` drafted rosters, as a `LeagueRosters` —
    exact here, unlike production where it's hand-imported from a paste.
    This is what keeps `week.waiver_candidates` from recommending a player
    one of the static opponents already drafted."""
    teams: dict[str, list[str]] = {}
    for i, roster_keys in enumerate(draft.rosters, start=1):
        if i == agent_slot:
            continue
        names = [board.by_key[k].name for k in roster_keys if k in board.by_key]
        teams[f"team_{i}"] = names
    return LeagueRosters(teams=teams)


@dataclass(frozen=True)
class SeasonResult:
    policy: str  # "agent" | "control"
    points_by_week: dict[int, float] = field(default_factory=dict)
    final_roster: list[dict] = field(default_factory=list)
    adds: list[tuple[int, str]] = field(default_factory=list)  # (week, player name) claimed


def _opponent_slot(
    schedule: Optional[Sequence[Sequence[tuple[int, int]]]], agent_slot: int, week_position: int
) -> Optional[int]:
    """`agent_slot`'s opponent at `schedule[week_position - 1]` (1-indexed,
    POSITIONALLY aligned to `weeks` — `schedule[i]` is `weeks[i]`'s pairing,
    same convention `scripts/backtest_season.py`'s own
    `round_robin_schedule(num_teams, len(weeks))` already assumes), or
    `None` when there's no schedule, the index is out of range, or the
    agent has a bye that round."""
    if schedule is None or not (1 <= week_position <= len(schedule)):
        return None
    for a, b in schedule[week_position - 1]:
        if a == agent_slot:
            return b
        if b == agent_slot:
            return a
    return None


def simulate_season(
    season: int,
    cfg: Config,
    board: Board,
    draft: DraftResult,
    agent_slot: int,
    policy: str,
    source: str,
    weeks: Sequence[int],
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
    signal_provider: Optional[SignalProvider] = None,
    schedule: Optional[Sequence[Sequence[tuple[int, int]]]] = None,
    game_provider: Optional[GameProvider] = None,
) -> SeasonResult:
    """Replay one manager's season under `policy` (`"agent"` or
    `"control"`), starting from `draft`'s already-drafted roster for
    `agent_slot`.

    Each week: build this week's point-in-time roster (`players_asof`),
    apply `week.adjusted_players` only under `"agent"`, set the lineup via
    `lineup.optimize` (unchanged production code), grade it against real
    results, then run `week.waiver_candidates` (also unchanged) against the
    SAME roster the lineup decision used — so `"control"`'s waiver adds are
    driven by raw projections and `"agent"`'s by spice-adjusted ones, the
    identical variable B3 isolates for the lineup-only comparison. A
    positive top candidate is applied immediately; a rolling-waiver
    priority is tracked and reset to the bottom on every successful claim,
    the standard mechanic `league.yml`'s `waiver_type: rolling` implies.

    `schedule` (optional; positionally aligned to `weeks` — see
    `_opponent_slot`), when given, feeds `week.matchup_lean` for
    `cfg.season.matchup_variance_weight` under the `"agent"` policy only
    (`"control"` never calls `adjusted_players` at all, so a lean would be
    unused there). Omitted, the lean stays 0.0 every week — an exact no-op,
    same as before this parameter existed.
    """
    if policy not in _POLICIES:
        raise ValueError(f"policy must be one of {_POLICIES}, got {policy!r}")

    scoring = cfg.league or LeagueScoring.fantasypros_default()
    num_teams = max(1, cfg.draft.num_teams)
    weeks_in_season = cfg.league.regular_season_weeks if cfg.league is not None else 15

    roster_rows = _roster_rows_from_keys(agent_roster(draft, agent_slot), board)
    league_rosters = _league_rosters_from_draft(draft, board, agent_slot)

    priority = num_teams  # start at the bottom; see the docstring above
    points_by_week: dict[int, float] = {}
    adds: list[tuple[int, str]] = []

    for week_position, wk in enumerate(weeks, start=1):
        projections = _projections_for(source, season, wk, cfg, cache_dir, opener)
        snapshot = as_of(season, wk, cache_dir=cache_dir, opener=opener)
        if signal_provider is not None:
            snapshot = snapshot.with_signals(signal_provider(season, wk, cfg, cache_dir=cache_dir, opener=opener))
        if game_provider is not None:
            snapshot = snapshot.with_game_weather(game_provider(season, wk, cfg, cache_dir=cache_dir, opener=opener))

        raw_players = players_asof(roster_rows, projections, snapshot)
        if policy == "agent":
            lean = 0.0
            opp_slot = _opponent_slot(schedule, agent_slot, week_position)
            if opp_slot is not None:
                opp_names = league_rosters.teams.get(f"team_{opp_slot}")
                if opp_names:
                    from .. import denial  # local import: avoids a cycle, same convention as denial.py's own

                    my_keys = [r["key"] for r in roster_rows]
                    opp_keys = denial.rival_roster_keys(opp_names, board)
                    my_total = weekmod.team_projected_total(my_keys, board, cfg.roster_positions, cfg)
                    opp_total = weekmod.team_projected_total(opp_keys, board, cfg.roster_positions, cfg)
                    lean = weekmod.matchup_lean(my_total, opp_total)
            lineup_players = weekmod.adjusted_players(
                raw_players, snapshot.weekly_intel(), cfg.season, snapshot.stadiums, lean
            )
        else:
            lineup_players = raw_players

        plan = optimize(lineup_players, cfg.roster_positions, wk, cfg)

        actuals = week_actuals(season, wk, scoring, cache_dir=cache_dir, opener=opener)
        key_by_id = key_by_player_id(roster_rows)
        points_by_week[wk] = sum(
            actuals.get(key_by_id.get(p.player_id), 0.0) for _slot, p in plan.assignments
        )

        candidates, _unmatched = weekmod.waiver_candidates(
            lineup_players, board, cfg.roster_positions, cfg,
            remaining_faab=0, my_priority=priority,
            weeks_remaining=max(1, weeks_in_season - wk + 1),
            league_rosters=league_rosters, limit=1, week=wk,
        )
        claimed = False
        if candidates and candidates[0].net > 0:
            best = candidates[0]
            add_key = actuals_key(best.add_name, best.position)
            add_bp = board.by_key.get(add_key)
            if best.drop_name is not None:
                drop_norm = normalize_name(best.drop_name)
                roster_rows = [r for r in roster_rows if normalize_name(r["name"]) != drop_norm]
            roster_rows.append({
                "key": add_key, "name": best.add_name, "position": best.position,
                "team": add_bp.team if add_bp is not None else "",
            })
            adds.append((wk, best.add_name))
            priority = num_teams  # spent the claim -- back to the bottom
            claimed = True
        if not claimed:
            priority = max(1, priority - 1)

    return SeasonResult(policy=policy, points_by_week=points_by_week, final_roster=roster_rows, adds=adds)


def static_opponent_points(
    season: int,
    cfg: Config,
    board: Board,
    draft: DraftResult,
    team_slot: int,
    source: str,
    weeks: Sequence[int],
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[int, float]:
    """A static opponent's weekly points — frozen at their drafted roster
    for the whole season, lineup set by `baselines._greedy_consensus_lineup`
    (the same "start the best healthy player" policy B3's `consensus`
    baseline uses) every week, no waivers at all. Cheap on purpose: 11 of
    these run per season alongside the one real agent-vs-control pair.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    roster_rows = _roster_rows_from_keys(draft.rosters[team_slot - 1], board)

    points_by_week: dict[int, float] = {}
    for wk in weeks:
        projections = _projections_for(source, season, wk, cfg, cache_dir, opener)
        snapshot = as_of(season, wk, cache_dir=cache_dir, opener=opener)
        players = players_asof(roster_rows, projections, snapshot)
        key_by_id = key_by_player_id(roster_rows)

        assignments, _bench = _greedy_consensus_lineup(players, cfg.roster_positions)
        actuals = week_actuals(season, wk, scoring, cache_dir=cache_dir, opener=opener)
        points_by_week[wk] = sum(actuals.get(key_by_id.get(p.player_id), 0.0) for _slot, p in assignments)

    return points_by_week
