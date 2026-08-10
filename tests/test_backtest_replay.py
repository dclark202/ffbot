from __future__ import annotations

import csv
import io

import pytest

from ffbot.backtest.replay import BASELINE_NAMES, _projections_for, replay, replay_week
from ffbot.config import Config

# --- A tiny, fully synthetic 2-week season -----------------------------
#
# Just enough depth at each position (with margin above the default
# roster_positions' sampled target counts) that `sample_roster` never comes
# up short: QB2/RB4/WR5/TE2/K1/DEF1 is the target for a bare Config()'s
# default roster shape (capacity 15) -- see test_backtest_rosters.py's own
# `_target_counts` tests for that derivation.

_POSITION_DEPTH = {"QB": 3, "RB": 6, "WR": 7, "TE": 3, "K": 2}

_PLAYER_FIELDS = [
    "player_display_name", "position", "team", "season", "week",
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds",
    "receptions", "receiving_yards", "receiving_tds",
    "fumbles_lost_total", "fg_made", "fg_att", "pat_made",
]
_ROSTER_FIELDS = ["season", "week", "team", "position", "full_name"]
_TEAM_FIELDS = [
    "team", "season", "week", "def_sacks", "def_interceptions", "def_fumbles",
    "def_fumbles_forced", "def_tds", "special_teams_tds", "def_safeties",
]
_GAME_FIELDS = ["season", "week", "home_team", "away_team", "home_score", "away_score", "gameday", "gametime"]


def _csv(fieldnames: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _player_stat_row(name: str, pos: str, week: int, i: int) -> dict:
    base = {"player_display_name": name, "position": pos, "team": "MIA", "season": 2023, "week": week}
    if pos == "QB":
        base.update(completions=20 + i, attempts=30 + i, passing_yards=250 + i * 5, passing_tds=2, passing_interceptions=0)
    elif pos == "RB":
        base.update(carries=15 + i, rushing_yards=60 + i * 4, rushing_tds=1 if i % 2 == 0 else 0)
    elif pos == "WR":
        base.update(receptions=4 + i, receiving_yards=50 + i * 3, receiving_tds=1 if i % 3 == 0 else 0)
    elif pos == "TE":
        base.update(receptions=3 + i, receiving_yards=30 + i * 2, receiving_tds=0)
    elif pos == "K":
        base.update(fg_made=2, fg_att=3, pat_made=2)
    return base


def _season_bytes() -> dict[str, bytes]:
    player_rows: list[dict] = []
    roster_rows: list[dict] = []
    for pos, n in _POSITION_DEPTH.items():
        for i in range(n):
            name = f"{pos} Player {i}"
            for week in (1, 2):
                player_rows.append(_player_stat_row(name, pos, week, i))
                roster_rows.append({"season": 2023, "week": week, "team": "MIA", "position": pos, "full_name": name})

    team_rows = [
        {"team": "MIA", "season": 2023, "week": w, "def_sacks": 3, "def_interceptions": 1,
         "def_fumbles": 1, "def_fumbles_forced": 1, "def_tds": 0, "special_teams_tds": 0, "def_safeties": 0}
        for w in (1, 2)
    ]
    game_rows = [
        {"season": 2023, "week": w, "home_team": "MIA", "away_team": "NE", "home_score": 24, "away_score": 17,
         "gameday": f"2023-09-{9+w:02d}", "gametime": "13:00"}
        for w in (1, 2)
    ]

    return {
        "stats_player_week_2023.csv": _csv(_PLAYER_FIELDS, player_rows),
        "stats_team_week_2023.csv": _csv(_TEAM_FIELDS, team_rows),
        "roster_weekly_2023.csv": _csv(_ROSTER_FIELDS, roster_rows),
        "schedules/games.csv": _csv(_GAME_FIELDS, game_rows),
    }


def _opener(overrides: dict[str, bytes] | None = None):
    payloads = _season_bytes()
    if overrides:
        payloads.update(overrides)

    def opener(url: str) -> bytes:
        for key, payload in payloads.items():
            if key in url:
                return payload
        if "stats_player_week" in url or "roster_weekly" in url:
            return b"player_display_name,position,team,season,week\n"
        if "stats_team_week" in url:
            return b"team,season,week\n"
        if "injuries" in url:
            return b"season,week,team,full_name,report_status\n"
        return b"unexpected,call\n1,2\n"
    return opener


@pytest.fixture
def cfg() -> Config:
    return Config()


class TestProjectionsFor:
    def test_naive_dispatch(self, cfg, tmp_path):
        result = _projections_for("naive", 2023, 2, cfg, tmp_path, _opener())
        assert isinstance(result, dict)

    def test_unknown_source_raises(self, cfg, tmp_path):
        with pytest.raises(ValueError):
            _projections_for("bogus", 2023, 2, cfg, tmp_path, _opener())


class TestReplayWeek:
    def test_produces_one_decision_per_roster(self, cfg, tmp_path):
        result = replay_week(2023, 2, cfg, "naive", rosters_per_week=5, seed=11, cache_dir=tmp_path, opener=_opener())
        assert len(result.decisions) == 5
        assert len(result.lineups) == 5

    def test_every_decision_has_all_five_baseline_scores(self, cfg, tmp_path):
        result = replay_week(2023, 2, cfg, "naive", rosters_per_week=3, seed=11, cache_dir=tmp_path, opener=_opener())
        for decision in result.decisions:
            assert set(decision.points) == set(BASELINE_NAMES)

    def test_oracle_never_scores_below_any_other_baseline(self, cfg, tmp_path):
        # The single most valuable sanity assertion in the suite: if the
        # oracle (built on REALIZED points) ever loses to a baseline built
        # on projections, the grading key itself is broken.
        result = replay_week(2023, 2, cfg, "naive", rosters_per_week=10, seed=11, cache_dir=tmp_path, opener=_opener())
        for decision in result.decisions:
            oracle = decision.points["oracle"]
            for name in ("control", "agent", "consensus", "random_legal"):
                assert oracle >= decision.points[name] - 1e-9, (
                    f"oracle ({oracle}) scored below {name} ({decision.points[name]}) "
                    f"for roster {decision.roster_index}"
                )


class TestReplay:
    def test_concatenates_across_seasons_and_weeks(self, cfg, tmp_path):
        result = replay([2023], [1, 2], cfg, "naive", rosters_per_week=3, seed=11, cache_dir=tmp_path, opener=_opener())
        assert len(result.decisions) == 6  # 2 weeks x 3 rosters
        assert {d.week for d in result.decisions} == {1, 2}

    def test_different_weeks_do_not_draw_identical_roster_samples(self, cfg, tmp_path):
        result = replay([2023], [1, 2], cfg, "naive", rosters_per_week=3, seed=11, cache_dir=tmp_path, opener=_opener())
        week1_lineups = [l["control"].assignments for l, d in zip(result.lineups, result.decisions) if d.week == 1]
        week2_lineups = [l["control"].assignments for l, d in zip(result.lineups, result.decisions) if d.week == 2]
        week1_names = [{p.name for _s, p in a} for a in week1_lineups]
        week2_names = [{p.name for _s, p in a} for a in week2_lineups]
        assert week1_names != week2_names
