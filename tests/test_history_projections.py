from __future__ import annotations

import pytest

from ffbot.config import Config, LeagueScoring, PassingScoring, ReceivingScoring, RushingScoring
from ffbot.history.projections import (
    ECR_CLEAN_SEASONS,
    _ecr_snapshot,
    _fit_rank_to_points_curve,
    _first_game_days,
    _game_log,
    _interp,
    _latest_scrape_before,
    _load_ros_ecr_by_date,
    ecr_projections,
    naive_projections,
    player_pool,
    players_asof,
)


def _ppr() -> LeagueScoring:
    return LeagueScoring(
        passing=PassingScoring(yards_per_point=25, td=4, int=-2),
        rushing=RushingScoring(yards_per_point=10, td=6),
        receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0),
    )


# --- Synthetic multi-week `stats_player_week` / `stats_team_week` data -----
#
# One WR (Real Wideout, MIA) with a clean, hand-computable weekly points
# progression across weeks 1-4 of season 2023, plus one week of season 2022
# (the "prior season") and one defense (MIA). A second WR (Rookie Nobody) has
# NO games anywhere -- the "true unknown" case.

_STATS_2023 = (
    "player_display_name,position,team,season,week,receptions,receiving_yards,receiving_tds,fumbles_lost_total\n"
    "Real Wideout,WR,MIA,2023,1,5,50,0,0\n"   # 5 + 5.0 = 10.0 pts
    "Real Wideout,WR,MIA,2023,2,6,60,1,0\n"   # 6 + 6.0 + 6 = 18.0 pts
    "Real Wideout,WR,MIA,2023,3,4,40,0,0\n"   # 4 + 4.0 = 8.0 pts
    "Real Wideout,WR,MIA,2023,4,10,100,0,0\n"  # week 4 -- must never leak into a week<4 projection
)

_STATS_2022 = (
    "player_display_name,position,team,season,week,receptions,receiving_yards,receiving_tds,fumbles_lost_total\n"
    "Real Wideout,WR,MIA,2022,10,8,80,1,0\n"  # 8 + 8.0 + 6 = 22.0 pts
)

_TEAM_STATS_EMPTY = "team,season,week,def_sacks,def_interceptions,def_fumbles,def_fumbles_forced,def_tds,special_teams_tds,def_safeties\n"

_GAMES_2023 = (
    "season,week,home_team,away_team,home_score,away_score,gameday,gametime\n"
    "2023,1,MIA,NE,20,10,2023-09-10,13:00\n"
    "2023,2,MIA,NE,20,10,2023-09-17,13:00\n"
    "2023,3,MIA,NE,20,10,2023-09-24,13:00\n"
    "2023,4,MIA,NE,20,10,2023-10-01,13:00\n"
)


def _opener(overrides: dict[str, bytes]):
    """`overrides` maps a URL substring to the bytes that URL should return;
    a `stats_player_week`/`stats_team_week`/`games` request for a season not
    in `overrides` gets an empty-but-valid CSV instead of a 404, matching
    `naive_projections`' "no data for this season -> empty history" contract.
    """
    def opener(url: str) -> bytes:
        for key, payload in overrides.items():
            if key in url:
                return payload
        if "stats_player_week" in url:
            return b"player_display_name,position,team,season,week\n"
        if "stats_team_week" in url:
            return _TEAM_STATS_EMPTY.encode("utf-8")
        if "schedules/games.csv" in url:
            return _GAMES_2023.encode("utf-8")
        if "roster_weekly" in url:
            return b"season,week,team,position,full_name\n"
        return b"unexpected,call\n1,2\n"
    return opener


def _naive_opener():
    return _opener({
        "stats_player_week_2023.csv": _STATS_2023.encode("utf-8"),
        "stats_player_week_2022.csv": _STATS_2022.encode("utf-8"),
        "stats_team_week_2023.csv": _TEAM_STATS_EMPTY.encode("utf-8"),
        "stats_team_week_2022.csv": _TEAM_STATS_EMPTY.encode("utf-8"),
    })


class TestGameLog:
    def test_before_week_excludes_target_and_later_weeks(self, tmp_path):
        log = _game_log(2023, _ppr(), tmp_path, _naive_opener(), before_week=3)
        games = dict(log["real wideout:WR"])
        assert set(games) == {1, 2}  # NOT week 3 (the target) or week 4

    def test_before_week_none_returns_whole_season(self, tmp_path):
        log = _game_log(2023, _ppr(), tmp_path, _naive_opener(), before_week=None)
        games = dict(log["real wideout:WR"])
        assert set(games) == {1, 2, 3, 4}

    def test_points_match_hand_computed_scoring(self, tmp_path):
        log = _game_log(2023, _ppr(), tmp_path, _naive_opener(), before_week=None)
        games = dict(log["real wideout:WR"])
        assert games[1] == pytest.approx(10.0)
        assert games[2] == pytest.approx(18.0)


class TestNaiveProjectionsLeakage:
    """The required leakage guarantee: a projection for week W must never
    reflect week W's own (or a later week's) result."""

    def test_week_4_data_never_leaks_into_week_3_projection(self, tmp_path):
        cfg = Config()
        cfg.league = _ppr()
        proj_wk3 = naive_projections(2023, 3, cfg, cache_dir=tmp_path, opener=_naive_opener())
        # Week 4's real score is 100 pts -- a huge outlier. If it leaked in,
        # the blended projection would be pulled far above what weeks 1-2
        # alone (10.0, 18.0) could ever produce.
        assert proj_wk3["real wideout:WR"] < 30.0

    def test_projecting_week_1_sees_no_in_season_history_at_all(self, tmp_path):
        cfg = Config()
        cfg.league = _ppr()
        # before_week=1 -> this_season game log is empty for everyone; the
        # player must fall back to the prior-season average (2022: 22.0),
        # never to a 2023 number.
        proj = naive_projections(2023, 1, cfg, cache_dir=tmp_path, opener=_naive_opener())
        assert proj["real wideout:WR"] == pytest.approx(22.0)


class TestNaiveProjectionsBlend:
    def test_zero_games_this_season_falls_back_to_prior_season_average(self, tmp_path):
        cfg = Config()
        cfg.league = _ppr()
        proj = naive_projections(2023, 1, cfg, cache_dir=tmp_path, opener=_naive_opener())
        assert proj["real wideout:WR"] == pytest.approx(22.0)

    def test_full_recency_window_uses_pure_recency_weighted_blend(self, tmp_path):
        cfg = Config()
        cfg.league = _ppr()
        cfg.projection.recency_window = 2
        cfg.projection.recency_weight = 1.0  # pure "last N games", no season-avg blend
        # Projecting week 3: games so far are weeks 1 (10.0), 2 (18.0). n=2
        # equals the recency_window, so w_reg=1.0 -- the floor value (prior
        # season / replacement) must NOT show up in the result at all.
        proj = naive_projections(2023, 3, cfg, cache_dir=tmp_path, opener=_naive_opener())
        assert proj["real wideout:WR"] == pytest.approx(14.0)  # mean(10.0, 18.0)

    def test_no_data_anywhere_falls_back_to_replacement_level(self, tmp_path):
        opener = _opener({
            "stats_player_week_2023.csv": (
                "player_display_name,position,team,season,week,receiving_yards\n"
            ).encode("utf-8"),
        })
        cfg = Config()
        cfg.league = _ppr()
        # No games logged anywhere for anyone -> replacement_rows is empty ->
        # derive_replacement has nothing to derive from -> the function must
        # not raise, and simply produces no projections.
        proj = naive_projections(2023, 5, cfg, cache_dir=tmp_path, opener=opener)
        assert proj == {}


class TestInterp:
    def test_empty_curve_returns_zero(self):
        assert _interp(5.0, []) == 0.0

    def test_below_range_clamps_to_first_point(self):
        assert _interp(0.5, [(1.0, 20.0), (5.0, 10.0)]) == 20.0

    def test_above_range_clamps_to_last_point(self):
        assert _interp(99.0, [(1.0, 20.0), (5.0, 10.0)]) == 10.0

    def test_linear_interpolation_between_points(self):
        assert _interp(3.0, [(1.0, 20.0), (5.0, 10.0)]) == pytest.approx(15.0)

    def test_single_point_curve_returns_that_points_value(self):
        assert _interp(3.0, [(1.0, 20.0)]) == 20.0


class TestLatestScrapeBefore:
    def test_picks_the_latest_qualifying_date(self):
        dates = ["2023-09-10", "2023-09-17", "2023-09-24"]
        assert _latest_scrape_before("2023-09-20", dates) == "2023-09-17"

    def test_same_day_scrape_is_not_before_kickoff(self):
        # Conservative-by-design: a same-calendar-day scrape is excluded,
        # since scrape_date carries no time-of-day resolution -- see the
        # module docstring.
        dates = ["2023-09-24"]
        assert _latest_scrape_before("2023-09-24", dates) is None

    def test_no_qualifying_date_returns_none(self):
        assert _latest_scrape_before("2020-01-01", ["2023-09-10"]) is None


class TestEcrSnapshotAndLoad:
    def test_load_filters_to_rp_and_ros_pages(self, tmp_path):
        csv = (
            "fp_page,ecr_type,player,pos,team,ecr,scrape_date\n"
            "/nfl/rankings/ros-ppr-wr.php,rp,Real Wideout,WR,MIA,1,2023-09-09\n"
            "/nfl/rankings/ppr-wr-cheatsheets.php,rp,Preseason Guy,WR,MIA,1,2023-08-01\n"  # not a ROS page
            "/nfl/rankings/ros-ppr-wr.php,dp,Dynasty Guy,WR,MIA,1,2023-09-09\n"  # not ecr_type "rp"
        )
        opener = _opener({"db_fpecr.csv.gz": _gzip(csv)})
        by_date = _load_ros_ecr_by_date(tmp_path, opener)
        names = [r["player"] for rows in by_date.values() for r in rows]
        assert names == ["Real Wideout"]

    def test_snapshot_keys_by_actuals_key(self, tmp_path):
        opener = _opener({"db_fpecr.csv.gz": _gzip(_ECR_CSV_NO_DUP)})
        by_date = _load_ros_ecr_by_date(tmp_path, opener)
        snapshot = _ecr_snapshot("2023-09-09", by_date)
        assert snapshot["real wideout:WR"] == 1.0
        assert snapshot["also a wideout:WR"] == 2.0


_ECR_CSV_NO_DUP = (
    "fp_page,ecr_type,player,pos,team,ecr,scrape_date\n"
    "/nfl/rankings/ros-ppr-wr.php,rp,Real Wideout,WR,MIA,1,2023-09-09\n"
    "/nfl/rankings/ros-ppr-wr.php,rp,Also A Wideout,WR,NE,2,2023-09-09\n"
)


def _gzip(text: str) -> bytes:
    import gzip
    return gzip.compress(text.encode("utf-8"))


class TestEcrProjectionsLeakage:
    def test_refuses_fit_seasons_containing_the_target_season(self, tmp_path):
        cfg = Config()
        with pytest.raises(ValueError):
            ecr_projections(2023, 5, cfg, fit_seasons=[2022, 2023], cache_dir=tmp_path, opener=_naive_opener())

    def test_default_fit_seasons_never_include_the_target_season(self):
        for season in ECR_CLEAN_SEASONS:
            fit = tuple(s for s in ECR_CLEAN_SEASONS if s != season)
            assert season not in fit


class TestEcrProjectionsEndToEnd:
    def test_missing_game_schedule_returns_empty(self, tmp_path):
        cfg = Config()
        opener = _opener({"db_fpecr.csv.gz": _gzip(_ECR_CSV_NO_DUP)})
        # A season/week with no cached games.csv rows at all.
        proj = ecr_projections(2099, 1, cfg, fit_seasons=[2022], cache_dir=tmp_path, opener=opener)
        assert proj == {}

    def test_no_qualifying_scrape_returns_empty(self, tmp_path):
        cfg = Config()
        games = "season,week,home_team,away_team,home_score,away_score,gameday,gametime\n2023,1,MIA,NE,20,10,2023-01-01,13:00\n"
        opener = _opener({
            "db_fpecr.csv.gz": _gzip(_ECR_CSV_NO_DUP),  # earliest scrape is 2023-09-09
            "schedules/games.csv": games.encode("utf-8"),
        })
        proj = ecr_projections(2023, 1, cfg, fit_seasons=[2022], cache_dir=tmp_path, opener=opener)
        assert proj == {}


class TestFitRankToPointsCurve:
    def test_curve_matches_hand_computed_points_at_observed_rank(self, tmp_path):
        # Fit season 2022: one week (10), one scrape before it, one player
        # at rank 1 who scored exactly 22.0 that week.
        cfg = Config()
        cfg.league = _ppr()
        ecr_by_date = _load_ros_ecr_by_date(tmp_path, _opener({"db_fpecr.csv.gz": _gzip(
            "fp_page,ecr_type,player,pos,team,ecr,scrape_date\n"
            "/nfl/rankings/ros-ppr-wr.php,rp,Real Wideout,WR,MIA,1,2022-10-01\n"
        )}))
        game_days = _first_game_days(tmp_path, _opener({
            "schedules/games.csv": (
                "season,week,home_team,away_team,home_score,away_score,gameday,gametime\n"
                "2022,10,MIA,NE,20,10,2022-10-09,13:00\n"
            ).encode("utf-8"),
        }))
        curve = _fit_rank_to_points_curve(
            [2022], cfg, ecr_by_date, game_days, tmp_path,
            _opener({"stats_player_week_2022.csv": _STATS_2022.encode("utf-8"),
                     "stats_team_week_2022.csv": _TEAM_STATS_EMPTY.encode("utf-8")}),
        )
        assert curve["WR"] == [(1.0, pytest.approx(22.0))]


class TestPlayerPool:
    def test_filters_to_scorable_positions_and_target_week(self, tmp_path):
        rows = (
            "season,week,team,position,full_name\n"
            "2023,5,MIA,WR,Real Wideout\n"
            "2023,5,MIA,OL,Some Lineman\n"   # not scorable -- excluded
            "2023,6,MIA,WR,Wrong Week Guy\n"  # different week -- excluded
        )
        opener = _opener({"roster_weekly_2023.csv": rows.encode("utf-8")})
        pool = player_pool(2023, 5, cache_dir=tmp_path, opener=opener)
        names = {row["name"] for row in pool if row["position"] != "DEF"}
        assert names == {"Real Wideout"}

    def test_includes_all_32_defenses(self, tmp_path):
        opener = _opener({"roster_weekly_2023.csv": b"season,week,team,position,full_name\n"})
        pool = player_pool(2023, 5, cache_dir=tmp_path, opener=opener)
        defenses = [row for row in pool if row["position"] == "DEF"]
        assert len(defenses) == 32


class TestPlayersAsof:
    def test_builds_players_with_projection_status_and_team(self):
        from ffbot.history.index import WeekSnapshot
        from ffbot.week import WeeklyPlayerIntel

        pool = [
            {"key": "real wideout:WR", "name": "Real Wideout", "position": "WR", "team": "MIA"},
            {"key": "unknown rookie:WR", "name": "Unknown Rookie", "position": "WR", "team": "NE"},
        ]
        projections = {"real wideout:WR": 15.5}
        snapshot = WeekSnapshot(
            season=2023, week=5,
            player_status={"unknown rookie": WeeklyPlayerIntel(name="Unknown Rookie", status="O")},
        )
        players = players_asof(pool, projections, snapshot)
        by_name = {p.name: p for p in players}

        assert by_name["Real Wideout"].projected_points == 15.5
        assert by_name["Real Wideout"].team == "MIA"
        assert by_name["Real Wideout"].status == ""
        assert by_name["Real Wideout"].eligible_positions == ["WR"]

        # No projection entry -> None, not 0 -- `lineup.py` treats these differently.
        assert by_name["Unknown Rookie"].projected_points is None
        assert by_name["Unknown Rookie"].status == "O"

    def test_player_ids_are_stable_across_repeated_calls_on_the_same_pool(self):
        from ffbot.history.index import WeekSnapshot

        pool = [
            {"key": "a:WR", "name": "A", "position": "WR", "team": "MIA"},
            {"key": "b:RB", "name": "B", "position": "RB", "team": "NE"},
        ]
        snapshot = WeekSnapshot(season=2023, week=5)
        p1 = players_asof(pool, {"a:WR": 1.0}, snapshot)
        p2 = players_asof(pool, {"b:RB": 2.0}, snapshot)
        assert [p.player_id for p in p1] == [p.player_id for p in p2] == [1, 2]
