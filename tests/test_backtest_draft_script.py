from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import ConfigError, DraftConfig
from scripts import backtest_draft  # noqa: E402
from scripts.backtest_draft import (  # noqa: E402
    _build_draft_config,
    _config_diff,
    _field_defaults,
    _parse_overrides,
)


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


class TestFieldDefaults:
    def test_scalar_fields_are_present_with_their_dataclass_default(self):
        defaults = _field_defaults()
        assert defaults["scarcity_weight"] == 0.0
        assert defaults["bench_replacement_depth"] == 0.0
        assert defaults["depth_decay"] == 1.0

    def test_default_factory_fields_are_excluded(self):
        # `position_caps`/`position_targets`/`aliases` are not sweepable
        # dials, and handing out a shared mutable default would be worse
        # than refusing -- `--isolate` errors on them by design.
        defaults = _field_defaults()
        for name in ("position_caps", "position_targets", "aliases", "board_csv"):
            assert name not in defaults


class TestConfigDiff:
    def test_identical_configs_report_no_difference(self):
        a = DraftConfig(num_teams=12)
        assert _config_diff(a, DraftConfig(num_teams=12)) == {}

    def test_reports_field_with_both_values(self):
        a = DraftConfig(scarcity_weight=1.0)
        c = DraftConfig(scarcity_weight=0.0)
        assert _config_diff(a, c) == {"scarcity_weight": (1.0, 0.0)}

    def test_reports_every_differing_field(self):
        a = DraftConfig(scarcity_weight=1.0, stack_bonus=0.3)
        c = DraftConfig(scarcity_weight=0.0, stack_bonus=0.0)
        assert set(_config_diff(a, c)) == {"scarcity_weight", "stack_bonus"}


class TestIsolationGuards:
    """The trap: both sides are built from `--config`'s own draft block, so
    an `--agent-override` for a dial already live in config.yml lands on the
    control too and measures nothing. This really happened to the first
    `scarcity_weight` sweep, which reported "0/90 drafts differed" -- output
    that reads like a finding about the dial rather than an operator error.
    """

    def _config_file(self, tmp_path, **draft_keys) -> str:
        body = {"roster_positions": {"QB": 1, "RB": 2, "WR": 2, "BN": 3}, "draft": draft_keys}
        path = tmp_path / "config.yml"
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        return str(path)

    def _run(self, argv):
        # `main` returns an exit code rather than raising, so a guard can be
        # asserted without running a single (expensive) draft simulation --
        # every check under test happens before the first season loop.
        return backtest_draft.main(argv)

    def test_override_matching_the_live_config_value_is_refused(self, tmp_path, capsys):
        cfg = self._config_file(tmp_path, scarcity_weight=1.0)
        rc = self._run([
            "--seasons", "2021", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1", "--control-spice-level", "1",
            "--agent-override", "scarcity_weight=1.0",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "cannot measure anything" in err
        assert "--isolate" in err

    def test_partial_no_op_override_is_refused_and_names_the_key(self, tmp_path, capsys):
        # Something else DOES differ (the spice levels), so the whole-config
        # check passes -- but the dial actually being swept still doesn't.
        cfg = self._config_file(tmp_path, scarcity_weight=1.0)
        rc = self._run([
            "--seasons", "2021", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "3", "--control-spice-level", "1",
            "--agent-override", "scarcity_weight=1.0",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "had no effect" in err
        assert "scarcity_weight" in err

    def test_isolate_sets_agent_to_value_and_control_to_the_default(self, tmp_path, capsys):
        cfg = self._config_file(tmp_path, scarcity_weight=1.0)
        # Seasons that have no cached board are skipped with a message, so
        # this reaches the diff print and then finds nothing to simulate --
        # enough to assert how --isolate resolved, without NFL data.
        self._run([
            "--seasons", "1999", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1", "--control-spice-level", "1",
            "--isolate", "scarcity_weight=1.0",
        ])
        out = capsys.readouterr().out
        assert "scarcity_weight: agent=1.0  control=0.0" in out

    def test_isolate_conflicting_with_an_explicit_override_is_refused(self, tmp_path, capsys):
        cfg = self._config_file(tmp_path)
        rc = self._run([
            "--seasons", "2021", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1",
            "--isolate", "scarcity_weight=1.0",
            "--agent-override", "scarcity_weight=2.0",
        ])
        assert rc == 1
        assert "conflicts with an explicit" in capsys.readouterr().err

    def test_isolate_on_a_non_scalar_field_is_refused_with_the_valid_list(self, tmp_path, capsys):
        cfg = self._config_file(tmp_path)
        rc = self._run([
            "--seasons", "2021", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1", "--isolate", "position_caps=2",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no scalar DraftConfig default" in err
        assert "scarcity_weight" in err  # the valid-field list is shown

    def test_a_genuinely_differing_sweep_is_not_refused(self, tmp_path, capsys):
        # The false-positive guard: a real sweep must still run. 1999 has no
        # cached board, so it skips the simulation and returns 1 for "nothing
        # to report" -- distinguishable from a guard refusal by the message.
        cfg = self._config_file(tmp_path, scarcity_weight=1.0)
        self._run([
            "--seasons", "1999", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1", "--control-spice-level", "1",
            "--control-override", "scarcity_weight=0.0",
        ])
        captured = capsys.readouterr()
        assert "cannot measure anything" not in captured.err
        assert "had no effect" not in captured.err
        assert "scarcity_weight: agent=1.0  control=0.0" in captured.out

    def test_differing_policies_alone_are_enough_to_measure(self, tmp_path, capsys):
        # agent-policy=adp vs control-policy=recommend is a legitimate
        # comparison even with identical configs (B7's VOR-chalk-vs-blind-ADP
        # run) -- the guard must not refuse it.
        cfg = self._config_file(tmp_path)
        self._run([
            "--seasons", "1999", "--seeds", "1", "--config", cfg,
            "--agent-spice-level", "1", "--control-spice-level", "1",
            "--agent-policy", "adp",
        ])
        captured = capsys.readouterr()
        assert "cannot measure anything" not in captured.err
        assert "none (policies differ)" in captured.out
