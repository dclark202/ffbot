"""A dial that is set but has no input must say so.

Five weekly dials shipped set, sliderized, and structurally inert for an
entire season: nothing in this repo ever wrote the researched fields they
read, `research-week.md` never asked for them, and `_momentum_multiplier`
returned exactly 1.0 for every player, every run, with nothing saying so.
That is the "never a silent success" contract failing one level below a
fetch. See the 2026-09-16 entry in docs/dev/INSEASON-FINDINGS.md.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ffbot import week
from ffbot.config import Config, SeasonConfig
from ffbot.report import _INTEL_BACKED_DIALS, _NOT_A_DIAL, _intel_coverage_alerts


def _cfg(**season_kw) -> Config:
    return Config(season=SeasonConfig(**season_kw))


def _intel(**per_player) -> week.WeeklyIntel:
    return week.WeeklyIntel(
        players={
            name: week.WeeklyPlayerIntel(name=name, **fields)
            for name, fields in per_player.items()
        }
    )


class TestTheDialTableCannotDriftFromTheCode:
    """Derive the set from the source rather than trusting a hand-written
    constant -- the technique `tests/test_config.py`'s untested-dial guard
    uses, for the same reason: two hand-maintained lists silently disagree.
    """

    def test_every_researched_score_field_is_a_dial_input_or_declared_not_one(self):
        src = Path(inspect.getsourcefile(week)).read_text(encoding="utf-8")
        fields = {
            node.args[0].value
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_score_field"
            and node.args and isinstance(node.args[0], ast.Constant)
        }
        assert fields, "the AST scan found no _score_field calls -- it has gone stale"
        unaccounted = fields - set(_INTEL_BACKED_DIALS.values()) - set(_NOT_A_DIAL)
        assert not unaccounted, (
            f"researched field(s) {sorted(unaccounted)} are read from weekly/week-NN.yml "
            "but are neither a dial's input in _INTEL_BACKED_DIALS nor declared in "
            "_NOT_A_DIAL. A new dial that reads a new field must register the pair, or "
            "it can go inert for a season with nothing saying so."
        )

    def test_every_registered_dial_actually_exists_on_the_config(self):
        season = SeasonConfig()
        for dial in _INTEL_BACKED_DIALS:
            assert hasattr(season, dial), f"{dial} is registered but is not a SeasonConfig field"

    def test_every_registered_field_actually_exists_on_the_intel_entry(self):
        entry = week.WeeklyPlayerIntel(name="x")
        for fieldname in _INTEL_BACKED_DIALS.values():
            assert hasattr(entry, fieldname)


class TestInertDialCoverage:
    def test_a_set_dial_with_zero_coverage_alerts(self):
        alerts = _intel_coverage_alerts(_intel(Somebody={"note": "role change"}), _cfg(usage_weight=0.15))
        assert len(alerts) == 1
        assert "usage_weight 0.15 (usage_trend)" in alerts[0]
        assert "inert" in alerts[0]

    def test_all_five_shipped_dials_are_named_when_research_writes_only_notes(self):
        """The exact 2026 state: eight researched entries, every one of them
        carrying `note` and nothing else."""
        cfg = _cfg(
            usage_weight=0.15, momentum_weight=0.15, divergence_weight=0.05,
            volatility_weight=0.05, upside_lean_weight=0.05,
        )
        alerts = _intel_coverage_alerts(
            _intel(**{f"P{i}": {"note": "n"} for i in range(8)}), cfg
        )
        assert len(alerts) == 1
        for dial in ("usage_weight", "momentum_weight", "divergence_weight",
                     "volatility_weight", "upside_lean_weight"):
            assert dial in alerts[0]
        assert "0 of 8" in alerts[0]

    def test_a_zero_weight_dial_never_alerts(self):
        """Nothing is wrong with an unset dial having no input."""
        assert _intel_coverage_alerts(_intel(P={"note": "n"}), _cfg(usage_weight=0.0)) == []

    def test_full_coverage_is_silent(self):
        alerts = _intel_coverage_alerts(
            _intel(A={"usage_trend": 60.0}, B={"usage_trend": 20.0}), _cfg(usage_weight=0.15)
        )
        assert alerts == []

    def test_partial_coverage_gets_the_bias_line_not_the_inert_line(self):
        """The more dangerous of the two, and the easier to miss:
        `week.usage_score(None)` returns 0.0, not a neutral 0.5, so an
        uncovered player is not "less signal" -- he is scored against, and
        which players are covered becomes a ranking effect in itself.
        """
        alerts = _intel_coverage_alerts(
            _intel(A={"usage_trend": 60.0}, B={"note": "n"}, C={"note": "n"}),
            _cfg(usage_weight=0.15),
        )
        assert len(alerts) == 1
        assert "input for 1 of 3" in alerts[0]
        assert "not a neutral 0.5" in alerts[0]
        assert "inert" not in alerts[0]

    def test_the_uncovered_score_really_is_zero_not_a_neutral_midpoint(self):
        """The premise of the alert above, asserted rather than assumed."""
        assert week.usage_score(None) == 0.0
        assert week.momentum_score(None) == 0.0
        assert week.divergence_score(None) == 0.0
        assert week.volatility_score(None) == 0.0
        assert week.upside_score(None) == 0.0

    def test_no_researched_entries_at_all_still_names_the_dials(self):
        """`week-NN.yml` missing entirely -- the week-2 case."""
        alerts = _intel_coverage_alerts(week.WeeklyIntel(), _cfg(usage_weight=0.15))
        assert len(alerts) == 1 and "0 of 0" in alerts[0]


class TestTheAlertReachesBothSurfaces:
    """`_all_alerts`' own docstring requires the CLI and the GUI to
    concatenate the same lists in the same order, so a new list added to one
    and not the other is silently dropped from the log."""

    def test_both_concatenations_name_the_new_lists(self):
        gui = Path("ffbot/webapi.py").read_text(encoding="utf-8")
        cli = Path("scripts/week_report.py").read_text(encoding="utf-8")
        for name in ("waiver_demand_alerts", "intel_coverage_alerts"):
            assert f"loaded.{name}" in gui, f"{name} missing from webapi's alert block"
            assert f"loaded.{name}" in cli, f"{name} missing from week_report._all_alerts"

    def test_they_appear_in_the_same_relative_order_in_both(self):
        gui = Path("ffbot/webapi.py").read_text(encoding="utf-8")
        cli = Path("scripts/week_report.py").read_text(encoding="utf-8")
        # Both files list them positionally; compare the index ordering.
        def idx(text):
            return sorted(
                ("availability_alerts", "waiver_demand_alerts", "intel_coverage_alerts"),
                key=lambda n: text.index(f"loaded.{n}"),
            )
        assert idx(gui) == idx(cli)
