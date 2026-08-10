from __future__ import annotations

import pytest

from ffbot.config import (
    DefenseScoring,
    DistanceBand,
    KickingScoring,
    LeagueScoring,
    PassingScoring,
    ReceivingScoring,
    RushingScoring,
    Tier,
)
from ffbot.history.actuals import (
    defense_statline,
    player_statline,
    points_allowed_for,
    score_defense_row,
    score_player_row,
    week_actuals,
)

# A representative nflverse `stats_player_week` row (real column names,
# subset of real values) for a WR game.
_WR_ROW = {
    "player_display_name": "Justin Jefferson",
    "position": "WR",
    "team": "MIN",
    "season": "2023",
    "week": "5",
    "receptions": "8",
    "targets": "11",
    "receiving_yards": "128",
    "receiving_tds": "1",
    "receiving_2pt_conversions": "0",
    "fumbles_lost_total": "0",
    "passing_40": "",
}

_QB_ROW = {
    "player_display_name": "Josh Allen",
    "position": "QB",
    "team": "BUF",
    "season": "2023",
    "week": "5",
    "completions": "24",
    "attempts": "35",
    "passing_yards": "280",
    "passing_tds": "2",
    "passing_interceptions": "1",
    "passing_2pt_conversions": "0",
    "passing_40": "2",
    "carries": "6",
    "rushing_yards": "35",
    "rushing_tds": "1",
    "fumbles_lost_total": "0",
}

_K_ROW = {
    "player_display_name": "Test Kicker",
    "position": "K",
    "team": "SF",
    "fg_made": "3",
    "fg_att": "4",
    "fg_made_0_19": "0", "fg_made_20_29": "1", "fg_made_30_39": "1",
    "fg_made_40_49": "1", "fg_made_50_59": "0", "fg_made_60_": "0",
    "fg_missed_0_19": "0", "fg_missed_20_29": "0", "fg_missed_30_39": "0",
    "fg_missed_40_49": "1", "fg_missed_50_59": "0", "fg_missed_60_": "0",
    "pat_made": "2", "pat_missed": "1", "pat_blocked": "0",
}

_TEAM_DEF_ROW = {
    "team": "SF",
    "season": "2023",
    "week": "5",
    "def_sacks": "4",
    "def_interceptions": "2",
    "def_fumbles": "1",
    "def_fumbles_forced": "1",
    "def_tds": "1",
    "special_teams_tds": "0",
    "def_safeties": "0",
}

_GAME_ROW = {
    "home_team": "SF",
    "away_team": "DAL",
    "home_score": "28",
    "away_score": "10",
}


def _ppr() -> LeagueScoring:
    return LeagueScoring(
        passing=PassingScoring(yards_per_point=25, td=4, int=-2, two_pt=2),
        rushing=RushingScoring(yards_per_point=10, td=6, two_pt=2),
        receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0, two_pt=2),
    )


class TestPlayerStatline:
    def test_wr_row_maps_expected_fields(self):
        stats = player_statline(_WR_ROW)
        assert stats.rec == 8.0
        assert stats.rec_yds == 128.0
        assert stats.rec_td == 1.0
        assert stats.fumbles_lost == 0.0
        assert stats.pass_completion_40plus is None  # blank cell -> None, not 0

    def test_qb_row_maps_expected_fields(self):
        stats = player_statline(_QB_ROW)
        assert stats.pass_yds == 280.0
        assert stats.pass_td == 2.0
        assert stats.pass_int == 1.0
        assert stats.rush_yds == 35.0
        assert stats.rush_td == 1.0
        assert stats.pass_completion_40plus == 2.0

    def test_kicker_row_builds_bands_and_pat_missed(self):
        stats = player_statline(_K_ROW)
        assert stats.fg_made_bands == {
            "0-19": 0.0, "20-29": 1.0, "30-39": 1.0, "40-49": 1.0, "50-59": 0.0, "60-": 0.0,
        }
        assert stats.fg_missed_bands["40-49"] == 1.0
        assert stats.pat_missed == 1.0  # pat_missed(1) + pat_blocked(0)

    def test_row_with_no_fg_activity_leaves_bands_none(self):
        stats = player_statline(_WR_ROW)
        assert stats.fg_made_bands is None
        assert stats.fg_missed_bands is None


class TestScorePlayerRow:
    def test_wr_end_to_end(self):
        pts, flags = score_player_row(_WR_ROW, _ppr())
        assert pts == 8 * 1.0 + 128 / 10 + 1 * 6
        assert flags == ()

    def test_qb_end_to_end_with_bonus(self):
        from ffbot.config import BonusScoring

        league = _ppr()
        league.bonuses = BonusScoring(pass_completion_40plus=1.0)
        pts, _ = score_player_row(_QB_ROW, league)
        expected = 280 / 25 + 2 * 4 - 1 * 2 + 35 / 10 + 1 * 6 + 2 * 1.0
        assert round(pts, 4) == round(expected, 4)

    def test_kicker_end_to_end_banded(self):
        league = LeagueScoring(
            kicking=KickingScoring(
                pat_made=1.0, pat_missed=-1.0,
                fg_by_distance=[
                    DistanceBand(0, 39, 3), DistanceBand(40, 49, 4), DistanceBand(50, 99, 5),
                ],
            )
        )
        pts, flags = score_player_row(_K_ROW, league)
        # made: 1@20-29(3) + 1@30-39(3) + 1@40-49(4) = 10; pat: 2*1 - 1*1 = 1
        assert pts == 11.0
        assert flags == ()


class TestDefenseScoring:
    def test_defense_statline_sums_def_and_special_teams_tds(self):
        stats = defense_statline(_TEAM_DEF_ROW, points_allowed_game=10.0)
        assert stats.def_td == 1.0  # def_tds(1) + special_teams_tds(0)
        assert stats.sack == 4.0
        assert stats.points_allowed_game == 10.0

    def test_score_defense_row_end_to_end(self):
        league = LeagueScoring(
            defense=DefenseScoring(
                sack=1.0, interception=2.0, fumble_recovery=2.0, forced_fumble=0.0,
                touchdown=6.0, safety=2.0,
                points_allowed=[Tier(0, 10), Tier(6, 7), Tier(13, 4), Tier(999, -4)],
            )
        )
        pts, flags = score_defense_row(_TEAM_DEF_ROW, 10.0, league)
        # 4 sacks + 2 int*2 + 1 fr*2 + 1 td*6 + 0 safety + PA(10 -> tier (6,13]=4)
        assert pts == 4 + 4 + 2 + 6 + 0 + 4
        assert flags == ()


class TestPointsAllowedFor:
    def test_home_team_allowed_away_score(self):
        assert points_allowed_for("SF", _GAME_ROW) == 10.0

    def test_away_team_allowed_home_score(self):
        assert points_allowed_for("DAL", _GAME_ROW) == 28.0

    def test_team_not_in_game_returns_none(self):
        assert points_allowed_for("KC", _GAME_ROW) is None

    def test_case_and_whitespace_insensitive(self):
        assert points_allowed_for(" sf ", _GAME_ROW) == 10.0


# --- week_actuals: the grading key --------------------------------------

_WEEK_ACTUALS_PLAYER_ROWS = (
    "player_display_name,position,team,season,week,receptions,receiving_yards,receiving_tds,fumbles_lost_total\n"
    "Real Wideout,WR,SF,2023,5,8,80,1,0\n"        # 8 + 8.0 + 6 = 22.0
    "Some Lineman,OL,SF,2023,5,,,,\n"              # not a scorable position -- excluded
    "Wrong Week Guy,WR,SF,2023,6,1,10,0,0\n"       # different week -- excluded
)

_WEEK_ACTUALS_TEAM_ROWS = (
    "team,season,week,def_sacks,def_interceptions,def_fumbles,def_fumbles_forced,def_tds,special_teams_tds,def_safeties\n"
    "SF,2023,5,4,2,1,1,1,0,0\n"
    "DAL,2023,6,1,0,0,0,0,0,0\n"  # different week -- excluded
)

_WEEK_ACTUALS_GAMES = (
    "season,week,home_team,away_team,home_score,away_score\n"
    "2023,5,SF,DAL,28,10\n"
)


def _week_actuals_opener(calls: list | None = None):
    def opener(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        if "stats_player_week_2023.csv" in url:
            return _WEEK_ACTUALS_PLAYER_ROWS.encode("utf-8")
        if "stats_team_week_2023.csv" in url:
            return _WEEK_ACTUALS_TEAM_ROWS.encode("utf-8")
        if "schedules/games.csv" in url:
            return _WEEK_ACTUALS_GAMES.encode("utf-8")
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


class TestWeekActuals:
    def test_scores_players_and_defenses_keyed_by_actuals_key(self, tmp_path):
        league = LeagueScoring(
            passing=PassingScoring(yards_per_point=25, td=4, int=-2),
            rushing=RushingScoring(yards_per_point=10, td=6),
            receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0),
            defense=DefenseScoring(sack=1.0, interception=2.0, fumble_recovery=2.0, forced_fumble=1.0, touchdown=6.0),
        )
        out = week_actuals(2023, 5, league, cache_dir=tmp_path, opener=_week_actuals_opener())

        assert out["real wideout:WR"] == pytest.approx(22.0)
        # 4 sack*1 + 2 int*2 + 1 fr*2 + 1 ff*1 + 1 td*6 = 4+4+2+1+6 = 17
        assert out["sf:DEF"] == pytest.approx(17.0)

    def test_excludes_non_scorable_positions_and_other_weeks(self, tmp_path):
        league = LeagueScoring()
        out = week_actuals(2023, 5, league, cache_dir=tmp_path, opener=_week_actuals_opener())
        assert "some lineman:OL" not in out
        assert "wrong week guy:WR" not in out
        assert "dal:DEF" not in out  # DAL's row was week 6, not 5

    def test_never_fetches_a_leaked_source(self, tmp_path):
        # `_week_actuals_opener` raises on anything besides the three
        # legitimate sources -- this test exists purely so an accidental new
        # fetch (e.g. injuries, or the ECR archive) fails loudly here.
        calls: list[str] = []
        week_actuals(2023, 5, LeagueScoring(), cache_dir=tmp_path, opener=_week_actuals_opener(calls))
        assert len(calls) == 3
