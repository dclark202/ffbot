from __future__ import annotations

import warnings

import pytest

from ffbot.config import (
    DRAFT_SPICE_PRESETS,
    SPICE_PRESETS,
    Config,
    ConfigError,
    DraftConfig,
    LeagueRostersSourceConfig,
    LeagueScoring,
    NotifyConfig,
    ProjectionConfig,
    ProjectionSourceConfig,
    ScoringConfig,
    SeasonConfig,
    SleeperConfig,
    TeamStanding,
    _coerce_block,
    _draft_from_dict,
)


class TestDefaults:
    def test_bare_config_has_no_league(self):
        cfg = Config()
        assert cfg.league is None
        assert cfg.league_file == ""

    def test_bare_config_from_dict_matches_defaults(self):
        # No real config.yml touched -- this is what keeps the whole test
        # suite hermetic (see tests/conftest.py's `cfg` fixture). `season` is
        # excluded from this comparison on purpose: `SeasonConfig.from_dict`
        # always resolves the spice-level preset (even absent an explicit
        # `spice_level` key it defaults to level 3's preset weights), while
        # the bare `Config()` dataclass default is `SeasonConfig()`'s own
        # zeroed field defaults — a pre-existing divergence, not one this
        # phase introduces or is responsible for reconciling.
        empty = Config.from_dict({})
        bare = Config()
        assert empty.sleeper == bare.sleeper
        assert empty.roster_positions == bare.roster_positions
        assert empty.projection == bare.projection
        assert empty.projection_source == bare.projection_source
        assert empty.drops == bare.drops
        assert empty.draft == bare.draft
        assert empty.league == bare.league is None

    def test_scoring_config_alias_still_importable(self):
        assert ScoringConfig is ProjectionConfig


class TestDraftConfigOrder:
    def test_default_is_snake(self):
        assert DraftConfig().order == "snake"


class TestGuiPollSeconds:
    def test_default_is_ten(self):
        assert DraftConfig().gui_poll_seconds == 10

    def test_loaded_from_config_yml_draft_block(self):
        cfg = Config.from_dict({"draft": {"gui_poll_seconds": 3}})
        assert cfg.draft.gui_poll_seconds == 3

    def test_absent_key_keeps_the_default(self):
        cfg = Config.from_dict({"draft": {"order": "linear"}})
        assert cfg.draft.gui_poll_seconds == 10

    def test_distinct_from_sync_poll_seconds(self):
        # gui_poll_seconds (browser-to-server) and sync_poll_seconds
        # (server-to-Sleeper) are two different polling loops -- setting one
        # must not touch the other.
        cfg = Config.from_dict({"draft": {"gui_poll_seconds": 3, "sync_poll_seconds": 20}})
        assert cfg.draft.gui_poll_seconds == 3
        assert cfg.draft.sync_poll_seconds == 20


class TestProjectionSourceConfig:
    def test_default_source_is_board_an_exact_no_op(self):
        # Must stay "board" -- every existing config.yml (real user configs
        # that predate this field) must load with identical behavior.
        assert ProjectionSourceConfig().source == "board"

    def test_from_dict_reads_the_projection_source_block(self):
        cfg = Config.from_dict({"projection_source": {"source": "sleeper", "cache_ttl_minutes": 30}})
        assert cfg.projection_source.source == "sleeper"
        assert cfg.projection_source.cache_ttl_minutes == 30

    def test_distinct_from_the_similarly_named_projection_block(self):
        # projection: (HOW a missing projection is estimated) and
        # projection_source: (WHERE numbers come from) must never collide --
        # setting one must not touch the other.
        cfg = Config.from_dict({
            "projection": {"questionable_multiplier": 0.5},
            "projection_source": {"source": "sleeper"},
        })
        assert cfg.projection.questionable_multiplier == 0.5
        assert cfg.projection_source.source == "sleeper"

    def test_unknown_key_raises_config_error(self):
        with pytest.raises(ConfigError):
            Config.from_dict({"projection_source": {"not_a_real_field": True}})

    def test_linear_accepted(self):
        assert DraftConfig(order="linear").order == "linear"

    def test_invalid_value_raises(self):
        with pytest.raises(ConfigError, match="snake"):
            DraftConfig(order="auction")

    def test_loaded_from_config_yml_draft_block(self):
        cfg = Config.from_dict({"draft": {"order": "linear"}})
        assert cfg.draft.order == "linear"

    def test_invalid_value_from_config_yml_raises(self):
        with pytest.raises(ConfigError):
            Config.from_dict({"draft": {"order": "nonsense"}})


class TestLeagueRostersSourceConfig:
    def test_default_source_is_file_an_exact_no_op(self):
        assert LeagueRostersSourceConfig().source == "file"

    def test_from_dict_reads_the_block(self):
        cfg = Config.from_dict({"league_rosters_source": {"source": "sleeper", "cache_ttl_minutes": 45}})
        assert cfg.league_rosters_source.source == "sleeper"
        assert cfg.league_rosters_source.cache_ttl_minutes == 45

    def test_unknown_key_raises_config_error(self):
        with pytest.raises(ConfigError):
            Config.from_dict({"league_rosters_source": {"not_a_real_field": True}})

    def test_bare_config_default_matches_dataclass_default(self):
        assert Config().league_rosters_source == LeagueRostersSourceConfig()


class TestNotifyConfig:
    def test_default_channel_is_off_an_exact_no_op(self):
        assert NotifyConfig().channel == "off"

    def test_from_dict_reads_the_block(self):
        cfg = Config.from_dict({
            "notify": {"channel": "ntfy", "ntfy_server": "https://ntfy.sh", "ntfy_topic": "my-secret-topic", "min_waiver_net": 2.5},
        })
        assert cfg.notify.channel == "ntfy"
        assert cfg.notify.ntfy_topic == "my-secret-topic"
        assert cfg.notify.min_waiver_net == 2.5

    def test_unknown_key_raises_config_error(self):
        with pytest.raises(ConfigError):
            Config.from_dict({"notify": {"not_a_real_field": True}})

    def test_bare_config_default_matches_dataclass_default(self):
        assert Config().notify == NotifyConfig()


class TestStreamPositions:
    def test_default_is_k_and_def(self):
        assert SeasonConfig().stream_positions == ["K", "DEF"]

    def test_override_survives_spice_level(self):
        cfg = Config.from_dict({"season": {"spice_level": 3, "stream_positions": ["K"]}})
        assert cfg.season.stream_positions == ["K"]
        assert cfg.season.spice_level == 3

    def test_default_present_at_every_spice_level(self):
        for level in SPICE_PRESETS:
            assert SeasonConfig.from_spice_level(level).stream_positions == ["K", "DEF"]


class TestCoerceBlock:
    def test_old_key_used_with_warning(self):
        with pytest.warns(DeprecationWarning, match="renamed"):
            raw = _coerce_block({"scoring": {"questionable_multiplier": 0.5}}, "config.yml", {"scoring": "projection"})
        assert raw == {"projection": {"questionable_multiplier": 0.5}}

    def test_both_keys_raises(self):
        with pytest.raises(ConfigError, match="both"):
            _coerce_block({"scoring": {}, "projection": {}}, "config.yml", {"scoring": "projection"})

    def test_neither_key_present_is_untouched(self):
        raw = _coerce_block({"drops": {}}, "config.yml", {"scoring": "projection"})
        assert raw == {"drops": {}}


class TestConfigFromDictRename:
    def test_old_scoring_key_still_loads(self):
        with pytest.warns(DeprecationWarning):
            cfg = Config.from_dict({"scoring": {"questionable_multiplier": 0.5}})
        assert cfg.projection.questionable_multiplier == 0.5

    def test_new_projection_key_loads_with_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            cfg = Config.from_dict({"projection": {"questionable_multiplier": 0.5}})
        assert cfg.projection.questionable_multiplier == 0.5

    def test_both_scoring_and_projection_raises(self):
        with pytest.raises(ConfigError):
            Config.from_dict({"scoring": {}, "projection": {}})

    def test_unknown_key_in_projection_raises_with_valid_keys_listed(self):
        with pytest.raises(ConfigError, match="questionable_multiplier"):
            Config.from_dict({"projection": {"totally_made_up_key": 1}})


class TestSleeperConfig:
    def test_defaults(self):
        cfg = Config.from_dict({})
        assert cfg.sleeper == SleeperConfig()
        assert cfg.sleeper.league_id == ""
        assert cfg.sleeper.roster_id is None

    def test_nested_block_parses(self):
        cfg = Config.from_dict({"sleeper": {"league_id": "123", "username": "duncan", "roster_id": 4}})
        assert cfg.sleeper.league_id == "123"
        assert cfg.sleeper.username == "duncan"
        assert cfg.sleeper.roster_id == 4

    def test_unknown_key_raises_with_valid_keys_listed(self):
        with pytest.raises(ConfigError, match="league_id"):
            Config.from_dict({"sleeper": {"totally_made_up_key": 1}})

    def test_legacy_top_level_league_id_still_honored_with_warning(self):
        with pytest.warns(DeprecationWarning, match="league_id"):
            cfg = Config.from_dict({"league_id": "legacy-id"})
        assert cfg.sleeper.league_id == "legacy-id"

    def test_legacy_league_id_does_not_override_new_style_key(self):
        # Both set -- the new-style nested key wins outright, no warning
        # needed since there's no ambiguity about which one is "current".
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cfg = Config.from_dict({"league_id": "old", "sleeper": {"league_id": "new"}})
        assert cfg.sleeper.league_id == "new"

    def test_legacy_team_key_warns_and_is_dropped(self):
        with pytest.warns(DeprecationWarning, match="team_key"):
            cfg = Config.from_dict({"team_key": "461.l.123.t.7"})
        assert not hasattr(cfg, "team_key")


class TestConfigLocalOverlay:
    def test_missing_overlay_is_a_noop(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("sleeper:\n  league_id: 'abc'\n", encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.sleeper.league_id == "abc"

    def test_overlay_field_wins_over_base(self, tmp_path):
        (tmp_path / "config.yml").write_text("sleeper:\n  league_id: 'abc'\n", encoding="utf-8")
        (tmp_path / "config.local.yml").write_text("sleeper:\n  league_id: 'xyz'\n", encoding="utf-8")
        cfg = Config.load(tmp_path / "config.yml")
        assert cfg.sleeper.league_id == "xyz"

    def test_overlay_partial_nested_block_does_not_blank_the_rest(self, tmp_path):
        # A local override of just draft.num_teams must not wipe out
        # draft.rounds, which only config.yml set.
        (tmp_path / "config.yml").write_text("draft:\n  num_teams: 12\n  rounds: 15\n", encoding="utf-8")
        (tmp_path / "config.local.yml").write_text("draft:\n  num_teams: 10\n", encoding="utf-8")
        cfg = Config.load(tmp_path / "config.yml")
        assert cfg.draft.num_teams == 10
        assert cfg.draft.rounds == 15

    def test_loading_config_local_yml_directly_does_not_self_merge(self, tmp_path):
        p = tmp_path / "config.local.yml"
        p.write_text("sleeper:\n  league_id: 'solo'\n", encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.sleeper.league_id == "solo"

    def test_no_base_config_but_overlay_present(self, tmp_path):
        (tmp_path / "config.local.yml").write_text("sleeper:\n  league_id: 'only-local'\n", encoding="utf-8")
        cfg = Config.load(tmp_path / "config.yml")
        assert cfg.sleeper.league_id == "only-local"


class TestLeagueFileLoading:
    def test_missing_league_file_stays_none(self, tmp_path):
        cfg = Config.from_dict({"league_file": str(tmp_path / "nope.yml")})
        assert cfg.league is None

    def test_empty_league_file_key_stays_none(self):
        cfg = Config.from_dict({})
        assert cfg.league is None

    def test_real_league_file_loads(self, tmp_path):
        p = tmp_path / "league.yml"
        p.write_text("passing:\n  int: -2\n", encoding="utf-8")
        cfg = Config.from_dict({"league_file": str(p)})
        assert cfg.league is not None
        assert cfg.league.passing.int == -2

    def test_league_scoring_load_empty_path_is_none_not_an_error(self):
        # Regression: Path("") resolves to Path("."), the cwd, which
        # .exists() reports True for -- LeagueScoring.load's own empty-path
        # guard must catch this directly, not just rely on every caller
        # remembering to check truthiness first (Config.from_dict already
        # does, but this is a public classmethod other code can call too).
        assert LeagueScoring.load("") is None


class TestLeagueScoringFromDict:
    def test_unknown_top_block_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"passing": {"nonexistent_field": 1}})

    def test_nested_distance_band_unknown_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"kicking": {"fg_by_distance": [{"min": 0, "max": 39, "pointz": 3}]}})

    def test_full_round_trip(self):
        league = LeagueScoring.from_dict({
            "passing": {"int": -2},
            "kicking": {"fg_by_distance": [{"min": 0, "max": 39, "points": 3}]},
            "defense": {"points_allowed": [{"max": 0, "points": 10}]},
        })
        assert league.passing.int == -2
        assert league.kicking.fg_by_distance[0].points == 3
        assert league.defense.points_allowed[0].points == 10

    def test_waiver_type_rolling_is_silently_accepted(self):
        # "rolling" is the only mode this repo ever modeled -- a league.yml
        # written before FAAB support was removed, or one a user copied from
        # docs, must keep loading with no warning.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            LeagueScoring.from_dict({"waiver_type": "rolling"})

    def test_waiver_type_faab_warns_and_is_ignored(self):
        # FAAB support is gone; the key is no longer even a LeagueScoring
        # field, so silently ignoring it (no strict unknown-key check at
        # this level) would leave a stale config.yml with no clue why
        # nothing changed. See Config.from_dict's own legacy-key precedent
        # for the same "warn, don't fail" contract.
        with pytest.warns(DeprecationWarning, match="waiver_type"):
            league = LeagueScoring.from_dict({"waiver_type": "faab"})
        assert not hasattr(league, "waiver_type")


class TestRosterCapacityWarning:
    def test_matching_capacity_is_silent(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Config.from_dict({
                "roster_positions": {"QB": 1, "BN": 2, "IR": 1},
                "draft": {"rounds": 3},
            })

    def test_mismatched_capacity_warns(self):
        with pytest.warns(UserWarning, match="draft.rounds"):
            Config.from_dict({
                "roster_positions": {"QB": 1, "BN": 2, "IR": 1},
                "draft": {"rounds": 99},
            })

    def test_ir_excluded_from_capacity(self):
        # 1 starter + 2 bench = 3, regardless of how many IR slots exist.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Config.from_dict({
                "roster_positions": {"QB": 1, "BN": 2, "IR": 5},
                "draft": {"rounds": 3},
            })


class TestLeagueScoringStandings:
    def test_default_has_no_teams(self):
        assert LeagueScoring().teams == []

    def test_teams_parsed_from_dict(self):
        league = LeagueScoring.from_dict({
            "playoff_teams": 4,
            "week": 8,
            "my_team": "Test Team's Dynasty",
            "teams": [
                {"name": "Rival A", "record": "6-2", "seed": 1, "waiver_priority": 9},
                {"name": "Rival B", "record": "2-6", "seed": 11, "eliminated": True},
            ],
        })
        assert league.week == 8
        assert league.my_team == "Test Team's Dynasty"
        assert len(league.teams) == 2
        assert league.teams[0] == TeamStanding(name="Rival A", record="6-2", seed=1, waiver_priority=9)
        assert league.teams[1].eliminated is True

    def test_unknown_team_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"teams": [{"name": "X", "not_a_real_field": 1}]})

    def test_team_missing_name_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"teams": [{"record": "6-2"}]})


class TestWeeklySpiceLadder:
    """B7 -- SPICE_PRESETS rescaled to 4 levels (1 Baseline, 2 Tactician,
    3 Sharp, 4 Use at your own risk). The information axis (weather, vegas,
    usage/momentum/divergence trend) still ramps 1->3 unchanged from B5's
    validated level 3; the variance axis (volatility, upside_lean,
    matchup_variance) now ramps 3->4 (B5's old levels 4/5 collapsed into
    one). Structural dials new to this ladder (waiver_value_mode, denial/
    blocking/priority, venue_disruption, kalshi) turn on at specific levels
    per the user's own level semantics -- see docs/dev/SPICE.md for the
    feature-by-level matrix and every backtest number behind these values.
    """

    _VARIANCE_FIELDS = ("volatility_weight", "upside_lean_weight", "matchup_variance_weight")
    _INFO_FIELDS = ("weather_weight", "vegas_weight", "usage_weight", "momentum_weight", "divergence_weight")

    def test_level_one_is_every_weight_at_zero(self):
        # The precondition for "level 1 == control" -- see
        # tests/test_week.py::TestSpiceLevelOneIsControl for the behavioral
        # proof this enables.
        cfg = SeasonConfig.from_spice_level(1)
        for f in self._VARIANCE_FIELDS + self._INFO_FIELDS:
            assert getattr(cfg, f) == 0.0, f

    def test_variance_fields_are_zero_through_level_two(self):
        for level in (1, 2):
            cfg = SeasonConfig.from_spice_level(level)
            for f in self._VARIANCE_FIELDS:
                assert getattr(cfg, f) == 0.0, f"level {level}, {f}"

    def test_variance_fields_climb_monotonically_from_level_three(self):
        for f in self._VARIANCE_FIELDS:
            values = [getattr(SeasonConfig.from_spice_level(lvl), f) for lvl in (3, 4)]
            assert values == sorted(values)
            assert values[0] < values[-1]  # strictly increasing, not flat

    def test_info_fields_climb_monotonically_across_every_level(self):
        for f in self._INFO_FIELDS:
            values = [getattr(SeasonConfig.from_spice_level(lvl), f) for lvl in (1, 2, 3, 4)]
            assert values == sorted(values)

    def test_info_fields_are_zero_through_level_two(self):
        # B7: level 2 ("Tactician") is explicitly defined as NO outside
        # data yet -- weather/vegas/trend all stay off until level 3.
        for level in (1, 2):
            cfg = SeasonConfig.from_spice_level(level)
            for f in self._INFO_FIELDS:
                assert getattr(cfg, f) == 0.0, f"level {level}, {f}"

    def test_level_three_matches_the_validated_b5_cell_unchanged(self):
        # Level 3's info-axis + small variance lean values are IDENTICAL to
        # B5's old validated level 3 -- the one spice-ladder cell in this
        # project's history to clear statistical significance on both train
        # (+0.392, 95% CI [+0.11,+0.68]) and a held-out season (+0.487, CI
        # [+0.11,+0.88]). B7 did not re-tune this cell.
        cfg = SeasonConfig.from_spice_level(3)
        assert cfg.weather_weight == 0.25
        assert cfg.vegas_weight == 0.20
        assert cfg.usage_weight == 0.15
        assert cfg.momentum_weight == 0.15
        assert cfg.divergence_weight == 0.05
        assert cfg.volatility_weight == 0.05
        assert cfg.upside_lean_weight == 0.05

    def test_level_four_variance_pair_is_the_largest_train_neutral_point(self):
        # B7's one re-tuned pair: a 3x3 grid sweep (train 2021-2023, 400
        # rosters/week) found the matched (0.60, 0.60) point confirmed
        # negative (train delta -0.794, 95% CI [-1.39,-0.19], excludes
        # zero) -- level 4 stops one notch short, at (0.45, 0.45), the
        # largest matched pair whose train CI still includes zero.
        cfg = SeasonConfig.from_spice_level(4)
        assert cfg.volatility_weight == 0.45
        assert cfg.upside_lean_weight == 0.45

    def test_structural_dials_are_zero_at_level_one_only(self):
        # B7: level 1 ("Baseline") has no tactics at all -- no blocking/
        # denial, no priority economics. Levels 2-4 all carry the same
        # judgment-set flat values (unmeasured by any backtest -- see each
        # field's own docstring).
        fields = (
            "blocking_hold_bonus", "denial_weight", "denial_opponent_boost",
            "denial_seed_window", "denial_priority_floor", "priority_value",
        )
        cfg1 = SeasonConfig.from_spice_level(1)
        for f in fields:
            assert getattr(cfg1, f) == 0, f"level 1, {f}"
        for level in (2, 3, 4):
            cfg = SeasonConfig.from_spice_level(level)
            for f in fields:
                assert getattr(cfg, f) != 0, f"level {level}, {f}"
                assert getattr(cfg, f) == getattr(SeasonConfig.from_spice_level(2), f), f"level {level}, {f}"

    def test_waiver_value_mode_is_naive_only_at_level_one(self):
        assert SeasonConfig.from_spice_level(1).waiver_value_mode == "points"
        for level in (2, 3, 4):
            assert SeasonConfig.from_spice_level(level).waiver_value_mode == "marginal"

    def test_streaming_weight_climbs_monotonically(self):
        values = [SeasonConfig.from_spice_level(lvl).streaming_weight for lvl in (1, 2, 3, 4)]
        assert values == sorted(values)
        assert 0.0 <= values[0] and values[-1] <= 1.0

    def test_kalshi_weight_is_zero_through_level_three(self):
        # Kalshi's NFL player-prop markets have zero overlap with this
        # repo's backtest window -- unlike the other info-axis fields,
        # kalshi_weight has no evidence base at any level below 4.
        for level in (1, 2, 3):
            assert SeasonConfig.from_spice_level(level).kalshi_weight == 0.0

    def test_kalshi_weight_is_nonzero_only_at_level_four(self):
        assert SeasonConfig.from_spice_level(4).kalshi_weight > 0.0

    def test_venue_disruption_weight_is_zero_through_level_three(self):
        for level in (1, 2, 3):
            assert SeasonConfig.from_spice_level(level).venue_disruption_weight == 0.0

    def test_venue_disruption_weight_is_nonzero_only_at_level_four(self):
        # Inconclusive, not confirmed-harmful -- ships at level 4 on the
        # same "use at your own risk" basis as kalshi_weight.
        assert SeasonConfig.from_spice_level(4).venue_disruption_weight > 0.0

    def test_out_of_range_level_raises_with_migration_hint(self):
        with pytest.raises(ValueError, match="1-4"):
            SeasonConfig.from_spice_level(5)


class TestDraftSpiceLadder:
    """B7 -- DraftConfig.spice_level, the draft-side analog of
    SeasonConfig.spice_level, rescaled to 4 levels. `None` (the dataclass
    default) must be a total no-op: nothing about how a plain
    `DraftConfig()`/`_construct` call resolves changes just because this
    field exists.
    """

    # arbitrage_weight is deliberately NOT here -- B5 retired it (confirmed
    # harm at its old live value, see its own docstring in ffbot/config.py)
    # and it now stays at its dataclass default (0.0) on every level; see
    # test_arbitrage_weight_is_retired_at_every_level below.
    _INFO_FIELDS = ("scoring_arbitrage_weight",)
    _VARIANCE_FIELDS = ("upside_weight", "volatility_weight", "stack_bonus")
    # B7: the five structural terms B5 always left out of the ladder now
    # join it (level 2 up) -- see each field's own docstring in
    # ffbot/config.py for the B7 isolation-sweep number behind it.
    _STRUCTURAL_FIELDS = (
        "team_concentration_weight", "same_team_position_weight",
        "bye_collision_weight", "block_weight", "balance_weight",
    )

    def test_bare_draft_config_spice_level_defaults_to_none(self):
        assert DraftConfig().spice_level is None

    def test_bare_draft_config_every_edge_weight_still_zero(self):
        # The new field's mere presence must not change a single existing
        # default -- this is what "no config.yml written before this field
        # existed changes behavior" actually rests on.
        cfg = DraftConfig()
        for f in self._INFO_FIELDS + self._VARIANCE_FIELDS + self._STRUCTURAL_FIELDS + ("risk_weight",):
            assert getattr(cfg, f) == 0.0, f

    def test_draft_from_dict_without_spice_level_key_is_a_construct_passthrough(self):
        raw = {"upside_weight": 0.7, "num_teams": 10}
        via_helper = _draft_from_dict(raw)
        assert via_helper.upside_weight == 0.7
        assert via_helper.num_teams == 10
        assert via_helper.spice_level is None

    def test_draft_from_dict_with_spice_level_resolves_the_preset(self):
        cfg = _draft_from_dict({"spice_level": 3})
        assert cfg.spice_level == 3
        assert cfg.upside_weight == DRAFT_SPICE_PRESETS[3]["upside_weight"]

    def test_draft_from_dict_spice_level_plus_override_wins_on_that_one_field(self):
        cfg = _draft_from_dict({"spice_level": 3, "upside_weight": 0.99})
        assert cfg.upside_weight == 0.99
        # Everything else still comes from the level-3 preset.
        assert cfg.risk_weight == DRAFT_SPICE_PRESETS[3]["risk_weight"]

    def test_level_one_is_every_edge_weight_at_zero(self):
        cfg = DraftConfig.from_spice_level(1)
        for f in self._INFO_FIELDS + self._VARIANCE_FIELDS + self._STRUCTURAL_FIELDS + ("risk_weight",):
            assert getattr(cfg, f) == 0.0, f

    def test_variance_fields_are_zero_through_level_two(self):
        for level in (1, 2):
            cfg = DraftConfig.from_spice_level(level)
            for f in self._VARIANCE_FIELDS:
                assert getattr(cfg, f) == 0.0, f"level {level}, {f}"

    def test_variance_fields_climb_monotonically_from_level_three(self):
        for f in self._VARIANCE_FIELDS:
            values = [getattr(DraftConfig.from_spice_level(lvl), f) for lvl in (3, 4)]
            assert values == sorted(values)
            assert values[0] < values[-1]

    def test_info_fields_climb_monotonically_across_every_level(self):
        for f in self._INFO_FIELDS:
            values = [getattr(DraftConfig.from_spice_level(lvl), f) for lvl in (1, 2, 3, 4)]
            assert values == sorted(values)

    def test_risk_weight_is_non_monotonic_dropping_from_level_three_to_four(self):
        # By design: higher spice tolerates MORE availability risk, so this
        # is the one edge dial that decreases as the ladder climbs.
        l3 = DraftConfig.from_spice_level(3)
        l4 = DraftConfig.from_spice_level(4)
        assert l3.risk_weight > 0.0
        assert l4.risk_weight < l3.risk_weight

    def test_risk_ramp_starts_earlier_and_completes_sooner_at_level_four(self):
        l3 = DraftConfig.from_spice_level(3)
        l4 = DraftConfig.from_spice_level(4)
        assert l4.risk_ramp_start <= l3.risk_ramp_start
        assert l4.risk_ramp_full <= l3.risk_ramp_full

    def test_structural_fields_are_zero_at_level_one_only(self):
        cfg1 = DraftConfig.from_spice_level(1)
        for f in self._STRUCTURAL_FIELDS:
            assert getattr(cfg1, f) == 0.0, f"level 1, {f}"
        for level in (2, 3, 4):
            cfg = DraftConfig.from_spice_level(level)
            for f in self._STRUCTURAL_FIELDS:
                assert getattr(cfg, f) != 0.0, f"level {level}, {f}"
                assert getattr(cfg, f) == getattr(DraftConfig.from_spice_level(2), f), f"level {level}, {f}"

    def test_arbitrage_weight_is_retired_at_every_level(self):
        # B5: confirmed harm at its old live value (0.20) via
        # scripts/backtest_draft.py -- excluded from every preset, so it
        # stays at the dataclass default (0.0) regardless of spice_level.
        for level in DRAFT_SPICE_PRESETS:
            assert DraftConfig.from_spice_level(level).arbitrage_weight == 0.0

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            DraftConfig.from_spice_level(9)

    def test_out_of_range_level_raises_with_migration_hint(self):
        with pytest.raises(ValueError, match="1-4"):
            DraftConfig.from_spice_level(5)

    def test_config_yml_now_resolves_the_draft_spice_ladder(self):
        # config.yml sets draft.spice_level: 3 and comments out every dial
        # DRAFT_SPICE_PRESETS controls (13 keys as of B7, including the
        # five structural terms newly folded in) -- this is the regression
        # guard for _draft_from_dict's override trap: any of them left
        # uncommented alongside spice_level would silently win over the
        # preset (see _draft_from_dict's docstring), making the ladder a
        # partial no-op for that one dial.
        cfg = Config.load("config.yml")
        assert cfg.draft.spice_level == 3
        assert cfg.draft.arbitrage_weight == 0.0  # B5: retired, excluded from every level
        for key, expected in DRAFT_SPICE_PRESETS[3].items():
            assert getattr(cfg.draft, key) == expected, key
        # depth_weight/depth_decay are NOT ladder fields (bench-depth
        # valuation stays hand-set at every level) -- proof the override
        # mechanism itself still works for everything outside the preset.
        assert cfg.draft.depth_decay == 0.5
