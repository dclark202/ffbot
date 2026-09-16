"""The league-demand seam and the speculative surface it feeds.

Every test here is about one of two contracts: that demand is DESCRIPTIVE
(it changes no recommendation, ever) and that a speculative row is not
executable (it never reaches a lineup, a drop or the recommend budget).
Both are proven structurally -- by building the same plan twice and diffing,
or by asserting the absence of a field -- rather than by sampling outputs.
"""

from __future__ import annotations

import json

import pytest

from ffbot import demand as demand_mod
from ffbot import week as weekmod
from ffbot.config import Config
from ffbot.demand import DemandSignal


# The three rosters that claimed Kaelon Black at the 2026-09-16 run, in
# Sleeper's own shape. Roster 1 is the manager's own.
_BLACK = "13414"
_PLAYERS = {
    _BLACK: {"full_name": "Kaelon Black", "position": "RB", "team": "SF"},
    "11834": {"first_name": "Some", "last_name": "Other", "position": "WR", "team": "GB"},
    "DET": {"position": "DEF", "team": "DET"},
}


def _waiver(status, rosters, adds, created=1789560013691):
    return {
        "type": "waiver", "status": status, "roster_ids": list(rosters),
        "adds": dict(adds), "drops": {}, "created": created,
        "status_updated": created,
    }


class TestRivalClaimSignals:
    """The signal that prompted all of this: it was already fetched every
    run and thrown away twice (`availability.derive` keeps only
    `status == "complete"`, `claim_outcomes` keeps only your own roster)."""

    def test_a_failed_rival_claim_is_counted(self):
        tx = [
            _waiver("complete", [2], {_BLACK: 2}),
            _waiver("failed", [5], {_BLACK: 5}),
            _waiver("failed", [12], {_BLACK: 12}),
        ]
        out, notes = demand_mod.rival_claim_signals(tx, _PLAYERS, my_roster_id=1)
        signals = out["kaelon black:RB"]
        assert len(signals) == 1
        assert signals[0].value == 3.0
        assert signals[0].unit == "claims"
        assert notes == []

    def test_it_is_retrospective_and_says_so(self):
        """The whole reason `is_retrospective` exists. Sleeper does not
        publish a claim until the run has processed it, so this can never
        inform the Tuesday check that precedes the run."""
        tx = [_waiver("failed", [5], {_BLACK: 5})]
        out, _ = demand_mod.rival_claim_signals(tx, _PLAYERS, my_roster_id=1)
        assert out["kaelon black:RB"][0].is_retrospective is True

    def test_your_own_claim_is_not_league_demand(self):
        tx = [_waiver("failed", [1], {_BLACK: 1})]
        out, _ = demand_mod.rival_claim_signals(tx, _PLAYERS, my_roster_id=1)
        assert out == {}

    def test_a_pending_claim_is_live_and_noted_once(self):
        """W7: whether the endpoint returns pending claims is unverified.
        Rather than assume, the module records the answer the first time it
        sees one -- the same way `availability` learns the real weekly run
        time from the log instead of trusting the configured guess."""
        tx = [_waiver("pending", [7], {_BLACK: 7})]
        out, notes = demand_mod.rival_claim_signals(tx, _PLAYERS, my_roster_id=1)
        assert out["kaelon black:RB"][0].is_retrospective is False
        assert len(notes) == 1 and "pending claim" in notes[0]


class TestTrendingAndOwnership:
    def test_trending_counts_are_leagues_not_percentages(self):
        rows = [{"player_id": _BLACK, "count": 41200}]
        out = demand_mod.trending_signals(rows, _PLAYERS, 48)
        sig = out["kaelon black:RB"][0]
        assert sig.value == 41200.0 and sig.unit == "leagues" and sig.window == "48h"
        assert sig.is_retrospective is False  # available BEFORE the run

    def test_ownership_delta_ignores_jitter(self):
        cur = {_BLACK: {"owned": 36.5}, "11834": {"owned": 50.2}}
        prior = {_BLACK: {"owned": 34.1}, "11834": {"owned": 50.0}}
        out = demand_mod.ownership_delta_signals(cur, prior, _PLAYERS, 2, min_delta=1.0)
        assert "kaelon black:RB" in out
        assert out["kaelon black:RB"][0].value == pytest.approx(2.4)
        assert "some other:WR" not in out  # +0.2 is noise, and gets no entry at all

    def test_a_missing_prior_snapshot_yields_nothing_rather_than_a_zero(self):
        cur = {_BLACK: {"owned": 36.5}}
        assert demand_mod.ownership_delta_signals(cur, {}, _PLAYERS, 2) == {}


class TestDeriveAccumulates:
    def test_two_sources_on_one_player_are_two_facts(self):
        a = {"kaelon black:RB": (DemandSignal("rival_failed_claims", 3, "claims", "run", "", "3 rivals", True),)}
        b = {"kaelon black:RB": (DemandSignal("sleeper_trending_add", 41200, "leagues", "48h", "", "41,200"),)}
        merged = demand_mod.derive(a, b)
        assert len(merged.signals_for("Kaelon Black", "RB")) == 2

    def test_a_league_specific_signal_outranks_a_global_one(self):
        """Twelve managers who share your waiver wire are better evidence
        about your wire than a million who do not."""
        league = demand_mod.derive(
            {"a:RB": (DemandSignal("rival_failed_claims", 1, "claims", "r", "", "t", True),)}
        )
        globalish = demand_mod.derive(
            {"a:RB": (DemandSignal("sleeper_trending_add", 99999, "leagues", "48h", "", "t"),)}
        )
        assert league.strength("A", "RB") > globalish.strength("A", "RB")


class TestSpeculativeSelection:
    """The selection rule IS the feature: a row earns its place by evidence
    that other managers want the player, never by being the least-bad thing
    the math rejected."""

    def _trace(self, *names_with_demand):
        trace = weekmod.ScanTrace()
        for i, (name, has_demand) in enumerate(names_with_demand):
            trace.zero_gain_rows.append(
                weekmod.SpeculativeCandidate(
                    add_name=name, position="RB", week_delta=-float(i),
                    demand=(
                        (DemandSignal("rival_failed_claims", 3, "claims", "r", "", "3 rivals", True),)
                        if has_demand else ()
                    ),
                )
            )
        return trace

    def _demand(self, *names):
        return demand_mod.derive({
            f"{n.lower()}:RB": (DemandSignal("rival_failed_claims", 3, "claims", "r", "", "t", True),)
            for n in names
        })

    def test_no_demand_means_no_section(self):
        cfg = Config.load("config.yml")
        trace = self._trace(("Nobody Wants Him", False))
        rows = weekmod.speculative_candidates(trace, self._demand(), cfg, limit=5)
        assert rows == []

    def test_a_player_already_on_a_real_row_is_excluded(self):
        """A player the plan is telling you to claim must never also appear
        as a curiosity."""
        cfg = Config.load("config.yml")
        trace = self._trace(("Kaelon Black", True))
        rows = weekmod.speculative_candidates(
            trace, self._demand("Kaelon Black"), cfg,
            exclude_names={"Kaelon Black"}, limit=5,
        )
        assert rows == []

    def test_limit_zero_turns_the_section_off_entirely(self):
        cfg = Config.load("config.yml")
        trace = self._trace(("Kaelon Black", True))
        assert weekmod.speculative_candidates(trace, self._demand("Kaelon Black"), cfg, limit=0) == []

    def test_no_demand_object_at_all_is_an_exact_no_op(self):
        cfg = Config.load("config.yml")
        assert weekmod.speculative_candidates(self._trace(("X", True)), None, cfg, limit=5) == []
        assert weekmod.speculative_candidates(None, self._demand("X"), cfg, limit=5) == []


class TestSpeculativeCandidateCarriesNoExecutableNumber:
    """The absence IS the invariant -- see `SpeculativeCandidate`'s docstring.

    A consumer that wanted to rank these against real adds would have to
    invent a number to do it, and inventing that number is exactly the
    failure this section exists to avoid.
    """

    @pytest.mark.parametrize("forbidden", ["net", "value", "claim_cost", "urgency", "is_claim", "kind"])
    def test_the_fields_that_would_make_it_executable_do_not_exist(self, forbidden):
        c = weekmod.SpeculativeCandidate(add_name="X", position="RB")
        assert not hasattr(c, forbidden)

    def test_the_blend_ships_under_a_name_that_is_not_points(self):
        """`gain` is the ros/week blend -- an average of a season total and
        one week. Rendering it as points is how a +0.2 move once read as
        '+0.6' and got notified."""
        from ffbot.webapi import speculative_json

        payload = speculative_json(
            weekmod.SpeculativeCandidate(add_name="X", position="RB", gain=-1.25)
        )
        assert payload["rank_key_blend"] == -1.25
        assert "gain" not in payload and "net" not in payload and "value" not in payload
        json.dumps(payload)  # must survive the week log and the wire


class TestTheHeaderIsNotALie:
    """`gain <= 0` catches two different things and only one belongs here.

    Found by running the feature against live data: the first two rows it
    produced were backup quarterbacks -- C.J. Stroud at +10.5 points a week
    "over" the worst rostered player -- because a second QB adds nothing to
    a lineup that seats one. Correctly filtered from the recommendations,
    and nonsense under a header that says "below your worst rostered
    player". They also buried the case the section exists for.
    """

    def _row(self, name, week_delta, ros_delta, open_spot=False):
        from ffbot.demand import DemandSignal

        return weekmod.SpeculativeCandidate(
            add_name=name, position="RB", week_delta=week_delta,
            ros_delta_per_week=ros_delta, open_spot=open_spot,
            drop_name="" if open_spot else "Tyjae Spears",
            demand=(DemandSignal("rival_failed_claims", 3, "claims", "r", "", "t", True),),
        )

    def _demand(self, *names):
        return demand_mod.derive({
            f"{n.lower()}:RB": (DemandSignal("rival_failed_claims", 3, "claims", "r", "", "t", True),)
            for n in names
        })

    def test_a_backup_who_beats_your_worst_on_both_horizons_is_excluded(self):
        cfg = Config.load("config.yml")
        trace = weekmod.ScanTrace()
        trace.zero_gain_rows = [self._row("Cj Stroud", +10.5, +7.2)]
        rows = weekmod.speculative_candidates(trace, self._demand("Cj Stroud"), cfg, limit=5)
        assert rows == []

    def test_a_genuine_hot_unknown_is_kept(self):
        """The Kaelon Black shape: worse on both horizons, wanted anyway."""
        cfg = Config.load("config.yml")
        trace = weekmod.ScanTrace()
        trace.zero_gain_rows = [self._row("Kaelon Black", -0.7, -2.4)]
        rows = weekmod.speculative_candidates(trace, self._demand("Kaelon Black"), cfg, limit=5)
        assert [r.add_name for r in rows] == ["Kaelon Black"]

    def test_below_on_either_horizon_is_enough(self):
        """A player who beats your worst this week but loses over the rest
        of the season is exactly the sort of trade-off worth showing."""
        cfg = Config.load("config.yml")
        trace = weekmod.ScanTrace()
        trace.zero_gain_rows = [self._row("Caleb Douglas", +0.8, -0.4)]
        rows = weekmod.speculative_candidates(trace, self._demand("Caleb Douglas"), cfg, limit=5)
        assert [r.add_name for r in rows] == ["Caleb Douglas"]

    def test_an_open_roster_spot_exempts_the_rule(self):
        """There is no drop to be below, so the comparison the rule is about
        does not exist."""
        cfg = Config.load("config.yml")
        trace = weekmod.ScanTrace()
        trace.zero_gain_rows = [self._row("Free Seat", +5.0, +5.0, open_spot=True)]
        rows = weekmod.speculative_candidates(trace, self._demand("Free Seat"), cfg, limit=5)
        assert [r.add_name for r in rows] == ["Free Seat"]
