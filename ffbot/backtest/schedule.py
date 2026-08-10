"""Head-to-head schedule + win-loss records for B4's season simulator (see
docs/BACKTEST.md).

Fantasy leagues are won on matchups, not points — a points edge that never
flips a matchup is worth nothing to a real manager. `ffbot.backtest.metrics`
reports points-based deltas because they're available today and far
higher-powered; this module is what lets a later pass validate a chosen
weight setting against the metric that actually decides a season, win rate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence


def round_robin_schedule(num_teams: int, weeks: int) -> list[list[tuple[int, int]]]:
    """`weeks` rounds of 1-indexed `(team_a, team_b)` pairings, via the
    "circle method" — one team fixed, the rest rotate one position each
    round, giving every team exactly one game per round and every pair of
    teams exactly one meeting per `num_teams - 1`-round cycle (or
    `num_teams`-round cycle with one bye each round, if `num_teams` is odd).

    `weeks` beyond one full cycle repeats it from the start — a real league
    with more `regular_season_weeks` (`league.yml`) than `num_teams - 1`
    just plays some pairings more than once (division rivals, e.g.), and
    restarting the same cycle is the least-arbitrary way to extend a
    round robin past its natural length without inventing a new algorithm.
    """
    if num_teams < 2:
        raise ValueError("num_teams must be >= 2")
    if weeks < 1:
        raise ValueError("weeks must be >= 1")

    teams = list(range(1, num_teams + 1))
    bye = len(teams) % 2 == 1
    if bye:
        teams.append(0)  # 0 = "bye" marker, filtered out of every round's pairs
    n = len(teams)
    cycle_len = n - 1

    fixed = teams[0]
    rotating = teams[1:]
    base_rounds: list[list[tuple[int, int]]] = []
    for _ in range(cycle_len):
        round_teams = [fixed] + rotating
        pairs = []
        for i in range(n // 2):
            a, b = round_teams[i], round_teams[n - 1 - i]
            if a != 0 and b != 0:
                pairs.append((a, b))
        base_rounds.append(pairs)
        rotating = [rotating[-1]] + rotating[:-1]  # rotate one position

    return [base_rounds[w % cycle_len] for w in range(weeks)]


@dataclass(frozen=True)
class ManagerRecord:
    wins: int = 0
    losses: int = 0
    ties: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_rate(self) -> float:
        """Ties count as half a win — the conventional fantasy-standings
        convention. `0.0` for a manager with no games played, never a
        division-by-zero crash."""
        if self.games == 0:
            return 0.0
        return (self.wins + 0.5 * self.ties) / self.games


def score_schedule(
    schedule: Sequence[Sequence[tuple[int, int]]],
    points_by_team_week: dict[tuple[int, int], float],
) -> dict[int, ManagerRecord]:
    """W-L-T per team over `schedule`, from `points_by_team_week` (`(team,
    week) -> realized points that week` — week is 1-indexed, matching
    `schedule`'s own enumeration). A missing `(team, week)` entry scores
    `0.0`, same "absent means unscored, not a crash" contract every other
    grading function in `ffbot.backtest` uses.

    The sum of every returned record's `games` always equals `2 *` the
    total number of pairings across `schedule` (each game counts once for
    each side) — a caller checking "no double-counted or dropped matchups"
    should verify exactly that.
    """
    wins: dict[int, int] = defaultdict(int)
    losses: dict[int, int] = defaultdict(int)
    ties: dict[int, int] = defaultdict(int)
    teams: set[int] = set()

    for week_idx, pairs in enumerate(schedule, start=1):
        for a, b in pairs:
            teams.add(a)
            teams.add(b)
            pa = points_by_team_week.get((a, week_idx), 0.0)
            pb = points_by_team_week.get((b, week_idx), 0.0)
            if pa > pb:
                wins[a] += 1
                losses[b] += 1
            elif pb > pa:
                wins[b] += 1
                losses[a] += 1
            else:
                ties[a] += 1
                ties[b] += 1

    return {t: ManagerRecord(wins=wins[t], losses=losses[t], ties=ties[t]) for t in teams}
