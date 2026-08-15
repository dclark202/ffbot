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

    def test_new_bonus_fields_flagged(self):
        from ffbot.config import BonusScoring

        league = LeagueScoring(
            bonuses=BonusScoring(
                rush_40plus=2.0, rec_40plus=2.0,
                pass_td_40plus=1.0, pass_td_50plus=2.0,
                rush_td_50plus=2.0, rec_td_50plus=2.0,
            )
        )
        rules = unmodeled_rules(league)
        assert any("40+ yard rush plays" in r for r in rules)
        assert any("40+ yard reception plays" in r for r in rules)
        assert any("40+ yard passing TDs" in r for r in rules)
        assert any("50+ yard passing TDs" in r for r in rules)
        assert any("50+ yard rushing TDs" in r for r in rules)
        assert any("50+ yard receiving TDs" in r for r in rules)

    def test_three_and_outs_still_flagged(self):
        from ffbot.config import DefenseScoring

        league = LeagueScoring(defense=DefenseScoring(three_and_outs=2.0))
        rules = unmodeled_rules(league)
        assert any("three-and-outs" in r for r in rules)

    def test_unknown_source_raises(self):
        import pytest

        with pytest.raises(ValueError, match="unmodeled_rules"):
            unmodeled_rules(LeagueScoring(), source="yahoo")

    def test_sleeper_weekly_clears_live_modeled_rules(self):
        # These rules are all genuinely projected by Sleeper's live weekly
        # feed (verified live -- see ffbot/projections/sleeper.py::
        # _stat_line) even though a FantasyPros CSV can never carry them.
        # Reporting them as unmodeled under the shipped sleeper-live default
        # was the exact bug this source-awareness fixes.
        from ffbot.config import BonusScoring, DefenseScoring, KickingScoring

        league = LeagueScoring(
            bonuses=BonusScoring(pass_completion_40plus=2.0, rush_40plus=2.0, rec_40plus=2.0),
            receiving=ReceivingScoring(two_pt=2.0),
            kicking=KickingScoring(pat_missed=-1.0),
            defense=DefenseScoring(block_kick=2.0, special_teams_td=6.0),
        )
        rules = unmodeled_rules(league, source="sleeper_weekly")
        assert rules == []

    def test_csv_still_flags_live_modeled_rules(self):
        # The same league as above, checked against the CSV path: a
        # FantasyPros export genuinely cannot express any of these, so they
        # must still be flagged there.
        from ffbot.config import BonusScoring, DefenseScoring, KickingScoring

        league = LeagueScoring(
            bonuses=BonusScoring(pass_completion_40plus=2.0, rush_40plus=2.0, rec_40plus=2.0),
            receiving=ReceivingScoring(two_pt=2.0),
            kicking=KickingScoring(pat_missed=-1.0),
            defense=DefenseScoring(block_kick=2.0, special_teams_td=6.0),
        )
        rules = unmodeled_rules(league, source="csv")
        # 3 40+-play bonuses + 2-point conversions (one combined line) +
        # missed PATs + blocked kicks + special-teams TDs.
        assert len(rules) == 7
        assert "export has makes only, no attempts" in next(r for r in rules if "missed PATs" in r)

    def test_genuinely_unmodelable_rules_survive_every_source(self):
        from ffbot.config import BonusScoring, DefenseScoring, MiscScoring

        league = LeagueScoring(
            bonuses=BonusScoring(rush_td_40plus=5.0),
            misc=MiscScoring(off_fumble_return_td=6.0),
            defense=DefenseScoring(three_and_outs=2.0, extra_point_returned=2.0),
        )
        for source in ("csv", "sleeper_weekly", "sleeper_season"):
            rules = unmodeled_rules(league, source=source)
            assert any("40+ yard rushing TDs" in r for r in rules)
            assert any("offensive fumble return" in r for r in rules)
            assert any("three-and-outs" in r for r in rules)
            assert any("extra points returned" in r for r in rules)

    def test_sleeper_season_weaker_than_weekly_for_40plus_play_bonuses(self):
        # The season endpoint's per-position coverage of the 40+-play bonus
        # counts is inconsistent (verified live -- see
        # _season_offense_stat_line's own docstring), so these stay flagged
        # under "sleeper_season" even though "sleeper_weekly" clears them.
        from ffbot.config import BonusScoring

        league = LeagueScoring(bonuses=BonusScoring(pass_completion_40plus=2.0, rush_40plus=2.0, rec_40plus=2.0))
        assert unmodeled_rules(league, source="sleeper_weekly") == []
        season_rules = unmodeled_rules(league, source="sleeper_season")
        assert any("40+ yard completions" in r for r in season_rules)
        assert any("40+ yard rush plays" in r for r in season_rules)
        assert any("40+ yard reception plays" in r for r in season_rules)

    def test_fg_missed_by_distance_flagged_on_every_source_with_distinct_reasons(self):
        # Deliberately unused even though Sleeper's live feed carries partial
        # miss-distance bands (fgmiss_20_29/30_39/40_49) -- they don't cover
        # the full ladder (no 0-19/50+ split), so feeding them in would risk
        # silently undercounting a kicker who misses outside that range. See
        # ffbot/projections/sleeper.py's module docstring for the identical
        # reasoning already applied to made-FG bands.
        league = LeagueScoring(
            kicking=KickingScoring(fg_missed_by_distance=[DistanceBand(0, 19, -1)])
        )
        csv_rules = unmodeled_rules(league, source="csv")
        sleeper_rules = unmodeled_rules(league, source="sleeper_weekly")
        assert any("no distance split on misses" in r for r in csv_rules)
        assert any("don't cover the full ladder" in r for r in sleeper_rules)


class TestHistoricalReplayFields:
    """The additive `StatLine` fields `ffbot/history/actuals.py` populates
    from real box scores — never touched by a FantasyPros-sourced StatLine,
    so every test above stays bit-identical (see the full-suite run in the
    PR/commit this landed in)."""

    def test_points_allowed_game_exact_no_flag(self):
        tiers = [Tier(0, 10), Tier(6, 7), Tier(13, 4), Tier(999, -4)]
        league = LeagueScoring(defense=DefenseScoring(points_allowed=tiers, points_allowed_stdev=9.5))
        # A real single game: 10 points allowed -> falls in (6, 13] -> 4 pts.
        # No distribution to integrate over, so no pa_distribution_estimated
        # flag even though points_allowed_stdev is set.
        stats = StatLine(points_allowed_game=10.0)
        pts, flags = score_statline(stats, "DEF", league)
        assert pts == 4.0
        assert flags == ()

    def test_points_allowed_game_wins_over_season(self):
        tiers = [Tier(0, 10), Tier(999, -4)]
        league = LeagueScoring(defense=DefenseScoring(points_allowed=tiers))
        stats = StatLine(points_allowed_game=0.0, points_allowed_season=999.0)
        pts, _ = score_statline(stats, "DEF", league)
        assert pts == 10.0  # used the exact game value, not the season estimate

    def test_fg_made_bands_exact_no_estimated_flag(self):
        league = LeagueScoring(
            kicking=KickingScoring(
                fg_by_distance=[
                    DistanceBand(0, 39, 3), DistanceBand(40, 49, 4), DistanceBand(50, 99, 5),
                ]
            )
        )
        # 2 short makes, 1 in the 40-49 band, 1 50+.
        stats = StatLine(fg_made_bands={"0-19": 1.0, "20-29": 1.0, "40-49": 1.0, "50-59": 1.0})
        pts, flags = score_statline(stats, "K", league)
        assert pts == 3 + 3 + 4 + 5
        assert flags == ()

    def test_fg_made_bands_ignored_without_league_ladder(self):
        # A league with only a flat fg_made falls back to the old fg_made
        # count/estimate path, even if banded data happens to be present.
        league = LeagueScoring(kicking=KickingScoring(fg_made=3.0))
        stats = StatLine(fg_made=2.0, fg_made_bands={"0-19": 1.0, "40-49": 1.0})
        pts, flags = score_statline(stats, "K", league)
        assert pts == 6.0
        assert flags == ()

    def test_fg_missed_bands_scored_when_league_configures_distance_misses(self):
        league = LeagueScoring(
            kicking=KickingScoring(
                fg_missed=0.0,
                fg_missed_by_distance=[DistanceBand(0, 49, -1), DistanceBand(50, 99, 0)],
            )
        )
        stats = StatLine(fg_missed_bands={"40-49": 2.0, "50-59": 1.0})
        pts, _ = score_statline(stats, "K", league)
        assert pts == -2.0  # two 40-49 misses at -1 each; the 50+ miss is worth 0

    def test_pat_missed_scored(self):
        league = LeagueScoring(kicking=KickingScoring(pat_made=1.0, pat_missed=-1.0))
        stats = StatLine(pat_made=3.0, pat_missed=1.0)
        pts, _ = score_statline(stats, "K", league)
        assert pts == 3.0 - 1.0

    def test_two_pt_conversions_split_by_type(self):
        league = LeagueScoring(
            passing=PassingScoring(two_pt=2.0),
            rushing=RushingScoring(two_pt=2.0),
            receiving=ReceivingScoring(two_pt=2.0),
        )
        stats = StatLine(pass_2pt=1.0, rush_2pt=1.0, rec_2pt=1.0)
        pts, _ = score_statline(stats, "RB", league)
        assert pts == 6.0

    def test_pass_completion_40plus_bonus(self):
        from ffbot.config import BonusScoring

        league = LeagueScoring(bonuses=BonusScoring(pass_completion_40plus=1.0))
        stats = StatLine(pass_completion_40plus=3.0)
        pts, _ = score_statline(stats, "QB", league)
        assert pts == 3.0

    def test_none_new_fields_are_bit_identical_to_before(self):
        # A StatLine that only sets pre-existing fields must score exactly
        # as it did before this StatLine grew new optional fields.
        stats = StatLine(rush_yds=100, rush_td=1)
        league = LeagueScoring()
        pts, flags = score_statline(stats, "RB", league)
        assert pts == 100 / 10 + 6
        assert flags == ()

    def test_rush_40plus_bonus(self):
        from ffbot.config import BonusScoring

        league = LeagueScoring(bonuses=BonusScoring(rush_40plus=2.0))
        stats = StatLine(rush_yds=50, rush_40plus=3.0)
        pts, flags = score_statline(stats, "RB", league)
        assert pts == 50 / 10 + 3.0 * 2.0
        assert flags == ()

    def test_rec_40plus_bonus(self):
        from ffbot.config import BonusScoring

        league = LeagueScoring(bonuses=BonusScoring(rec_40plus=2.0))
        stats = StatLine(rec_yds=50, rec_40plus=2.0)
        pts, _ = score_statline(stats, "WR", league)
        assert pts == 50 / 10 + 2.0 * 2.0

    def test_rush_and_rec_40plus_are_zero_impact_by_default(self):
        # Bare LeagueScoring() defaults these to 0.0 -- a StatLine that sets
        # rush_40plus/rec_40plus contributes nothing unless the league
        # actually pays for it.
        league = LeagueScoring()
        stats = StatLine(rush_40plus=5.0, rec_40plus=5.0, rush_yds=0)
        pts, _ = score_statline(stats, "RB", league)
        assert pts == 0.0

    def test_yards_allowed_game_exact_no_flag(self):
        from ffbot.config import DefenseScoring, Tier

        tiers = [Tier(100, 5), Tier(300, 0), Tier(999, -5)]
        league = LeagueScoring(defense=DefenseScoring(yards_allowed=tiers))
        stats = StatLine(yards_allowed_game=250.0)
        pts, flags = score_statline(stats, "DEF", league)
        assert pts == 0.0  # falls in (100, 300] -> 0
        assert flags == ()

    def test_yards_allowed_game_wins_over_season(self):
        from ffbot.config import DefenseScoring, Tier

        tiers = [Tier(100, 5), Tier(999, -5)]
        league = LeagueScoring(defense=DefenseScoring(yards_allowed=tiers))
        stats = StatLine(yards_allowed_game=50.0, yards_allowed_season=99999.0)
        pts, _ = score_statline(stats, "DEF", league)
        assert pts == 5.0  # used the exact game value, not the season estimate

    def test_yards_allowed_season_estimated_and_flagged(self):
        from ffbot.config import DefenseScoring, Tier

        tiers = [Tier(200, 5), Tier(999, -5)]
        league = LeagueScoring(defense=DefenseScoring(yards_allowed=tiers), games_per_season=10)
        stats = StatLine(yards_allowed_season=1000.0)  # 100/game -> <= 200 -> 5
        pts, flags = score_statline(stats, "DEF", league)
        assert pts == 5.0
        assert "ya_point_estimate" in flags

    def test_yards_allowed_not_scored_when_league_has_no_ladder(self):
        league = LeagueScoring()  # defense.yards_allowed defaults to []
        stats = StatLine(yards_allowed_game=250.0, yards_allowed_season=3000.0)
        pts, flags = score_statline(stats, "DEF", league)
        assert pts == 0.0
        assert flags == ()

    def test_block_kick_scored(self):
        from ffbot.config import DefenseScoring

        league = LeagueScoring(defense=DefenseScoring(block_kick=2.0))
        stats = StatLine(block_kick=1.0)
        pts, flags = score_statline(stats, "DEF", league)
        assert pts == 2.0
        assert flags == ()

    def test_special_teams_td_additive_with_def_td(self):
        # Sleeper scores a defensive/return TD (def_td -> touchdown) and a
        # kick/punt-return TD (st_td -> special_teams_td) as two SEPARATE
        # rule categories -- a league that sets both must get both, summed,
        # not one overwriting the other.
        from ffbot.config import DefenseScoring

        league = LeagueScoring(defense=DefenseScoring(touchdown=6.0, special_teams_td=6.0))
        stats = StatLine(def_td=1.0, special_teams_td=1.0)
        pts, _ = score_statline(stats, "DEF", league)
        assert pts == 12.0

    def test_block_kick_and_special_teams_td_zero_impact_by_default(self):
        league = LeagueScoring()  # both default to 0.0
        stats = StatLine(block_kick=3.0, special_teams_td=2.0)
        pts, _ = score_statline(stats, "DEF", league)
        assert pts == 0.0
