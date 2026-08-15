from __future__ import annotations

import warnings

from ffbot.sleeper.scoring_import import (
    league_dict_from_sleeper_scoring,
    roster_positions_from_sleeper,
    scoring_drift,
)


class TestLeagueDictFromSleeperScoring:
    def test_passing_yards_reciprocal(self):
        league, unmapped = league_dict_from_sleeper_scoring({"pass_yd": 0.04})
        assert league["passing"]["yards_per_point"] == 25.0
        assert unmapped == []

    def test_direct_fields_copy_across(self):
        league, _ = league_dict_from_sleeper_scoring(
            {"pass_td": 4, "pass_int": -2, "rec": 1.0, "fum_lost": -2}
        )
        assert league["passing"]["td"] == 4
        assert league["passing"]["int"] == -2
        assert league["receiving"]["reception"] == 1.0
        assert league["misc"]["fumble_lost"] == -2

    def test_defensive_int_distinct_from_passing_int(self):
        league, _ = league_dict_from_sleeper_scoring({"pass_int": -2, "int": 2})
        assert league["passing"]["int"] == -2
        assert league["defense"]["interception"] == 2

    def test_fg_by_distance_bands_built_in_order(self):
        league, _ = league_dict_from_sleeper_scoring(
            {"fgm_0_19": 3, "fgm_20_29": 3, "fgm_40_49": 4, "fgm_50p": 5}
        )
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 0, "max": 19, "points": 3} in bands
        assert {"min": 40, "max": 49, "points": 4} in bands
        assert {"min": 50, "max": 99, "points": 5} in bands
        # 30-39 was never supplied, so no band for it
        assert not any(b["min"] == 30 for b in bands)

    def test_fg_missed_by_distance_is_negative_regardless_of_sign(self):
        league, _ = league_dict_from_sleeper_scoring({"fgmiss_0_19": 1})
        assert league["kicking"]["fg_missed_by_distance"] == [{"min": 0, "max": 19, "points": -1}]

    def test_flat_fgmiss_used_when_no_distance_bands(self):
        league, _ = league_dict_from_sleeper_scoring({"fgmiss": 1})
        assert league["kicking"]["fg_missed"] == -1

    def test_points_allowed_ladder(self):
        league, _ = league_dict_from_sleeper_scoring(
            {"pts_allow_0": 10, "pts_allow_35p": -4}
        )
        ladder = league["defense"]["points_allowed"]
        assert {"max": 0, "points": 10} in ladder
        assert {"max": 999, "points": -4} in ladder

    def test_a_real_zero_valued_band_is_kept_not_dropped(self):
        """Regression test: a Sleeper league that genuinely scores a band at
        0 (e.g. 21-27 points allowed = 0) must still produce that band in
        the ladder. Silently dropping it (treating 0 as "not present," the
        rule for ordinary point fields) would leave a gap that the NEXT
        band's `max` wrongly swallows -- caught by round-tripping this
        function against a real live Sleeper league during the public-
        template rollout, where `pts_allow_21_27: 0` vanished and a defense
        allowing 25 points was mispriced under the `max: 34` band instead."""
        league, unmapped = league_dict_from_sleeper_scoring(
            {"pts_allow_0": 10, "pts_allow_21_27": 0, "pts_allow_35p": -4}
        )
        ladder = league["defense"]["points_allowed"]
        assert {"max": 27, "points": 0} in ladder
        assert len(ladder) == 3
        assert "pts_allow_21_27" not in unmapped

    def test_a_real_zero_valued_fg_band_is_kept_not_dropped(self):
        league, unmapped = league_dict_from_sleeper_scoring(
            {"fgm_0_19": 0, "fgm_50p": 5}
        )
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 0, "max": 19, "points": 0} in bands
        assert "fgm_0_19" not in unmapped

    def test_te_premium_bonus_becomes_absolute_reception_by_position(self):
        league, _ = league_dict_from_sleeper_scoring({"rec": 1.0, "bonus_rec_te": 0.5})
        assert league["receiving"]["reception_by_position"] == {"TE": 1.5}

    def test_a_recognized_zero_value_is_written_explicitly_not_dropped(self):
        """A 0 from Sleeper is real information -- the league explicitly
        scores this at zero -- and must be written into the modeled field so
        scoring logic uses the real value, not silently fall through to
        LeagueScoring's own dataclass default (which may not even be 0 --
        e.g. `int` defaults to -2.0)."""
        league, unmapped = league_dict_from_sleeper_scoring({"pass_2pt": 0, "int": 0})
        assert league["passing"]["two_pt"] == 0
        assert league["defense"]["interception"] == 0
        assert unmapped == []

    def test_unrecognized_key_reported_regardless_of_value(self):
        _, unmapped = league_dict_from_sleeper_scoring({"idp_tkl_solo": 1.0, "st_ff": 0})
        assert unmapped == ["idp_tkl_solo", "st_ff"]

    def test_unrecognized_keys_written_as_sleeper_unmapped_placeholder(self):
        """Even a key this function can't model yet gets a permanent home in
        the generated league.yml (as `sleeper_unmapped`), not just a
        stdout-only warning that vanishes after the one-time import run --
        "could theoretically be used" once a field exists for it."""
        league, _ = league_dict_from_sleeper_scoring({"idp_tkl_solo": 1.0, "st_ff": 0})
        assert league["sleeper_unmapped"] == {"idp_tkl_solo": 1.0, "st_ff": 0}

    def test_no_sleeper_unmapped_key_when_nothing_is_unmapped(self):
        league, _ = league_dict_from_sleeper_scoring({"pass_td": 4})
        assert "sleeper_unmapped" not in league

    def test_unexposed_defaults_carried_forward(self):
        league, _ = league_dict_from_sleeper_scoring({})
        assert league["kicking"]["fg_distance_mix"]["30-39"] == 0.28
        assert league["defense"]["points_allowed_stdev"] == 9.5

    def test_name_and_source_pass_through(self):
        league, _ = league_dict_from_sleeper_scoring({}, name="My League", source="Sleeper API")
        assert league["name"] == "My League"
        assert league["source"] == "Sleeper API"

    def test_empty_scoring_produces_no_unmapped_keys(self):
        _, unmapped = league_dict_from_sleeper_scoring({})
        assert unmapped == []


class TestFinerFgBands:
    def test_50_59_and_60_plus_bands_built(self):
        league, unmapped = league_dict_from_sleeper_scoring({"fgm_50_59": 5, "fgm_60p": 6})
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 50, "max": 59, "points": 5} in bands
        assert {"min": 60, "max": 99, "points": 6} in bands
        assert "fgm_50_59" not in unmapped and "fgm_60p" not in unmapped

    def test_fine_bands_use_the_fine_distance_mix(self):
        league, _ = league_dict_from_sleeper_scoring({"fgm_50_59": 5, "fgm_60p": 6})
        mix = league["kicking"]["fg_distance_mix"]
        assert "50-59" in mix and "60-99" in mix
        assert "50-99" not in mix

    def test_coarse_50p_still_uses_the_coarse_distance_mix(self):
        league, _ = league_dict_from_sleeper_scoring({"fgm_50p": 5})
        mix = league["kicking"]["fg_distance_mix"]
        assert "50-99" in mix
        assert "50-59" not in mix

    def test_fallback_50p_used_when_no_finer_split_present(self):
        league, unmapped = league_dict_from_sleeper_scoring({"fgm_50p": 5})
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 50, "max": 99, "points": 5} in bands
        assert "fgm_50p" not in unmapped

    def test_finer_split_wins_when_both_vocabularies_present(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            league, unmapped = league_dict_from_sleeper_scoring(
                {"fgm_50_59": 5, "fgm_60p": 6, "fgm_50p": 99}
            )
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 50, "max": 99, "points": 99} not in bands
        assert {"min": 50, "max": 59, "points": 5} in bands
        assert "fgm_50p" not in unmapped  # dropped, not left unmapped
        assert any("fgm_50p" in str(w.message) for w in caught)

    def test_a_real_zero_valued_finer_band_is_kept(self):
        league, unmapped = league_dict_from_sleeper_scoring({"fgm_50_59": 0, "fgm_60p": 6})
        bands = league["kicking"]["fg_by_distance"]
        assert {"min": 50, "max": 59, "points": 0} in bands
        assert "fgm_50_59" not in unmapped

    def test_miss_bands_mirror_the_same_fallback_logic(self):
        league, unmapped = league_dict_from_sleeper_scoring({"fgmiss_50_59": 1, "fgmiss_60p": 2})
        bands = league["kicking"]["fg_missed_by_distance"]
        assert {"min": 50, "max": 59, "points": -1} in bands
        assert {"min": 60, "max": 99, "points": -2} in bands


class TestNewDirectMapKeys:
    def test_pass_cmp_40p_maps_to_pass_completion_bonus(self):
        league, unmapped = league_dict_from_sleeper_scoring({"pass_cmp_40p": 1.0})
        assert league["bonuses"]["pass_completion_40plus"] == 1.0
        assert unmapped == []

    def test_rush_and_rec_40p_map_to_the_new_per_play_bonuses(self):
        league, _ = league_dict_from_sleeper_scoring({"rush_40p": 2.0, "rec_40p": 2.0})
        assert league["bonuses"]["rush_40plus"] == 2.0
        assert league["bonuses"]["rec_40plus"] == 2.0

    def test_td_distance_bonuses_mapped_for_unmodeled_rules_to_warn_about(self):
        league, _ = league_dict_from_sleeper_scoring(
            {"pass_td_40p": 1.0, "pass_td_50p": 2.0, "rush_td_50p": 2.0, "rec_td_50p": 2.0}
        )
        assert league["bonuses"]["pass_td_40plus"] == 1.0
        assert league["bonuses"]["pass_td_50plus"] == 2.0
        assert league["bonuses"]["rush_td_50plus"] == 2.0
        assert league["bonuses"]["rec_td_50plus"] == 2.0

    def test_def_3_and_out_maps_to_three_and_outs(self):
        league, unmapped = league_dict_from_sleeper_scoring({"def_3_and_out": 2.0})
        assert league["defense"]["three_and_outs"] == 2.0
        assert unmapped == []

    def test_special_teams_only_keys_stay_unmapped(self):
        # Deliberately not guessed at -- see the module's own reasoning for
        # leaving st_ff/st_fum_rec/st_td/def_st_ff/def_st_fum_rec unmapped
        # rather than risk double-counting against def_td/ff/fum_rec.
        _, unmapped = league_dict_from_sleeper_scoring(
            {"st_ff": 1.0, "st_fum_rec": 1.0, "st_td": 6.0, "def_st_ff": 1.0, "def_st_fum_rec": 1.0}
        )
        assert set(unmapped) == {"st_ff", "st_fum_rec", "st_td", "def_st_ff", "def_st_fum_rec"}


class TestScoringDrift:
    def test_no_drift_when_settings_match(self):
        raw = {"passing": {"td": 4.0}}
        lines = scoring_drift(raw, {"pass_td": 4.0})
        assert lines == []

    def test_changed_value_reported(self):
        raw = {"defense": {"forced_fumble": 0.0}}
        lines = scoring_drift(raw, {"ff": 1.0})
        assert any("defense.forced_fumble" in line and "0.0" in line and "1.0" in line for line in lines)

    def test_new_live_key_not_in_league_yaml_reported(self):
        raw = {"passing": {"td": 4.0}}
        lines = scoring_drift(raw, {"pass_td": 4.0, "int": 2.0})
        assert any("defense.interception" in line for line in lines)

    def test_unexposed_estimate_fields_never_flagged(self):
        # fg_distance_mix/points_allowed_stdev are carried-forward estimates
        # with no live source at all -- comparing them would only ever
        # compare this importer's own default against itself.
        raw = {"kicking": {"fg_distance_mix": {"0-19": 0.99}}, "defense": {"points_allowed_stdev": 1.0}}
        lines = scoring_drift(raw, {"fgm_0_19": 3})
        assert not any("fg_distance_mix" in line or "points_allowed_stdev" in line for line in lines)

    def test_new_unmapped_key_reported_once_not_already_known(self):
        raw = {"sleeper_unmapped": {"idp_tkl_solo": 1.0}}
        lines = scoring_drift(raw, {"idp_tkl_solo": 1.0, "idp_tkl_ast": 0.5})
        assert any("idp_tkl_ast" in line for line in lines)
        assert not any(
            "idp_tkl_solo" in line and "not seen" in line for line in lines
        )

    def test_band_ladder_order_does_not_cause_false_drift(self):
        raw = {"defense": {"points_allowed": [
            {"max": 999, "points": -4}, {"max": 0, "points": 10},
        ]}}
        lines = scoring_drift(raw, {"pts_allow_0": 10, "pts_allow_35p": -4})
        assert lines == []

    def test_no_drift_at_all_returns_empty_list(self):
        assert scoring_drift({}, {}) == []


class TestRosterPositionsFromSleeper:
    def test_counts_each_slot(self):
        positions = ["QB", "RB", "RB", "WR", "WR", "FLEX", "K", "DEF", "BN", "BN", "BN"]
        assert roster_positions_from_sleeper(positions) == {
            "QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "K": 1, "DEF": 1, "BN": 3,
        }

    def test_flex_names_pass_through_unchanged(self):
        counts = roster_positions_from_sleeper(["SUPER_FLEX", "WRRB_FLEX", "REC_FLEX"])
        assert counts == {"SUPER_FLEX": 1, "WRRB_FLEX": 1, "REC_FLEX": 1}

    def test_reserve_slots_added_as_ir_when_present(self):
        counts = roster_positions_from_sleeper(["QB", "BN"], reserve_slots=2)
        assert counts["IR"] == 2

    def test_no_ir_key_when_reserve_slots_zero(self):
        counts = roster_positions_from_sleeper(["QB", "BN"], reserve_slots=0)
        assert "IR" not in counts
