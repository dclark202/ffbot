from __future__ import annotations

import pytest

from ffbot import policy
from ffbot.config import Config, SeasonConfig
from ffbot.policy import can_drop, droppable

from .conftest import mk


class TestDropProtection:
    def test_unprotected_player_may_be_dropped(self, cfg):
        v = can_drop(mk("Scrub", "RB", percent_owned=3.0, draft_round=None), cfg)
        assert v.allowed

    def test_yahoo_undroppable_flag_is_absolute(self, cfg):
        v = can_drop(mk("Star", "RB", percent_owned=1.0, is_undroppable=True), cfg)
        assert not v.allowed
        assert "undroppable" in v.reason

    def test_never_drop_list_is_honoured_case_insensitively(self):
        cfg = Config()
        cfg.drops.never_drop = ["  bijan robinson "]
        v = can_drop(mk("Bijan Robinson", "RB", percent_owned=1.0), cfg)
        assert not v.allowed
        assert "never_drop" in v.reason

    def test_high_ownership_protects(self, cfg):
        v = can_drop(mk("Popular", "WR", percent_owned=85.0), cfg)
        assert not v.allowed
        assert "85%" in v.reason

    def test_ownership_just_below_threshold_does_not_protect(self, cfg):
        assert can_drop(mk("Fringe", "WR", percent_owned=59.9), cfg).allowed

    def test_early_draft_pick_protects(self, cfg):
        v = can_drop(mk("Pick2", "RB", percent_owned=10.0, draft_round=2), cfg)
        assert not v.allowed
        assert "round 2" in v.reason

    def test_late_draft_pick_does_not_protect(self, cfg):
        assert can_drop(mk("Pick9", "RB", percent_owned=10.0, draft_round=9), cfg).allowed

    def test_bye_week_player_is_not_droppable(self, cfg):
        """The classic self-inflicted wound this rule exists to prevent."""
        p = mk("OnBye", "RB", percent_owned=20.0, bye_week=7)
        v = can_drop(p, cfg, week=7)
        assert not v.allowed
        assert "temporarily" in v.reason

    def test_same_player_is_droppable_outside_their_bye(self, cfg):
        p = mk("OnBye", "RB", percent_owned=20.0, bye_week=7)
        assert can_drop(p, cfg, week=8).allowed

    def test_questionable_player_is_not_droppable(self, cfg):
        v = can_drop(mk("Dinged", "RB", percent_owned=20.0, status="Q"), cfg, week=3)
        assert not v.allowed

    def test_season_ending_injury_is_droppable(self, cfg):
        """IR is not temporary in the way a bye is — this one may go."""
        assert can_drop(mk("Done", "RB", percent_owned=20.0, status="IR"), cfg, week=3).allowed

    def test_missing_ownership_data_does_not_protect_by_accident(self, cfg):
        assert can_drop(mk("Unknown", "RB", percent_owned=None), cfg).allowed


class TestDroppableOrdering:
    def test_worst_player_is_offered_first(self, cfg):
        players = [
            mk("Mid", "RB", percent_owned=40.0),
            mk("Worst", "RB", percent_owned=1.0),
            mk("Protected", "RB", percent_owned=90.0),
        ]
        result = droppable(players, cfg)
        assert [p.name for p in result] == ["Worst", "Mid"]

    def test_returns_empty_when_everyone_is_protected(self, cfg):
        players = [mk("A", "RB", percent_owned=90.0), mk("B", "RB", is_undroppable=True)]
        assert droppable(players, cfg) == []

    def test_custom_key_overrides_percent_owned_ordering(self, cfg):
        # Every percent_owned is None (no live Yahoo data) -- exactly the
        # case the default ordering degenerates to a no-op tie on. A real
        # key (e.g. week.hold_margin) must actually be honored instead.
        players = [mk("Mid", "RB"), mk("Worst", "RB"), mk("Best", "RB")]
        value = {"Worst": -10.0, "Mid": 0.0, "Best": 10.0}
        result = droppable(players, cfg, key=lambda p: value[p.name])
        assert [p.name for p in result] == ["Worst", "Mid", "Best"]

class TestCanClaim:
    """The noise-floor guardrail. Ships at 0.0 (an exact no-op) pending
    evidence, so most of what matters here is that it is inert by default and
    correctly shaped when it is not."""

    SCALE = 7.1
    PRED = {"QB": 0.385, "RB": 0.406, "WR": 0.423, "TE": 0.502, "K": 0.199, "DEF": 0.232}

    def _cfg(self, weight: float) -> Config:
        return Config(season=SeasonConfig(noise_floor_weight=weight))

    def test_shipped_default_is_an_exact_no_op(self):
        from ffbot.config import Config as C

        cfg = C.load("config.yml")
        assert cfg.season.noise_floor_weight == 0.0
        for gain in (0.0001, 0.6, 50.0):
            assert policy.can_claim(gain, "DEF", self.SCALE, cfg, self.PRED).allowed

    def test_a_sub_floor_gain_is_refused_with_the_number_in_the_reason(self):
        v = policy.can_claim(0.6, "DEF", self.SCALE, self._cfg(0.10), self.PRED)
        assert not v.allowed
        assert "DEF" in v.reason
        assert "noise floor" in v.reason

    def test_a_real_gain_clears_it(self):
        assert policy.can_claim(13.9, "RB", self.SCALE, self._cfg(0.10), self.PRED).allowed

    def test_empty_predictiveness_degrades_to_a_position_blind_floor(self):
        """The state of every live board before `draft.rank_calibration` was
        pointed at the curve file, and of any fresh clone (data/history/ is
        gitignored). The per-position sharpening is a bonus, not a
        prerequisite."""
        cfg = self._cfg(0.10)
        reasons = {
            policy.can_claim(0.01, pos, self.SCALE, cfg, {}).reason
            for pos in ("WR", "DEF", "K", "TE")
        }
        floors = {r.split("inside the ")[1].split("-point")[0] for r in reasons}
        assert len(floors) == 1, f"floor varied by position with no factors: {floors}"

    def test_populated_predictiveness_makes_def_stricter_than_wr(self):
        """B10's measurement, which is the whole basis for a per-position
        floor: DEF projections carry roughly half a WR's signal, so a DEF
        needs about twice the margin to mean anything."""
        cfg = self._cfg(0.10)
        def floor_for(pos):
            v = policy.can_claim(-1.0, pos, self.SCALE, cfg, self.PRED)
            return float(v.reason.split("inside the ")[1].split("-point")[0])

        assert floor_for("DEF") > floor_for("WR")
        assert floor_for("K") > floor_for("TE")

    def test_an_unknown_position_does_not_blow_up_the_floor(self):
        """`_MIN_PREDICTIVENESS` bounds the divisor -- an absent or
        pathologically small factor must not produce an unbounded floor."""
        cfg = self._cfg(0.10)
        v = policy.can_claim(-1.0, "DEF", self.SCALE, cfg, {"DEF": 0.0})
        floor = float(v.reason.split("inside the ")[1].split("-point")[0])
        assert floor <= 0.10 * self.SCALE / 0.1 + 1e-9

    def test_the_floor_scales_with_the_decision_scale(self):
        cfg = self._cfg(0.10)
        def floor_for(scale):
            v = policy.can_claim(-1.0, "WR", scale, cfg, self.PRED)
            return float(v.reason.split("inside the ")[1].split("-point")[0])

        assert floor_for(14.2) == pytest.approx(floor_for(7.1) * 2, rel=1e-3)

    def test_it_is_pure_and_holds_no_forced_need_logic(self):
        """The bye/OUT exemption belongs to the CALLER -- `gameplan` knows
        whether the incumbent is out; `policy` must not guess."""
        import inspect

        src = inspect.getsource(policy.can_claim)
        for leak in ("bye", "incumbent", "status", "OUT"):
            assert leak not in src, f"forced-need logic leaked into policy: {leak}"
