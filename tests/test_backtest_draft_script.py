from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import ConfigError, DraftConfig
from scripts.backtest_draft import _build_draft_config, _parse_overrides  # noqa: E402


class TestParseOverrides:
    def test_float_field_parses_as_float(self):
        assert _parse_overrides(["bye_collision_weight=0.30"]) == {"bye_collision_weight": 0.30}

    def test_int_field_parses_as_int_not_float(self):
        out = _parse_overrides(["risk_ramp_start=1"])
        assert out == {"risk_ramp_start": 1}
        assert isinstance(out["risk_ramp_start"], int)

    def test_multiple_specs(self):
        out = _parse_overrides(["stack_bonus=0.30", "block_weight=0.40"])
        assert out == {"stack_bonus": 0.30, "block_weight": 0.40}

    def test_empty_list_returns_empty_dict(self):
        assert _parse_overrides([]) == {}

    def test_malformed_spec_raises(self):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            _parse_overrides(["not-a-kv-pair"])


class TestBuildDraftConfig:
    def _base(self) -> DraftConfig:
        # Mirrors config.yml's own draft block having real structural
        # fields set -- exactly what a bare `DraftConfig.from_spice_level`
        # call used to silently discard (the B5-era bug this fixes).
        # depth_decay is NOT a DRAFT_SPICE_PRESETS field (bench-depth
        # valuation stays hand-set at every level), so it's the field this
        # class uses to prove non-ladder config.yml values survive a preset
        # apply untouched.
        return DraftConfig(
            num_teams=10, position_targets={"QB": 1, "RB": 5}, position_caps={"QB": 2},
            depth_decay=0.5,
        )

    def test_structural_fields_survive_the_preset_overlay(self):
        base = self._base()
        out = _build_draft_config(base, 1, {})
        assert out.num_teams == 10
        assert out.position_targets == {"QB": 1, "RB": 5}
        assert out.position_caps == {"QB": 2}
        assert out.depth_decay == 0.5

    def test_level_one_preset_zeros_ladder_dials_but_leaves_non_ladder_fields_alone(self):
        # stack_bonus and block_weight are BOTH DRAFT_SPICE_PRESETS fields
        # as of B7 -- a level-1 preset apply must zero them. depth_decay is
        # NOT a ladder field, so the base config's hand-set 0.5 must survive
        # untouched -- this is the exact "preserve structural machinery"
        # behavior the preset-overlay (vs. whole-object-replace) fixes.
        base = self._base()
        out = _build_draft_config(base, 1, {})
        assert out.stack_bonus == 0.0
        assert out.block_weight == 0.0
        assert out.depth_decay == 0.5

    def test_higher_level_resolves_nonzero_preset_values(self):
        base = self._base()
        out = _build_draft_config(base, 3, {})
        assert out.upside_weight > 0.0
        assert out.spice_level == 3

    def test_override_wins_over_the_preset(self):
        base = self._base()
        out = _build_draft_config(base, 1, {"stack_bonus": 0.99})
        assert out.stack_bonus == 0.99

    def test_override_does_not_disturb_other_preserved_fields(self):
        base = self._base()
        out = _build_draft_config(base, 1, {"stack_bonus": 0.99})
        assert out.position_targets == {"QB": 1, "RB": 5}

    def test_out_of_range_level_raises_value_error(self):
        with pytest.raises(ValueError, match="spice_level"):
            _build_draft_config(self._base(), 99, {})

    def test_unknown_override_key_raises_config_error_naming_valid_keys(self):
        with pytest.raises(ConfigError, match="Valid keys"):
            _build_draft_config(self._base(), 1, {"not_a_real_field": 1.0})
