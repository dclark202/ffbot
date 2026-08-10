from __future__ import annotations

import warnings

import pytest

from ffbot.config import (
    Config,
    ConfigError,
    DraftConfig,
    LeagueScoring,
    ProjectionConfig,
    ScoringConfig,
    TeamStanding,
    _coerce_block,
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
        assert empty.drops == bare.drops
        assert empty.faab == bare.faab
        assert empty.draft == bare.draft
        assert empty.league == bare.league is None

    def test_scoring_config_alias_still_importable(self):
        assert ScoringConfig is ProjectionConfig


class TestDraftConfigOrder:
    def test_default_is_snake(self):
        assert DraftConfig().order == "snake"

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
