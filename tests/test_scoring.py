from __future__ import annotations

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
from ffbot.scoring import (
    StatLine,
    _fg_value_per_kick,
    _points_allowed_per_game,
    score_statline,
    unmodeled_rules,
)


class TestScoreStatlineOffense:
    def test_ppr_standard_half(self):
        stats = StatLine(rec=10, rec_yds=100, rec_td=1)
        ppr = LeagueScoring(receiving=ReceivingScoring(reception=1.0, td=6, yards_per_point=10))
        half = LeagueScoring(receiving=ReceivingScoring(reception=0.5, td=6, yards_per_point=10))
        standard = LeagueScoring(receiving=ReceivingScoring(reception=0.0, td=6, yards_per_point=10))

        ppr_pts, _ = score_statline(stats, "WR", ppr)
        half_pts, _ = score_statline(stats, "WR", half)
        std_pts, _ = score_statline(stats, "WR", standard)

        assert ppr_pts == 10 + 10 + 6  # 10 rec + 10 yds/10 + 6 TD
        assert half_pts == 5 + 10 + 6
        assert std_pts == 0 + 10 + 6
        assert ppr_pts > half_pts > std_pts

    def test_reception_by_position_override(self):
        stats = StatLine(rec=5, rec_yds=0, rec_td=0)
        league = LeagueScoring(
            receiving=ReceivingScoring(reception=0.5, reception_by_position={"TE": 1.5})
        )
        te_pts, _ = score_statline(stats, "TE", league)
        wr_pts, _ = score_statline(stats, "WR", league)
        assert te_pts == 5 * 1.5
        assert wr_pts == 5 * 0.5

    def test_pass_int_minus_two_vs_minus_one(self):
        stats = StatLine(pass_yds=3814, pass_td=27, pass_int=11)
        minus_one = LeagueScoring(passing=PassingScoring(yards_per_point=25, td=4, int=-1))
        minus_two = LeagueScoring(passing=PassingScoring(yards_per_point=25, td=4, int=-2))
        pts1, _ = score_statline(stats, "QB", minus_one)
        pts2, _ = score_statline(stats, "QB", minus_two)
        # -2 vs -1 on 11 INTs is an 11-point swing.
        assert round(pts1 - pts2, 2) == 11.0

    def test_missing_stat_contributes_nothing(self):
        stats = StatLine(rush_yds=100)  # no rush_td, no fumbles
        league = LeagueScoring()
        pts, _ = score_statline(stats, "RB", league)
        assert pts == 10.0  # 100 / 10 yards-per-point, nothing else


class TestFieldGoalValue:
    def test_flat_when_no_distance_ladder(self):
        league = LeagueScoring(kicking=KickingScoring(fg_made=3.0))
        value, flags = _fg_value_per_kick(league)
        assert value == 3.0
        assert flags == ()

    def test_distance_ladder_with_mix_is_estimated(self):
        league = LeagueScoring(
            kicking=KickingScoring(
                fg_by_distance=[
                    DistanceBand(0, 39, 3), DistanceBand(40, 49, 4), DistanceBand(50, 99, 5),
                ],
                fg_distance_mix={"0-39": 0.58, "40-49": 0.27, "50-99": 0.15},
            )
        )
        value, flags = _fg_value_per_kick(league)
        expected = 0.58 * 3 + 0.27 * 4 + 0.15 * 5
        assert round(value, 4) == round(expected, 4)
        assert flags == ("fg_distance_estimated",)

    def test_distance_ladder_without_mix_falls_back_to_flat_average(self):
        league = LeagueScoring(
            kicking=KickingScoring(
                fg_by_distance=[DistanceBand(0, 39, 3), DistanceBand(40, 99, 5)],
                fg_distance_mix={},
            )
        )
        value, flags = _fg_value_per_kick(league)
        assert value == 4.0  # flat average of 3 and 5
        assert flags == ("fg_distance_estimated",)

    def test_end_to_end_kicker_scoring(self):
        stats = StatLine(fg_made=35.2, fg_att=39.9, pat_made=47.0)
        league = LeagueScoring(
            kicking=KickingScoring(
                pat_made=1.0,
                fg_by_distance=[
                    DistanceBand(0, 39, 3), DistanceBand(40, 49, 4), DistanceBand(50, 99, 5),
                ],
                fg_distance_mix={"0-39": 0.58, "40-49": 0.27, "50-99": 0.15},
            )
        )
        pts, flags = score_statline(stats, "K", league)
        per_fg = 0.58 * 3 + 0.27 * 4 + 0.15 * 5
        assert round(pts, 2) == round(35.2 * per_fg + 47.0, 2)
        assert "fg_distance_estimated" in flags


class TestPointsAllowed:
    def test_point_estimate_when_stdev_zero(self):
        tiers = [Tier(0, 10), Tier(6, 7), Tier(999, -4)]
        # mean = 17/17 = 1.0 pt/game -> first tier whose max it's <= is (max=6, 7)
        per_game = _points_allowed_per_game(season_total=17.0, games=17, tiers=tiers, stdev=0.0)
        assert per_game == 7

    def test_monotone_in_points_allowed(self):
        tiers = [
            Tier(0, 10), Tier(6, 7), Tier(13, 4), Tier(20, 1),
            Tier(27, 0), Tier(34, -1), Tier(999, -4),
        ]
        low = _points_allowed_per_game(season_total=17 * 5, games=17, tiers=tiers, stdev=5.0)
        high = _points_allowed_per_game(season_total=17 * 30, games=17, tiers=tiers, stdev=5.0)
        assert low > high  # fewer points allowed scores more

    def test_distribution_vs_point_estimate_houston_example(self):
        tiers = [
            Tier(0, 10), Tier(6, 7), Tier(13, 4), Tier(20, 1),
            Tier(27, 0), Tier(34, -1), Tier(999, -4),
        ]
        point_estimate = _points_allowed_per_game(322.0, 17, tiers, stdev=0.0)
        distribution = _points_allowed_per_game(322.0, 17, tiers, stdev=9.5)
        assert point_estimate == 1.0  # 322/17 = 18.94 -> falls in (13, 20] band -> 1
        # The distribution correctly earns some 7s/10s while eating some -1s;
        # for this specific mean/stdev it comes out higher than the flat
        # point estimate.
        assert distribution > point_estimate

    def test_zero_games_or_no_tiers_is_zero(self):
        assert _points_allowed_per_game(100.0, 0, [Tier(999, -4)], stdev=5.0) == 0.0
        assert _points_allowed_per_game(100.0, 17, [], stdev=5.0) == 0.0

    def test_end_to_end_def_scoring_matches_reconciled_default(self):
        stats = StatLine(
            sack=48.8, interception=14.8, fumble_recovery=11.6, forced_fumble=18.3,
            def_td=2.8, safety=1.0, points_allowed_season=322.0,
        )
        fp_default = LeagueScoring.fantasypros_default()
        pts, flags = score_statline(stats, "DEF", fp_default)
        # FantasyPros' own export for this exact line is 120.4 and scores
        # points allowed at zero (fp_default.defense.points_allowed == []).
        assert round(pts, 1) == 120.4
        assert flags == ()

    def test_end_to_end_def_scoring_with_pa_tiers(self):
        stats = StatLine(
            sack=48.8, interception=14.8, fumble_recovery=11.6, forced_fumble=18.3,
            def_td=2.8, safety=1.0, points_allowed_season=322.0,
        )
        league = LeagueScoring(
            defense=DefenseScoring(
                sack=1.0, interception=2.0, fumble_recovery=2.0, forced_fumble=0.0,
                touchdown=6.0, safety=2.0,
                points_allowed=[
                    Tier(0, 10), Tier(6, 7), Tier(13, 4), Tier(20, 1),
                    Tier(27, 0), Tier(34, -1), Tier(999, -4),
                ],
                points_allowed_stdev=9.5,
            )
        )
        pts, flags = score_statline(stats, "DEF", league)
        # Non-PA components reproduce the FantasyPros baseline (120.4); PA
        # then adds real points on top, since this league scores it and
        # FantasyPros' own export does not.
        assert pts > 120.4
        assert "pa_distribution_estimated" in flags


class TestUnmodeledRules:
    def test_fully_zeroed_league_has_no_unmodeled_rules(self):
        # A LeagueScoring with every "no export column" field explicitly
        # zeroed has nothing to warn about. (The bare LeagueScoring()
        # dataclass default is NOT this — its generic field defaults
        # deliberately mirror a common Yahoo league, which includes a
        # nonzero two_pt, so it does have unmodeled rules; see
        # test_two_pt_flagged_when_any_of_three_blocks_set below.)
        league = LeagueScoring(
            passing=PassingScoring(two_pt=0),
            rushing=RushingScoring(two_pt=0),
            receiving=ReceivingScoring(two_pt=0),
        )
        assert unmodeled_rules(league) == []

    def test_bonuses_and_misc_flagged(self):
        from ffbot.config import BonusScoring, MiscScoring

        league = LeagueScoring(
            bonuses=BonusScoring(rush_td_40plus=5.0),
            misc=MiscScoring(off_fumble_return_td=6.0),
        )
        rules = unmodeled_rules(league)
        assert any("40+ yard rushing TDs" in r for r in rules)
        assert any("offensive fumble return" in r for r in rules)

    def test_two_pt_flagged_when_any_of_three_blocks_set(self):
        league = LeagueScoring(receiving=ReceivingScoring(two_pt=2.0))
        rules = unmodeled_rules(league)
        assert any("2-point conversions" in r for r in rules)
