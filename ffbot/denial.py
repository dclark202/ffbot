"""Tactical denial — is a waiver claim worth taking specifically because a
contending rival needs it, not because you would ever start him yourself.

`week.hold_margin` (core vs. streaming) is a function of your own roster
alone and structurally cannot see this — denial value needs the other 11
rosters. Two optional inputs make it a real, computed number instead of a
guess: `league_rosters.yml` (`scripts/import_league_rosters.py`) gives
every rival's actual roster, and `league.yml`'s `teams:` section gives
curated standings. Every function here degrades to an exact no-op when
either is missing, or when `cfg.season.denial_weight` is 0.0 (the
default) — same contract as every other optional research input in this
codebase.

Denial is an inference about other humans' likely behavior, not a
verifiable fact about your own roster, so per the "what's verifiable moves
the number, what's speculative stays a note" contract this stays a
separately flagged recommendation (`denial_candidates`) rather than a
silent addition to the ordinary waiver ranking — the one exception is
*claim urgency* (a rival is thin and threatens to grab this first), which
is folded into `week.waiver_candidates`' own `net` because "claim it before
someone else does" is an ordinary, non-speculative reason to move now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .board import Board, BoardPlayer, to_player
from .config import Config, TeamStanding
from .league_rosters import LeagueRosters
from .lineup import optimize
from .names import normalize_name


def threat(standing: TeamStanding | None, playoff_teams: int, num_teams: int) -> float:
    """0..1 how much a rival's playoff chase should weigh in denial math.

    An eliminated team (locked out of waivers under `lock_eliminated_teams`)
    is exactly 0 — they cannot claim anyone regardless of need. Otherwise,
    closeness to the cut line by seed: the team right at the bubble (seed ==
    `playoff_teams`) is 1.0, fading linearly for teams further from it in
    either direction. An unknown seed (no `league.yml` standings entry, or
    `playoff_teams` not configured) assumes moderate threat — 0.5 — rather
    than 0 (which would silently suppress denial for every rival you
    haven't gotten around to entering) or 1.0 (which would treat every
    unresearched rival as maximally threatening).
    """
    if standing is not None and standing.eliminated:
        return 0.0
    if standing is None or standing.seed is None or playoff_teams <= 0 or num_teams <= playoff_teams:
        return 0.5
    distance = abs(standing.seed - playoff_teams)
    span = max(1, num_teams - playoff_teams)
    return max(0.0, 1.0 - distance / span)


def rival_roster_keys(names: Sequence[str], board: Board) -> list[str]:
    """Board keys for a rival's rostered players.

    `names` are display names as written in `league_rosters.yml`, which
    only ever contains names `scripts/import_league_rosters.py` already
    matched against this exact board — so a normalized-name lookup is
    sufficient here (no fuzzy fallback needed, unlike matching a fresh,
    unreviewed name).
    """
    by_name: dict[str, str] = {}
    for bp in board.players:
        by_name.setdefault(normalize_name(bp.name), bp.key)
    return [by_name[normalize_name(n)] for n in names if normalize_name(n) in by_name]


def thin_at(roster_keys: Sequence[str], board: Board, roster_positions: dict[str, int], cfg: Config) -> list[str]:
    """Starting slots a rival's roster leaves unfilled — derived by running
    the same optimizer against their roster, never researched or guessed.
    """
    players = [to_player(board.by_key[k], uid) for uid, k in enumerate(roster_keys, start=1) if k in board.by_key]
    plan = optimize(players, roster_positions, None, cfg)
    return sorted(set(plan.unfilled_slots))


def _standings_by_name(teams: Sequence[TeamStanding]) -> dict[str, TeamStanding]:
    return {t.name: t for t in teams}


def denial_value(
    candidate: BoardPlayer,
    league_rosters: LeagueRosters,
    board: Board,
    roster_positions: dict[str, int],
    cfg: Config,
) -> float:
    """Season points `candidate` would add to threatened rivals' optimal
    lineups, summed across every rival and weighted by `threat`, scaled by
    `cfg.season.denial_weight`.

    Exactly 0.0 whenever `denial_weight` is 0.0 (the default) or
    `league_rosters` has no teams — this cannot see denial value on its
    own, since it is a function of the other 11 rosters, not your own.
    `denial_weight` is a direct points multiplier (not a decision-scale
    fraction like the draft/weekly edge terms): a rival's roster gain is
    already in the same season-points unit `week.hold_margin` and
    `week.drop_cost` use, so no extra scale conversion is needed here.
    """
    weight = cfg.season.denial_weight
    if weight == 0.0 or not league_rosters.teams or cfg.league is None:
        return 0.0

    from .draft import need as draft_need  # local import: avoids a cycle

    standings = _standings_by_name(cfg.league.teams)
    playoff_teams = cfg.league.playoff_teams
    num_teams = cfg.draft.num_teams

    total = 0.0
    for team_name, names in league_rosters.teams.items():
        t = threat(standings.get(team_name), playoff_teams, num_teams)
        if t <= 0.0:
            continue
        roster_keys = rival_roster_keys(names, board)
        gain = draft_need(candidate, roster_keys, board, cfg)
        if gain > 0.0:
            total += t * gain

    return weight * total


@dataclass(frozen=True)
class DenialCandidate:
    add_name: str
    position: str
    denial_value: float
    reason: str


def denial_candidates(
    roster_keys: Sequence[str],
    board: Board,
    roster_positions: dict[str, int],
    cfg: Config,
    league_rosters: LeagueRosters,
    rostered_names: set[str],
    streaming_floor: float,
    limit: int | None = None,
) -> list[DenialCandidate]:
    """Free agents worth claiming PURELY to deny a rival — never because you
    would start them yourself. Fires only when `denial_value` exceeds
    `streaming_floor` (the same bench spot's value as a streamer, from
    `week.best_streaming_baseline`), so a denial claim is never recommended
    for less than its real opportunity cost.

    Exactly empty whenever `cfg.season.denial_weight` is 0.0, no
    `league.yml` is configured, or `league_rosters` has no teams — see
    `denial_value`. `rostered_names` should already include both your own
    roster and every rival's (from `league_rosters.rostered_names()`), so a
    player already claimed anywhere is never suggested.

    Scans at most `cfg.season.waiver_pool_size` players (the board is
    already VOR-sorted, so this is the same best-first truncation
    `week.waiver_candidates` applies) — a denial candidate deep in the
    replacement-level tail isn't worth the O(pool x rivals) optimizer calls
    this requires.
    """
    if cfg.season.denial_weight == 0.0 or cfg.league is None or not league_rosters.teams:
        return []

    limit = limit if limit is not None else cfg.season.recommend_count
    standings = _standings_by_name(cfg.league.teams)
    playoff_teams = cfg.league.playoff_teams
    num_teams = cfg.draft.num_teams
    pool_size = max(1, cfg.season.waiver_pool_size)

    from .draft import need as draft_need  # local import: avoids a cycle

    rival_keys_by_team = {
        team: rival_roster_keys(names, board) for team, names in league_rosters.teams.items()
    }

    out: list[DenialCandidate] = []
    for bp in board.players[:pool_size]:
        if normalize_name(bp.name) in rostered_names:
            continue

        best_team, best_gain, total = "", 0.0, 0.0
        for team_name, keys in rival_keys_by_team.items():
            t = threat(standings.get(team_name), playoff_teams, num_teams)
            if t <= 0.0:
                continue
            gain = draft_need(bp, keys, board, cfg)
            if gain <= 0.0:
                continue
            total += t * gain
            if gain > best_gain:
                best_team, best_gain = team_name, gain

        dv = cfg.season.denial_weight * total
        if dv > streaming_floor:
            out.append(DenialCandidate(
                add_name=bp.name, position=bp.position, denial_value=dv,
                reason=f"denies {best_team} (+{best_gain:.1f} to their lineup)" if best_team else "denies a rival",
            ))

    out.sort(key=lambda c: -c.denial_value)
    return out[:limit]
