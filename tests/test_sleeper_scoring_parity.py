"""Our weekly points must be the number the Sleeper app shows.

The payloads and settings below were recorded from the live feed and this
league on 2026-09-13, when the report showed Cam Little at 9.4 (Sleeper: 6.5)
and the Chiefs DEF at 7.5 (Sleeper: 8.5) -- see
docs/dev/INSEASON-FINDINGS.md. The expected totals are Sleeper's own.
"""

from __future__ import annotations

import pytest

from ffbot.board import apply_league_scoring
from ffbot.config import LeagueScoring
from ffbot.projections.sleeper import _row_from_entry
from ffbot.scoring import score_sleeper_stats, score_statline

SETTINGS = {
    "blk_kick": 2.0, "def_3_and_out": 2.0, "def_st_ff": 1.0, "def_st_fum_rec": 1.0, "def_st_td": 6.0,
    "def_td": 6.0, "ff": 1.0, "fgm_0_19": 3.0, "fgm_20_29": 3.0, "fgm_30_39": 3.0, "fgm_40_49": 4.0,
    "fgm_50_59": 5.0, "fgm_60p": 6.0, "fgmiss": -1.0, "fum": 0.0, "fum_lost": -2.0, "fum_rec": 2.0,
    "fum_rec_td": 6.0, "int": 2.0, "pass_2pt": 2.0, "pass_cmp_40p": 2.0, "pass_int": -2.0, "pass_td": 4.0,
    "pass_td_40p": 0.0, "pass_td_50p": 2.0, "pass_yd": 0.04, "pts_allow_0": 10.0, "pts_allow_14_20": 1.0,
    "pts_allow_1_6": 7.0, "pts_allow_21_27": 0.0, "pts_allow_28_34": -1.0, "pts_allow_35p": -4.0,
    "pts_allow_7_13": 4.0, "rec": 1.0, "rec_2pt": 2.0, "rec_td": 6.0, "rec_yd": 0.1, "rush_2pt": 2.0,
    "rush_40p": 2.0, "rush_td": 6.0, "rush_td_40p": 0.0, "rush_td_50p": 2.0, "rush_yd": 0.1, "sack": 1.0,
    "safe": 2.0, "st_ff": 1.0, "st_fum_rec": 1.0, "st_td": 6.0, "xpm": 1.0, "xpmiss": -1.0,
}

LAWRENCE = {
    "bonus_rush_td_qb": 0.32, "cmp_pct": 61.63, "fum": 0.43, "fum_lost": 0.19, "gp": 1.0, "pass_2pt": 0.09,
    "pass_att": 32.49, "pass_cmp": 20.02, "pass_cmp_40p": 0.42, "pass_fd": 22.81, "pass_inc": 12.46,
    "pass_int": 0.65, "pass_sack": 2.68, "pass_td": 1.7, "pass_yd": 228.12, "pts_ppr": 18.59,
    "rush_2pt": 0.02, "rush_40p": 0.05, "rush_att": 4.38, "rush_fd": 1.58, "rush_td": 0.32, "rush_yd": 15.82,
}
KC_DEF = {
    "blk_kick": 0.06, "def_fum_td": 0.06, "def_kr_yd": 102.09, "def_pr_yd": 119.32, "def_td": 0.19, "ff": 0.83,
    "fum_rec": 0.57, "gp": 1.0, "int": 0.77, "pass_int_td": 0.13, "pr_yd": 17.23, "pts_allow": 20.5,
    "pts_allow_14_20": 1.0, "pts_ppr": 8.53, "sack": 2.74, "tkl_loss": 5.42, "yds_allow": 333.15,
    "yds_allow_300_349": 1.0,
}
DET_DEF = {
    "blk_kick": 0.06, "def_fum_td": 0.06, "def_kr_td": 0.06, "def_kr_yd": 103.5, "def_pr_td": 0.06,
    "def_pr_yd": 119.33, "def_td": 0.12, "ff": 0.67, "fum_rec": 0.67, "gp": 1.0, "int": 0.79,
    "pass_int_td": 0.06, "pr_yd": 15.83, "pts_allow": 21.5, "pts_allow_21_27": 1.0, "pts_ppr": 7.37,
    "sack": 2.56, "st_td": 0.06, "tkl_loss": 4.57, "yds_allow": 343.94, "yds_allow_300_349": 1.0,
}
LITTLE = {
    "fga": 2.32, "fgm": 2.0, "fgm_20_29": 0.31, "fgm_30_39": 0.46, "fgm_40_49": 0.41, "fgm_yds": 46.96,
    "fgmiss_30_39": 0.04, "fgmiss_40_49": 0.04, "gp": 1.0, "pts_ppr": 6.52, "xpa": 2.83, "xpm": 2.71,
    "xpmiss": 0.13,
}


def _entry(first: str, last: str, position: str, team: str, stats: dict) -> dict:
    return {
        "company": "rotowire",
        "player": {"first_name": first, "last_name": last, "position": position, "team": team},
        "stats": stats,
    }


CASES = [
    (_entry("Trevor", "Lawrence", "QB", "JAX", LAWRENCE), 18.91),
    (_entry("Kansas City", "Chiefs", "DEF", "KC", KC_DEF), 8.51),
    (_entry("Detroit", "Lions", "DEF", "DET", DET_DEF), 7.35),
    (_entry("Cam", "Little", "K", "JAX", LITTLE), 6.53),
]


class TestScoreSleeperStats:
    @pytest.mark.parametrize("entry,expected", CASES, ids=["QB", "KC DEF", "DET DEF", "K"])
    def test_reproduces_sleepers_number(self, entry, expected):
        assert score_sleeper_stats(entry["stats"], SETTINGS) == pytest.approx(expected, abs=0.01)

    def test_ignores_keys_either_side_lacks_and_non_numbers(self):
        assert score_sleeper_stats({"pass_td": 1.0, "cmp_pct": 60.0, "x": "junk"}, {"pass_td": 4.0, "rec": 1.0}) == 4.0


class TestApplyLeagueScoringPrefersSleeperArithmetic:
    @pytest.mark.parametrize("entry,expected", CASES, ids=["QB", "KC DEF", "DET DEF", "K"])
    def test_row_scores_to_sleepers_number(self, entry, expected):
        rows = [_row_from_entry(entry)]
        apply_league_scoring(rows, LeagueScoring(sleeper_scoring_settings=SETTINGS))
        assert rows[0]["points"] == pytest.approx(expected, abs=0.01)
        assert rows[0]["points_flags"] == ("sleeper_exact",)

    def test_without_settings_the_statline_path_still_runs(self):
        rows = [_row_from_entry(CASES[0][0])]
        apply_league_scoring(rows, LeagueScoring())
        assert "sleeper_exact" not in rows[0]["points_flags"]

    def test_settings_load_from_league_yml(self):
        league = LeagueScoring.from_dict({"sleeper_scoring_settings": {"rec": 1, "pass_td": 4.0, "flag": True}})
        assert league.sleeper_scoring_settings == {"rec": 1.0, "pass_td": 4.0}


class TestStatLineFallbackFollowsSleeper:
    """With no settings, the StatLine route must land near Sleeper's number
    rather than on the two measured bugs."""

    def _league(self) -> LeagueScoring:
        return LeagueScoring.from_dict({
            "kicking": {
                "fg_by_distance": [
                    {"min": 0, "max": 19, "points": 3.0}, {"min": 20, "max": 29, "points": 3.0},
                    {"min": 30, "max": 39, "points": 3.0}, {"min": 40, "max": 49, "points": 4.0},
                    {"min": 50, "max": 59, "points": 5.0}, {"min": 60, "max": 99, "points": 6.0},
                ],
                "fg_distance_mix": {"0-19": 0.06, "20-29": 0.24, "30-39": 0.28, "40-49": 0.27, "50-59": 0.13, "60-99": 0.02},
                "pat_made": 1.0, "pat_missed": -1.0, "fg_missed": -1.0,
            },
            "defense": {
                "points_allowed": [
                    {"max": 0, "points": 10.0}, {"max": 6, "points": 7.0}, {"max": 13, "points": 4.0},
                    {"max": 20, "points": 1.0}, {"max": 27, "points": 0.0}, {"max": 34, "points": -1.0},
                    {"max": 999, "points": -4.0},
                ],
                "sack": 1.0, "interception": 2.0, "fumble_recovery": 2.0, "forced_fumble": 1.0,
                "safety": 2.0, "block_kick": 2.0, "touchdown": 6.0, "special_teams_td": 6.0,
            },
        })

    def test_kicker_is_scored_by_distance_band_not_the_league_mix(self):
        stats = _row_from_entry(CASES[3][0])["stats"]
        points, flags = score_statline(stats, "K", self._league())
        assert points == pytest.approx(6.53, abs=0.5)  # was 9.44
        assert "fg_distance_estimated" not in flags

    def test_fractional_points_allowed_lands_in_sleepers_bucket(self):
        stats = _row_from_entry(CASES[1][0])["stats"]
        points, _ = score_statline(stats, "DEF", self._league())
        assert points == pytest.approx(8.51, abs=0.05)  # was 7.51: 20.5 fell into the 21-27 tier
