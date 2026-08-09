from __future__ import annotations

from ffbot.config import Config
from ffbot.policy import can_bid_on, can_drop, droppable, max_faab_bid

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


class TestFaab:
    def test_bid_is_capped_by_share_of_budget(self, cfg):
        # 35% of 100 = 35, well under the spendable 95.
        assert max_faab_bid(100, cfg) == 35

    def test_reserve_is_held_back_when_budget_is_low(self, cfg):
        # 35% of 10 = 3, but spendable is 10 - 5 = 5, so the cap binds at 3.
        assert max_faab_bid(10, cfg) == 3

    def test_reserve_binds_when_it_is_the_tighter_limit(self):
        cfg = Config()
        cfg.faab.max_bid_pct = 0.9
        cfg.faab.min_reserve = 5
        # 90% of 10 = 9, but only 5 is spendable.
        assert max_faab_bid(10, cfg) == 5

    def test_no_bidding_once_only_the_reserve_remains(self, cfg):
        assert max_faab_bid(5, cfg) == 0
        assert max_faab_bid(0, cfg) == 0

    def test_exhausted_budget_never_goes_negative(self, cfg):
        assert max_faab_bid(1, cfg) == 0

    def test_ignores_players_nobody_wants(self, cfg):
        v = can_bid_on(mk("Nobody", "WR", percent_owned=0.5), cfg)
        assert not v.allowed

    def test_bids_on_a_rising_player(self, cfg):
        assert can_bid_on(mk("Breakout", "WR", percent_owned=14.0), cfg).allowed
