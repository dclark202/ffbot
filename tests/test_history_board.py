from __future__ import annotations

import csv
import gzip
import io
import json

import pytest

from ffbot.config import Config, LeagueScoring, ReceivingScoring, RushingScoring
from ffbot.history.board import (
    _fit_season_rank_to_points_curve,
    _load_preseason_ecr_by_date,
    _preseason_snapshot,
    historical_board,
)
from ffbot.history.projections import _first_game_days


def _gzip(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


_ECR_FIELDS = ["fp_page", "ecr_type", "player", "pos", "team", "ecr", "scrape_date"]


def _ecr_row(page, player, pos, team, rank, date):
    return {"fp_page": page, "ecr_type": "rp", "player": player, "pos": pos, "team": team, "ecr": rank, "scrape_date": date}


def _ecr_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_ECR_FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return _gzip(buf.getvalue())


_GAMES_FIELDS = ["season", "week", "home_team", "away_team", "home_score", "away_score", "gameday", "gametime"]


def _games_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_GAMES_FIELDS, restval="")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


_PLAYER_FIELDS = ["player_display_name", "position", "team", "season", "week", "receptions", "receiving_yards", "receiving_tds", "carries", "rushing_yards", "rushing_tds", "fumbles_lost_total"]


def _stat_row(name, pos, team, season, week, recyd=0, rushyd=0):
    return {
        "player_display_name": name, "position": pos, "team": team, "season": season, "week": week,
        "receptions": 5 if pos == "WR" else 0, "receiving_yards": recyd, "receiving_tds": 0,
        "carries": 15 if pos == "RB" else 0, "rushing_yards": rushyd, "rushing_tds": 0, "fumbles_lost_total": 0,
    }


def _stats_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_PLAYER_FIELDS, restval="")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


_ECR_ROWS = [
    # 2021 (fit season): Real Wideout is rank 1, scores well both games.
    _ecr_row("/nfl/rankings/ppr-wr-cheatsheets.php", "Real Wideout", "WR", "MIA", 1, "2021-07-10"),
    _ecr_row("/nfl/rankings/ppr-rb-cheatsheets.php", "Steady Runner", "RB", "MIA", 1, "2021-07-10"),
    # 2022 (fit season): a different WR takes rank 1 and scores even better;
    # Real Wideout slips to rank 2 and scores less.
    _ecr_row("/nfl/rankings/ppr-wr-cheatsheets.php", "Other WR", "WR", "NE", 1, "2022-07-10"),
    _ecr_row("/nfl/rankings/ppr-wr-cheatsheets.php", "Real Wideout", "WR", "MIA", 2, "2022-07-10"),
    _ecr_row("/nfl/rankings/ppr-rb-cheatsheets.php", "Steady Runner", "RB", "MIA", 1, "2022-07-10"),
    # 2023 (target season): a brand-new player ranked 1 -- the curve fit on
    # 2021/2022 must project them, never a number derived from 2023 itself.
    _ecr_row("/nfl/rankings/ppr-wr-cheatsheets.php", "New Star WR", "WR", "BUF", 1, "2023-07-10"),
    _ecr_row("/nfl/rankings/ppr-rb-cheatsheets.php", "Steady Runner", "RB", "MIA", 1, "2023-07-10"),
]

_STATS_2021 = [
    _stat_row("Real Wideout", "WR", "MIA", 2021, 1, recyd=100),
    _stat_row("Real Wideout", "WR", "MIA", 2021, 2, recyd=100),  # 2 games x 10.0 = 20.0/game -> 40.0 total (PPR: 5 rec*1 + 100/10=10 -> 15/game -> 30 total, see _ppr() below)
    _stat_row("Steady Runner", "RB", "MIA", 2021, 1, rushyd=50),
    _stat_row("Steady Runner", "RB", "MIA", 2021, 2, rushyd=50),
]
_STATS_2022 = [
    _stat_row("Other WR", "WR", "NE", 2022, 1, recyd=200),
    _stat_row("Other WR", "WR", "NE", 2022, 2, recyd=200),
    _stat_row("Real Wideout", "WR", "MIA", 2022, 1, recyd=50),
    _stat_row("Real Wideout", "WR", "MIA", 2022, 2, recyd=50),
    _stat_row("Steady Runner", "RB", "MIA", 2022, 1, rushyd=50),
    _stat_row("Steady Runner", "RB", "MIA", 2022, 2, rushyd=50),
]

_GAMES_ROWS = [
    {"season": 2021, "week": 1, "home_team": "MIA", "away_team": "NE", "home_score": 20, "away_score": 10, "gameday": "2021-09-09", "gametime": "13:00"},
    {"season": 2022, "week": 1, "home_team": "MIA", "away_team": "NE", "home_score": 20, "away_score": 10, "gameday": "2022-09-08", "gametime": "13:00"},
    {"season": 2023, "week": 1, "home_team": "MIA", "away_team": "BUF", "home_score": 20, "away_score": 10, "gameday": "2023-09-07", "gametime": "13:00"},
]


def _ppr() -> Config:
    cfg = Config()
    cfg.league = LeagueScoring(
        receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0),
        rushing=RushingScoring(yards_per_point=10, td=6),
    )
    cfg.draft.num_teams = 2
    cfg.roster_positions = {"WR": 1, "RB": 1, "BN": 1}
    return cfg


def _opener(overrides: dict[str, bytes] | None = None):
    payloads = {
        "db_fpecr.csv.gz": _ecr_csv(_ECR_ROWS),
        "schedules/games.csv": _games_csv(_GAMES_ROWS),
        "stats_player_week_2021.csv": _stats_csv(_STATS_2021),
        "stats_player_week_2022.csv": _stats_csv(_STATS_2022),
        "stats_team_week_2021.csv": b"team,season,week\n",
        "stats_team_week_2022.csv": b"team,season,week\n",
        "adp/ppr": json.dumps({"players": [
            {"name": "New Star WR", "position": "WR", "team": "BUF", "adp": 12.0, "stdev": 2.0, "bye": 7},
            {"name": "Steady Runner", "position": "RB", "team": "MIA", "adp": 30.0, "stdev": 3.0, "bye": 5},
        ]}).encode("utf-8"),
    }
    if overrides:
        payloads.update(overrides)

    def opener(url: str) -> bytes:
        for key, payload in payloads.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


class TestPreseasonSnapshot:
    def test_parses_offense_and_uses_actuals_key_convention(self, tmp_path):
        by_date = _load_preseason_ecr_by_date(tmp_path, _opener())
        snapshot = _preseason_snapshot("2023-07-10", by_date)
        rank, display, team = snapshot["new star wr:WR"]
        assert (rank, display, team) == (1.0, "New Star WR", "BUF")
        assert snapshot["steady runner:RB"][0] == 1.0

    def test_only_preseason_pages_included(self, tmp_path):
        rows = _ECR_ROWS + [
            {"fp_page": "/nfl/rankings/ros-ppr-wr.php", "ecr_type": "rp", "player": "In Season Guy",
             "pos": "WR", "team": "MIA", "ecr": "1", "scrape_date": "2023-07-10"},
        ]
        by_date = _load_preseason_ecr_by_date(tmp_path, _opener({"db_fpecr.csv.gz": _ecr_csv(rows)}))
        snapshot = _preseason_snapshot("2023-07-10", by_date)
        assert "in season guy:WR" not in snapshot


class TestFitSeasonRankToPointsCurve:
    def test_curve_averages_across_fit_seasons_at_the_same_rank(self, tmp_path):
        cfg = _ppr()
        by_date = _load_preseason_ecr_by_date(tmp_path, _opener())
        game_days = _first_game_days(tmp_path, _opener())
        curve = _fit_season_rank_to_points_curve([2021, 2022], cfg, by_date, game_days, tmp_path, _opener())
        # rank 1 WR bucket: 2021's Real Wideout (rank 1) + 2022's Other WR
        # (rank 1) -- rank 2 WR bucket: 2022's Real Wideout only.
        assert "WR" in curve
        ranks = [r for r, _pts in curve["WR"]]
        assert ranks == sorted(ranks)


class TestHistoricalBoard:
    def test_refuses_fit_seasons_containing_the_target_season(self, tmp_path):
        cfg = _ppr()
        with pytest.raises(ValueError):
            historical_board(2023, cfg, num_teams=2, fit_seasons=[2022, 2023], cache_dir=tmp_path, opener=_opener())

    def test_raises_with_no_cached_week_1_schedule(self, tmp_path):
        cfg = _ppr()
        with pytest.raises(ValueError):
            historical_board(2099, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path, opener=_opener())

    def test_raises_with_no_qualifying_preseason_scrape(self, tmp_path):
        cfg = _ppr()
        games = _games_csv(_GAMES_ROWS + [
            {"season": 2019, "week": 1, "home_team": "MIA", "away_team": "NE", "home_score": 20, "away_score": 10, "gameday": "2019-01-01", "gametime": "13:00"},
        ])
        with pytest.raises(ValueError):
            historical_board(2019, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path,
                              opener=_opener({"schedules/games.csv": games}))

    def test_raises_when_newest_scrape_is_far_older_than_target_week_1(self, tmp_path):
        # The real bug this guards: with the DynastyProcess archive frozen
        # at some date, `_latest_scrape_before` still happily returns the
        # newest available scrape for ANY later target season, with no
        # complaint about how stale it is. A target season whose week 1 is
        # ~3 years after the newest cached scrape must raise, not silently
        # hand back a multi-year-old preseason cheatsheet.
        cfg = _ppr()
        games = _games_csv(_GAMES_ROWS + [
            {"season": 2026, "week": 1, "home_team": "MIA", "away_team": "BUF", "home_score": 20, "away_score": 10, "gameday": "2026-09-06", "gametime": "13:00"},
        ])
        with pytest.raises(ValueError, match="days earlier"):
            historical_board(2026, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path,
                              opener=_opener({"schedules/games.csv": games}))

    def test_scrape_within_the_staleness_window_is_accepted(self, tmp_path):
        # The existing 2023 fixture's scrape (2023-07-10) sits 59 days before
        # its week 1 (2023-09-07) -- comfortably inside the window. This is
        # a sanity check that the staleness guard doesn't also reject a
        # perfectly legitimate, freshly-scraped board.
        cfg = _ppr()
        historical_board(2023, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path, opener=_opener())

    def test_end_to_end_board_has_adp_and_vor_ranking(self, tmp_path):
        cfg = _ppr()
        board = historical_board(2023, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path, opener=_opener())

        names = {bp.name for bp in board.players}
        assert names == {"New Star WR", "Steady Runner"}

        by_name = {bp.name: bp for bp in board.players}
        assert by_name["New Star WR"].adp == 12.0
        assert by_name["Steady Runner"].adp == 30.0

        # rank is 1-indexed by VOR descending, contiguous.
        ranks = sorted(bp.rank for bp in board.players)
        assert ranks == list(range(1, len(board.players) + 1))

    def test_player_names_preserve_display_casing_not_normalized(self, tmp_path):
        # A real regression: BoardPlayer.name must be "New Star WR", never
        # the lowercased actuals_key form ("new star wr") that matching
        # internally uses -- this field is shown directly to a user.
        cfg = _ppr()
        board = historical_board(2023, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path, opener=_opener())
        names = {bp.name for bp in board.players}
        assert "New Star WR" in names
        assert "new star wr" not in names

    def test_team_falls_back_to_ecr_row_when_adp_has_no_match(self, tmp_path):
        # New Star WR has ADP; give a second target-season player no ADP
        # entry at all and confirm their team still comes from the ECR row
        # rather than going blank.
        cfg = _ppr()
        rows = _ECR_ROWS + [_ecr_row("/nfl/rankings/ppr-rb-cheatsheets.php", "No Adp RB", "RB", "DAL", 2, "2023-07-10")]
        board = historical_board(
            2023, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path,
            opener=_opener({"db_fpecr.csv.gz": _ecr_csv(rows)}),
        )
        by_name = {bp.name: bp for bp in board.players}
        assert by_name["No Adp RB"].team == "DAL"
        assert by_name["No Adp RB"].adp is None

    def test_missing_adp_entry_leaves_adp_none_not_a_crash(self, tmp_path):
        cfg = _ppr()
        board = historical_board(
            2023, cfg, num_teams=2, fit_seasons=[2021, 2022], cache_dir=tmp_path,
            opener=_opener({"adp/ppr": json.dumps({"players": []}).encode("utf-8")}),
        )
        assert all(bp.adp is None for bp in board.players)
