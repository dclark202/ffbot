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


class TestAStreamedPositionIsPricedAgainstItsIncumbent:
    """You never roster two kickers, so a kicker is not paid for by your
    worst running back.

    The first live MONITOR section said "K Matt Gay -0.6 vs Tyjae Spears",
    comparing two players who never compete for anything and reading as a
    suggestion to drop a back for a second kicker. The manager caught it
    (2026-09-16). The incumbent rule is the one
    `gameplan._stream_swap_rows` already uses for real stream rows.
    """

    LAYOUT = {"QB": 1, "RB": 2, "K": 1, "BN": 2}

    def _fixture(self):
        from ffbot.board import Board
        from ffbot.config import DraftConfig, SeasonConfig
        from ffbot.models import Player
        from tests.conftest import mk_bp

        players = [
            mk_bp("My Qb", "QB", points=300.0, team="BUF", rank=1, vor=200.0),
            mk_bp("My Rb", "RB", points=250.0, team="SF", rank=2, vor=180.0),
            mk_bp("My Rb Two", "RB", points=200.0, team="BAL", rank=3, vor=130.0),
            mk_bp("My Kicker", "K", points=100.0, team="JAX", rank=6, vor=10.0),
            mk_bp("Bench Rb", "RB", points=70.0, team="NYJ", rank=40, vor=2.0),
            mk_bp("Wire Kicker", "K", points=95.0, team="LV", rank=44, vor=5.0),
            mk_bp("Wire Rb", "RB", points=20.0, team="CHI", rank=60, vor=-40.0),
        ]
        board = Board(
            players=players, by_key={p.key: p for p in players},
            replacement={"RB": 60.0, "QB": 120.0, "K": 40.0},
            starters_per_pos={}, tier_last={},
        )
        roster = [
            Player(player_id=1, name="My Qb", eligible_positions=["QB"], selected_position="QB",
                   team="BUF", projected_points=18.0),
            Player(player_id=2, name="My Rb", eligible_positions=["RB"], selected_position="RB",
                   team="SF", projected_points=15.0),
            Player(player_id=3, name="My Rb Two", eligible_positions=["RB"], selected_position="RB",
                   team="BAL", projected_points=12.0),
            Player(player_id=4, name="My Kicker", eligible_positions=["K"], selected_position="K",
                   team="JAX", projected_points=8.0),
            Player(player_id=5, name="Bench Rb", eligible_positions=["RB"], selected_position="BN",
                   team="NYJ", projected_points=4.0),
        ]
        cfg = Config(
            roster_positions=self.LAYOUT,
            season=SeasonConfig(waiver_pool_size=50, stream_positions=["K"], noise_floor_weight=0.0),
            draft=DraftConfig(num_teams=12),
        )
        return roster, board, cfg

    def test_a_kicker_is_compared_to_your_kicker_not_your_worst_bench_player(self):
        roster, board, cfg = self._fixture()
        trace = weekmod.ScanTrace()
        weekmod.waiver_candidates(
            roster, board, self.LAYOUT, cfg, my_priority=6, week=3, limit=10_000, trace=trace,
        )
        kicker = next(
            (c for c in trace.zero_gain_rows if c.position == "K"), None,
        )
        assert kicker is not None, "the wire kicker should have been scanned and filtered"
        assert kicker.drop_name == "My Kicker"
        assert kicker.drop_name != "Bench Rb"
        assert kicker.week_delta == pytest.approx(kicker.week_proj - 8.0)

    def test_a_non_streamed_position_still_costs_the_worst_droppable(self):
        roster, board, cfg = self._fixture()
        trace = weekmod.ScanTrace()
        weekmod.waiver_candidates(
            roster, board, self.LAYOUT, cfg, my_priority=6, week=3, limit=10_000, trace=trace,
        )
        non_k = [c for c in trace.zero_gain_rows if c.position != "K"]
        assert non_k, "the fixture should filter at least one non-kicker"
        assert all(c.drop_name != "My Kicker" for c in non_k)


class TestMonitorShowsOnePlayerPerPosition:
    """A streamed position always has several near-identical candidates on
    the wire. Without one-per-position the section fills with defenses and
    crowds out the one genuinely novel player -- the 2026-09-15 "four
    defenses for one drop" pathology in a different hat."""

    def _rows(self, *specs):
        trace = weekmod.ScanTrace()
        for name, pos in specs:
            trace.zero_gain_rows.append(
                weekmod.SpeculativeCandidate(
                    add_name=name, position=pos, week_delta=-1.0,
                    demand=(DemandSignal("ownership_delta", 20.0, "pct_owned", "w", "", "t"),),
                )
            )
        return trace

    def test_two_defenses_and_a_receiver_shows_one_defense_and_the_receiver(self):
        cfg = Config.load("config.yml")
        trace = self._rows(("Def One", "DEF"), ("Def Two", "DEF"), ("Some Wr", "WR"))
        demand = demand_mod.derive({
            f"{n.lower()}:{p}": (DemandSignal("ownership_delta", 20.0, "pct_owned", "w", "", "t"),)
            for n, p in (("Def One", "DEF"), ("Def Two", "DEF"), ("Some Wr", "WR"))
        })
        rows = weekmod.speculative_candidates(trace, demand, cfg, limit=2)
        assert {r.position for r in rows} == {"DEF", "WR"}


class TestStrengthScalesWithHowMuchTheSignalMoved:
    """A defense the league moved 49 points of ownership on must outrank one
    it moved 7 points on. Counting WHICH signals fired rather than how far
    they moved put them in the wrong order on the first live run."""

    def _owned(self, pct):
        return demand_mod.derive({
            "x:DEF": (DemandSignal("ownership_delta", pct, "pct_owned", "w", "", "t"),)
        })

    def test_a_bigger_ownership_move_ranks_higher(self):
        assert self._owned(49.0).strength("X", "DEF") > self._owned(7.0).strength("X", "DEF")

    def test_it_saturates_rather_than_running_away(self):
        """An unbounded term would let one enormous trending count swamp a
        league-specific claim, which is the ordering this exists to keep."""
        huge = demand_mod.derive({
            "x:DEF": (DemandSignal("sleeper_trending_add", 9_000_000.0, "leagues", "48h", "", "t"),)
        })
        claimed = demand_mod.derive({
            "x:DEF": (DemandSignal("rival_failed_claims", 3.0, "claims", "r", "", "t", True),)
        })
        assert claimed.strength("X", "DEF") > huge.strength("X", "DEF")


class TestALiveSlateDoesNotChooseTheDrop:
    """2026-09-20, 16:05 on a Sunday: the pre-kickoff push carried

        MONITOR
          DEF Tampa Bay Buccaneers  -9.1 vs Kansas City Chiefs  +72% owned
          WR Xavier Hutchinson      -9.1 vs Kansas City Chiefs  +15% owned

    and the manager's reading of it was "it's recommending to monitor
    dropping my *only* defense for a backup WR."

    He was right, and two separate things had gone wrong.

    THE DROP. `drops.protect_pct_owned` (60) already held twelve of the
    fourteen rostered players undroppable; the survivors were Tyjae Spears
    and the Kansas City defense. Spears's game had kicked off, so
    `policy.can_drop` refused him too and `ranked_droppable` returned
    `[Kansas City]`. Its `[0]` -- labelled "worst hold value on your
    roster" -- was therefore the only defense on the roster, carrying a
    `hold_margin` of 107.5 precisely BECAUSE dropping him empties the DEF
    slot. A lock is a fact about the clock, not about value, so the
    speculative rows now rank the drop with `ignore_game_locks=True`.

    The fixture below reproduces the mechanism with locks alone; the
    ownership guard is just another way to arrive at the same short list.

    THE NUMBER. Both players' games had kicked off, so the scan zeroed
    their this-week points and `week_delta` became `-drop_week_proj` -- the
    incumbent's projection, negated. That is why two unrelated players
    printed the identical -9.1, and why an already-played candidate gets no
    row at all now.
    """

    LAYOUT = {"QB": 1, "RB": 2, "DEF": 1, "BN": 2}

    def _fixture(self, lock_the_bench: bool):
        from ffbot.board import Board
        from ffbot.config import DraftConfig, SeasonConfig
        from ffbot.models import Player
        from tests.conftest import mk_bp

        players = [
            mk_bp("My Qb", "QB", points=300.0, team="BUF", rank=1, vor=200.0),
            mk_bp("My Rb", "RB", points=250.0, team="SF", rank=2, vor=180.0),
            mk_bp("My Rb Two", "RB", points=200.0, team="BAL", rank=3, vor=130.0),
            mk_bp("My Defense", "DEF", points=105.0, team="KC", rank=6, vor=12.0),
            mk_bp("Bench Rb", "RB", points=70.0, team="NYJ", rank=40, vor=2.0),
            mk_bp("Bench Wr", "WR", points=120.0, team="DAL", rank=25, vor=30.0),
            mk_bp("Wire Wr", "WR", points=58.0, team="HOU", rank=61, vor=-30.0),
            mk_bp("Wire Defense", "DEF", points=98.0, team="TB", rank=70, vor=5.0),
        ]
        board = Board(
            players=players, by_key={p.key: p for p in players},
            replacement={"RB": 60.0, "QB": 120.0, "DEF": 40.0, "WR": 80.0},
            starters_per_pos={}, tier_last={},
        )
        roster = [
            Player(player_id=1, name="My Qb", eligible_positions=["QB"], selected_position="QB",
                   team="BUF", projected_points=18.0, game_locked=lock_the_bench),
            Player(player_id=2, name="My Rb", eligible_positions=["RB"], selected_position="RB",
                   team="SF", projected_points=15.0, game_locked=lock_the_bench),
            Player(player_id=3, name="My Rb Two", eligible_positions=["RB"], selected_position="RB",
                   team="BAL", projected_points=12.0, game_locked=lock_the_bench),
            # The late game: unplayed, and therefore the ONLY player the
            # lock filter leaves droppable.
            Player(player_id=4, name="My Defense", eligible_positions=["DEF"],
                   selected_position="DEF", team="KC", projected_points=9.1),
            Player(player_id=5, name="Bench Rb", eligible_positions=["RB"], selected_position="BN",
                   team="NYJ", projected_points=4.0, game_locked=lock_the_bench),
            # Fills the roster, so the rows have a drop to be below at all.
            Player(player_id=6, name="Bench Wr", eligible_positions=["WR"], selected_position="BN",
                   team="DAL", projected_points=11.0, game_locked=lock_the_bench),
        ]
        cfg = Config(
            roster_positions=self.LAYOUT,
            season=SeasonConfig(waiver_pool_size=50, stream_positions=["DEF"], noise_floor_weight=0.0),
            draft=DraftConfig(num_teams=12),
        )
        return roster, board, cfg

    def _wr_row(self, lock_the_bench):
        roster, board, cfg = self._fixture(lock_the_bench)
        trace = weekmod.ScanTrace()
        weekmod.waiver_candidates(
            roster, board, self.LAYOUT, cfg, my_priority=6, week=3, limit=10_000, trace=trace,
        )
        return next((c for c in trace.zero_gain_rows if c.add_name == "Wire Wr"), None)

    def test_a_receiver_is_not_priced_against_your_only_defense_mid_slate(self):
        row = self._wr_row(lock_the_bench=True)
        assert row is not None, "the wire receiver should have been scanned and filtered"
        assert row.drop_name == "Bench Rb", (
            "the drop is the worst player on the roster, not the only one the "
            "clock still allows you to move"
        )

    def test_the_locked_slate_gives_the_same_answer_as_the_morning_did(self):
        """The whole point: the value question has one answer all Sunday."""
        assert self._wr_row(lock_the_bench=True).drop_name == self._wr_row(
            lock_the_bench=False
        ).drop_name

    def test_the_reported_hold_margin_belongs_to_the_named_drop(self):
        """It read `best_drop_key`'s margin regardless of whose name the row
        carried, so an incumbent-priced row quoted a different player's
        number."""
        roster, board, cfg = self._fixture(lock_the_bench=False)
        trace = weekmod.ScanTrace()
        weekmod.waiver_candidates(
            roster, board, self.LAYOUT, cfg, my_priority=6, week=3, limit=10_000, trace=trace,
        )
        keys, _ = weekmod.roster_board_keys(roster, board)
        by_name = {p.name: p for p in roster}
        seen = set()
        for row in trace.zero_gain_rows:
            if not row.drop_name:
                continue
            drop = by_name[row.drop_name]
            key = f"{weekmod.normalize_name(drop.name)}:{row.drop_position}"
            seen.add(row.drop_name)
            assert row.drop_hold_margin == pytest.approx(
                weekmod.hold_margin(key, keys, board, cfg, drop.blocking)
            ), f"{row.add_name}'s row quotes a margin that is not {row.drop_name}'s"
        assert len(seen) > 1, (
            "the fixture must produce both an incumbent-priced row and a "
            "shared-drop one, or this asserts nothing"
        )

    def test_a_player_whose_game_has_started_gets_no_row(self):
        """His this-week number is `0 - drop_week_proj`: a fact about the
        incumbent. Two unrelated players printed the identical -9.1."""
        from ffbot.availability import PlayerAvailability

        cfg = Config.load("config.yml")
        trace = weekmod.ScanTrace()
        signal = DemandSignal("ownership_delta", 70.0, "pct_owned", "w", "", "t")
        trace.zero_gain_rows = [
            weekmod.SpeculativeCandidate(
                add_name="Played Wr", position="WR", week_delta=-9.1,
                ros_delta_per_week=-2.8, drop_name="My Defense",
                demand=(signal,), availability=PlayerAvailability(game_started=True),
            ),
            weekmod.SpeculativeCandidate(
                add_name="Late Rb", position="RB", week_delta=-1.2,
                ros_delta_per_week=-0.4, drop_name="Bench Rb",
                demand=(signal,), availability=PlayerAvailability(game_started=False),
            ),
        ]
        demand = demand_mod.derive({
            "played wr:WR": (signal,), "late rb:RB": (signal,),
        })
        rows = weekmod.speculative_candidates(trace, demand, cfg, limit=5)
        assert [r.add_name for r in rows] == ["Late Rb"]
