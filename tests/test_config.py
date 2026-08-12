from __future__ import annotations

import warnings

import pytest

from ffbot.config import (
    DRAFT_SPICE_PRESETS,
    SPICE_PRESETS,
    Config,
    ConfigError,
    DraftConfig,
    LeagueScoring,
    ProjectionConfig,
    ProjectionSourceConfig,
    ScoringConfig,
    SeasonConfig,
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
        assert empty.league_id == bare.league_id
        assert empty.roster_positions == bare.roster_positions
        assert empty.projection == bare.projection
        assert empty.projection_source == bare.projection_source
        assert empty.drops == bare.drops
        assert empty.faab == bare.faab
        assert empty.draft == bare.draft
        assert empty.league == bare.league is None

    def test_scoring_config_alias_still_importable(self):
        assert ScoringConfig is ProjectionConfig


class TestDraftConfigOrder:
    def test_default_is_snake(self):
        assert DraftConfig().order == "snake"


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


class TestConfigLocalOverlay:
    def test_missing_overlay_is_a_noop(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("league_id: 'abc'\n", encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.league_id == "abc"

    def test_overlay_field_wins_over_base(self, tmp_path):
        (tmp_path / "config.yml").write_text("league_id: 'abc'\n", encoding="utf-8")
        (tmp_path / "config.local.yml").write_text("league_id: 'xyz'\n", encoding="utf-8")
        cfg = Config.load(tmp_path / "config.yml")
        assert cfg.league_id == "xyz"

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
        p.write_text("league_id: 'solo'\n", encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.league_id == "solo"

    def test_no_base_config_but_overlay_present(self, tmp_path):
        (tmp_path / "config.local.yml").write_text("league_id: 'only-local'\n", encoding="utf-8")
        cfg = Config.load(tmp_path / "config.yml")
        assert cfg.league_id == "only-local"


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


class TestLeagueScoringFromDict:
    def test_unknown_top_block_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"passing": {"nonexistent_field": 1}})

    def test_nested_distance_band_unknown_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"kicking": {"fg_by_distance": [{"min": 0, "max": 39, "pointz": 3}]}})

    def test_full_round_trip(self):
        league = LeagueScoring.from_dict({
            "waiver_type": "rolling",
            "passing": {"int": -2},
            "kicking": {"fg_by_distance": [{"min": 0, "max": 39, "points": 3}]},
            "defense": {"points_allowed": [{"max": 0, "points": 10}]},
        })
        assert league.waiver_type == "rolling"
        assert league.passing.int == -2
        assert league.kicking.fg_by_distance[0].points == 3
        assert league.defense.points_allowed[0].points == 10


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
            "my_team": "Jake Butt's Button Bazaar",
            "teams": [
                {"name": "Rival A", "record": "6-2", "seed": 1, "waiver_priority": 9},
                {"name": "Rival B", "record": "2-6", "seed": 11, "eliminated": True},
            ],
        })
        assert league.week == 8
        assert league.my_team == "Jake Butt's Button Bazaar"
        assert len(league.teams) == 2
        assert league.teams[0] == TeamStanding(name="Rival A", record="6-2", seed=1, waiver_priority=9)
        assert league.teams[1].eliminated is True

    def test_unknown_team_key_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"teams": [{"name": "X", "not_a_real_field": 1}]})

    def test_team_missing_name_raises(self):
        with pytest.raises(ConfigError):
            LeagueScoring.from_dict({"teams": [{"record": "6-2"}]})


class TestTwoAxisSpiceLadder:
    """B5 -- SPICE_PRESETS re-derived along two axes: information (weather,
    vegas, usage/momentum/divergence trend) ramps 1->3, variance
    (volatility, upside_lean, matchup_variance) ramps 3->5. See
    docs/BACKTEST.md's B5 section for the measurements behind these values.
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
            values = [getattr(SeasonConfig.from_spice_level(lvl), f) for lvl in (3, 4, 5)]
            assert values == sorted(values)
            assert values[0] < values[-1]  # strictly increasing, not flat

    def test_info_fields_climb_monotonically_across_every_level(self):
        for f in self._INFO_FIELDS:
            values = [getattr(SeasonConfig.from_spice_level(lvl), f) for lvl in (1, 2, 3, 4, 5)]
            assert values == sorted(values)

    def test_venue_disruption_and_denial_stay_out_of_every_level(self):
        # Unchanged invariant from before this re-derivation -- no evidence
        # base for venue_disruption_weight, and denial needs external
        # config regardless of spice level.
        for level in SPICE_PRESETS:
            cfg = SeasonConfig.from_spice_level(level)
            assert cfg.venue_disruption_weight == 0.0
            assert cfg.denial_weight == 0.0
            assert cfg.blocking_hold_bonus == 0.0

    def test_streaming_weight_climbs_monotonically(self):
        values = [SeasonConfig.from_spice_level(lvl).streaming_weight for lvl in (1, 2, 3, 4, 5)]
        assert values == sorted(values)
        assert 0.0 <= values[0] and values[-1] <= 1.0


class TestDraftSpiceLadder:
    """B5 -- DraftConfig.spice_level, the draft-side analog of
    SeasonConfig.spice_level. `None` (the dataclass default) must be a
    total no-op: nothing about how a plain `DraftConfig()`/`_construct`
    call resolves changes just because this field exists.
    """

    # arbitrage_weight is deliberately NOT here -- B5 retired it (confirmed
    # harm at its old live value, see its own docstring in ffbot/config.py)
    # and it now stays at its dataclass default (0.0) on every level; see
    # test_arbitrage_weight_is_retired_at_every_level below.
    _INFO_FIELDS = ("scoring_arbitrage_weight",)
    _VARIANCE_FIELDS = ("upside_weight", "volatility_weight", "stack_bonus")

    def test_bare_draft_config_spice_level_defaults_to_none(self):
        assert DraftConfig().spice_level is None

    def test_bare_draft_config_every_edge_weight_still_zero(self):
        # The new field's mere presence must not change a single existing
        # default -- this is what "no config.yml written before this field
        # existed changes behavior" actually rests on.
        cfg = DraftConfig()
        for f in self._INFO_FIELDS + self._VARIANCE_FIELDS + ("risk_weight",):
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
        for f in self._INFO_FIELDS + self._VARIANCE_FIELDS + ("risk_weight",):
            assert getattr(cfg, f) == 0.0, f

    def test_variance_fields_are_zero_through_level_two(self):
        for level in (1, 2):
            cfg = DraftConfig.from_spice_level(level)
            for f in self._VARIANCE_FIELDS:
                assert getattr(cfg, f) == 0.0, f"level {level}, {f}"

    def test_variance_fields_climb_monotonically_from_level_three(self):
        for f in self._VARIANCE_FIELDS:
            values = [getattr(DraftConfig.from_spice_level(lvl), f) for lvl in (3, 4, 5)]
            assert values == sorted(values)
            assert values[0] < values[-1]

    def test_info_fields_climb_monotonically_across_every_level(self):
        for f in self._INFO_FIELDS:
            values = [getattr(DraftConfig.from_spice_level(lvl), f) for lvl in (1, 2, 3, 4, 5)]
            assert values == sorted(values)

    def test_risk_ramp_starts_earlier_and_completes_sooner_at_level_five(self):
        l3 = DraftConfig.from_spice_level(3)
        l5 = DraftConfig.from_spice_level(5)
        assert l5.risk_ramp_start <= l3.risk_ramp_start
        assert l5.risk_ramp_full <= l3.risk_ramp_full

    def test_untouched_fields_stay_out_of_every_level(self):
        # Same "no evidence base -- don't ride the dial" reasoning as
        # venue_disruption_weight on the weekly side.
        for level in DRAFT_SPICE_PRESETS:
            cfg = DraftConfig.from_spice_level(level)
            assert cfg.team_concentration_weight == 0.0
            assert cfg.same_team_position_weight == 0.0
            assert cfg.bye_collision_weight == 0.0
            assert cfg.block_weight == 0.0
            assert cfg.balance_weight == 0.0

    def test_arbitrage_weight_is_retired_at_every_level(self):
        # B5: confirmed harm at its old live value (0.20) via
        # scripts/backtest_draft.py -- excluded from every preset, so it
        # stays at the dataclass default (0.0) regardless of spice_level.
        for level in DRAFT_SPICE_PRESETS:
            assert DraftConfig.from_spice_level(level).arbitrage_weight == 0.0

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            DraftConfig.from_spice_level(9)

    def test_config_yml_without_spice_level_is_bit_identical_to_before(self):
        # The regression guard: config.yml's OWN hand-narrated draft block
        # has no spice_level key, so loading it must resolve to exactly
        # what it always has.
        cfg = Config.load("config.yml")
        assert cfg.draft.spice_level is None
        assert cfg.draft.arbitrage_weight == 0.0  # B5: retired, see config.yml's own comment
        assert cfg.draft.upside_weight == 0.45
