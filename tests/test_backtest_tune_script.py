from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import SeasonConfig
from scripts.backtest_tune import (  # noqa: E402
    LINEUP_INERT_FIELDS,
    NO_PROVIDER_FIELDS,
    SIGNAL_DEPENDENT_FIELDS,
    _apply_overrides,
    _parse_grid,
    main,
)


class TestParseGrid:
    def test_single_key_produces_one_combo_per_value(self):
        combos = _parse_grid(["vegas_weight=0,0.1,0.2"])
        assert combos == [{"vegas_weight": 0.0}, {"vegas_weight": 0.1}, {"vegas_weight": 0.2}]

    def test_two_keys_produce_the_cartesian_product(self):
        combos = _parse_grid(["vegas_weight=0,0.1", "weather_weight=0,0.2"])
        assert combos == [
            {"vegas_weight": 0.0, "weather_weight": 0.0},
            {"vegas_weight": 0.0, "weather_weight": 0.2},
            {"vegas_weight": 0.1, "weather_weight": 0.0},
            {"vegas_weight": 0.1, "weather_weight": 0.2},
        ]


class TestApplyOverrides:
    def test_spice_level_resolves_the_preset(self):
        base = SeasonConfig()
        out = _apply_overrides(base, {"spice_level": 3})
        assert out == SeasonConfig.from_spice_level(3)

    def test_non_spice_key_overlays_onto_base_leaving_everything_else(self):
        base = SeasonConfig.from_spice_level(3)
        out = _apply_overrides(base, {"vegas_weight": 0.99})
        assert out.vegas_weight == 0.99
        assert out.weather_weight == base.weather_weight  # untouched


class TestNewDialsAreDisjointFromSignalDependentFields:
    def test_no_provider_and_lineup_inert_do_not_overlap_signal_dependent(self):
        assert not (NO_PROVIDER_FIELDS & SIGNAL_DEPENDENT_FIELDS)
        assert not (LINEUP_INERT_FIELDS & SIGNAL_DEPENDENT_FIELDS)

    def test_kalshi_weight_is_a_real_seasonconfig_field(self):
        assert hasattr(SeasonConfig(), "kalshi_weight")

    def test_every_lineup_inert_field_is_a_real_seasonconfig_field(self):
        cfg = SeasonConfig()
        for name in LINEUP_INERT_FIELDS:
            assert hasattr(cfg, name), name


class TestMainRefusesDeadSweeps:
    def _argv(self, grid: str) -> list[str]:
        return ["--train", "2021-2022", "--test", "2023", "--grid", grid]

    def test_kalshi_weight_sweep_is_refused_before_touching_config(self, capsys):
        # No --config is passed and no historical cache exists at the
        # default path -- if this reaches Config.load/replay it will raise
        # a different, unrelated error. Reaching the "structurally
        # unmeasurable" message proves the guard fires first.
        rc = main(self._argv("kalshi_weight=0,0.15"))
        assert rc == 1
        assert "structurally unmeasurable" in capsys.readouterr().err

    def test_streaming_weight_sweep_is_refused_before_touching_config(self, capsys):
        rc = main(self._argv("streaming_weight=0,0.9"))
        assert rc == 1
        assert "dead-dial sweep in THIS script" in capsys.readouterr().err

    def test_denial_weight_sweep_is_refused(self, capsys):
        rc = main(self._argv("denial_weight=0,0.15"))
        assert rc == 1
        assert "dead-dial sweep in THIS script" in capsys.readouterr().err

    def test_ordinary_dial_is_not_covered_by_either_new_guard_set(self):
        # vegas_weight is a real, exercisable weekly dial -- it must not be
        # caught by either new refusal, or a legitimate sweep would be
        # blocked. (Not run through main() end-to-end: past these guards,
        # main() calls Config.load + a real historical replay, which needs
        # network/cache setup this unit test shouldn't depend on.)
        assert "vegas_weight" not in NO_PROVIDER_FIELDS
        assert "vegas_weight" not in LINEUP_INERT_FIELDS
