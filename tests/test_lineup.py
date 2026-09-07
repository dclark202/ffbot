from __future__ import annotations

from ffbot.config import Config
from ffbot.lineup import early_window_teams, optimize, score_player
from ffbot.models import BENCH

from .conftest import mk


class TestHeldInIr:
    """`LineupPlan.held_in_ir` -- the third group (alongside assignments/
    bench) a caller showing the WHOLE roster needs, since `optimize()`
    excludes IR-parked players from the other two entirely."""

    def test_ir_eligible_player_excluded_from_assignments_and_bench(self, cfg, standard_league):
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("Hurt Guy", "RB", "IR", 18, status="IR"),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert [p.name for p in plan.held_in_ir] == ["Hurt Guy"]
        assert "Hurt Guy" not in [p.name for _, p in plan.assignments]
        assert "Hurt Guy" not in [p.name for p in plan.bench]

    def test_no_ir_players_leaves_held_in_ir_empty(self, cfg, standard_league):
        players = [mk("QB1", "QB", "QB", 22)]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert plan.held_in_ir == []

    def test_recovered_player_no_longer_ir_eligible_rejoins_the_pool(self, cfg, standard_league):
        # selected_position is still "IR" but status is healthy -- not
        # IR-eligible anymore, so optimize() must pull them back into
        # consideration (assignments or bench) rather than parking them.
        players = [mk("Recovered", "QB", "IR", 22, status="")]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert plan.held_in_ir == []
        assert [p.name for _, p in plan.assignments] == ["Recovered"]


def slot_of(plan, name: str) -> str:
    """Where the plan puts `name` — the slot label, or BN."""
    for slot, p in plan.assignments:
        if p.name == name:
            return slot
    return BENCH


class TestNoop:
    def test_optimal_lineup_produces_no_moves(self, cfg, standard_league):
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("RB1", "RB", "RB", 18),
            mk("RB2", "RB", "RB", 14),
            mk("WR1", "WR", "WR", 17),
            mk("WR2", "WR", "WR", 13),
            mk("TE1", "TE", "TE", 11),
            mk("FLEX", "WR", "W/R/T", 12),
            mk("K1", "K", "K", 8),
            mk("D1", "DEF", "DEF", 7),
            mk("Bench1", "RB", BENCH, 4),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert plan.is_noop()
        assert plan.moves == []
        assert plan.unfilled_slots == []

    def test_rerunning_a_plan_is_stable(self, cfg, standard_league):
        """Applying the plan then re-optimizing must produce nothing further.

        This is what makes the frequent Actions ticks safe to repeat.
        """
        players = [
            mk("QB1", "QB", BENCH, 22),
            mk("RB1", "RB", BENCH, 18),
            mk("RB2", "RB", BENCH, 14),
            mk("WR1", "WR", BENCH, 17),
            mk("WR2", "WR", BENCH, 13),
            mk("TE1", "TE", BENCH, 11),
            mk("FLEX", "WR", BENCH, 12),
            mk("K1", "K", BENCH, 8),
            mk("D1", "DEF", BENCH, 7),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert plan.moves

        by_id = {p.player_id: p for p in players}
        for m in plan.moves:
            by_id[m.player.player_id].selected_position = m.to_slot

        assert optimize(players, standard_league, week=3, cfg=cfg).is_noop()


class TestHardBenchRules:
    def test_injured_starter_is_benched_for_a_worse_healthy_player(
        self, cfg, standard_league
    ):
        """The whole point of the Sunday sweep: OUT beats projection, always."""
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("StarRB", "RB", "RB", 25, status="O"),
            mk("RB2", "RB", "RB", 14),
            mk("ScrubRB", "RB", BENCH, 3),
            mk("WR1", "WR", "WR", 17),
            mk("WR2", "WR", "WR", 13),
            mk("TE1", "TE", "TE", 11),
            mk("FLEX", "WR", "W/R/T", 12),
            mk("K1", "K", "K", 8),
            mk("D1", "DEF", "DEF", 7),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)

        assert slot_of(plan, "StarRB") == BENCH
        assert slot_of(plan, "ScrubRB") == "RB"
        assert ("StarRB", "status O") in [(p.name, r) for p, r in plan.benched_for_cause]

    def test_bye_week_starter_is_benched(self, cfg, standard_league):
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("ByeRB", "RB", "RB", 25, bye_week=7),
            mk("RB2", "RB", "RB", 14),
            mk("ScrubRB", "RB", BENCH, 3),
            mk("WR1", "WR", "WR", 17),
            mk("WR2", "WR", "WR", 13),
            mk("TE1", "TE", "TE", 11),
            mk("FLEX", "WR", "W/R/T", 12),
            mk("K1", "K", "K", 8),
            mk("D1", "DEF", "DEF", 7),
        ]
        plan = optimize(players, standard_league, week=7, cfg=cfg)
        assert slot_of(plan, "ByeRB") == BENCH
        assert slot_of(plan, "ScrubRB") == "RB"

    def test_bye_week_player_is_startable_in_other_weeks(self, cfg, standard_league):
        players = [mk("ByeRB", "RB", BENCH, 25, bye_week=7)]
        assert optimize(players, {"RB": 1}, week=6, cfg=cfg).assignments[0][1].name == "ByeRB"

    def test_doubtful_is_benched_by_default(self, cfg):
        players = [mk("Hurt", "RB", "RB", 20, status="D"), mk("Fine", "RB", BENCH, 5)]
        plan = optimize(players, {"RB": 1}, week=3, cfg=cfg)
        assert slot_of(plan, "Fine") == "RB"

    def test_doubtful_can_be_downgraded_to_a_discount(self):
        cfg = Config()
        cfg.projection.doubtful_is_out = False
        players = [mk("Hurt", "RB", "RB", 20, status="D"), mk("Fine", "RB", BENCH, 5)]
        plan = optimize(players, {"RB": 1}, week=3, cfg=cfg)
        # 20 * 0.85 = 17, still comfortably ahead of 5.
        assert slot_of(plan, "Hurt") == "RB"

    def test_unfillable_slot_is_reported_not_filled_with_an_out_player(
        self, cfg, standard_league
    ):
        """An empty slot and an OUT player both score zero — say so rather than pretend."""
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("K1", "K", "K", 8, status="O"),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert "K" in plan.unfilled_slots
        assert slot_of(plan, "K1") == BENCH


class TestQuestionable:
    def test_questionable_is_discounted_not_banned(self, cfg):
        players = [mk("Q", "RB", BENCH, 20, status="Q"), mk("Healthy", "RB", BENCH, 15)]
        plan = optimize(players, {"RB": 1}, week=3, cfg=cfg)
        # 20 * 0.85 = 17 > 15
        assert slot_of(plan, "Q") == "RB"

    def test_discount_can_flip_a_close_call(self, cfg):
        players = [mk("Q", "RB", "RB", 20, status="Q"), mk("Healthy", "RB", BENCH, 18)]
        plan = optimize(players, {"RB": 1}, week=3, cfg=cfg)
        # 20 * 0.85 = 17 < 18
        assert slot_of(plan, "Healthy") == "RB"


class TestSlotEligibility:
    def test_flex_takes_the_best_remaining_eligible_player(self, cfg):
        layout = {"RB": 1, "WR": 1, "W/R/T": 1}
        players = [
            mk("RB1", "RB", BENCH, 20),
            mk("WR1", "WR", BENCH, 18),
            mk("TE1", "TE", BENCH, 16),
            mk("WR2", "WR", BENCH, 9),
        ]
        plan = optimize(players, layout, week=3, cfg=cfg)
        assert slot_of(plan, "TE1") == "W/R/T"
        assert slot_of(plan, "WR2") == BENCH

    def test_kicker_cannot_occupy_flex(self, cfg):
        plan = optimize([mk("K1", "K", BENCH, 50)], {"W/R/T": 1}, week=3, cfg=cfg)
        assert plan.assignments == []
        assert plan.unfilled_slots == ["W/R/T"]

    def test_superflex_accepts_a_quarterback(self, cfg):
        plan = optimize([mk("QB2", "QB", BENCH, 19)], {"Q/W/R/T": 1}, week=3, cfg=cfg)
        assert slot_of(plan, "QB2") == "Q/W/R/T"

    def test_multi_position_player_is_placed_where_it_helps(self, cfg):
        layout = {"RB": 1, "WR": 1}
        players = [
            mk("Swing", "RB,WR", BENCH, 15),
            mk("PureRB", "RB", BENCH, 20),
        ]
        plan = optimize(players, layout, week=3, cfg=cfg)
        assert slot_of(plan, "PureRB") == "RB"
        assert slot_of(plan, "Swing") == "WR"

    def test_augmenting_path_beats_naive_greedy(self, cfg):
        """The case a first-fit greedy gets wrong.

        Slot order puts the flex first. A naive pass seats the WR in the flex,
        then has nowhere for the RB and leaves 18 points on the bench. Correct
        play is to displace the WR into its own slot and give the flex to the
        RB — which requires following an augmenting path.
        """
        layout = {"W/R/T": 1, "WR": 1}
        players = [
            mk("TopWR", "WR", BENCH, 20),
            mk("OnlyRB", "RB", BENCH, 18),
        ]
        plan = optimize(players, layout, week=3, cfg=cfg)

        assert slot_of(plan, "TopWR") == "WR"
        assert slot_of(plan, "OnlyRB") == "W/R/T"
        assert len(plan.assignments) == 2


class TestInjuredReserve:
    def test_ir_player_stays_put(self, cfg, standard_league):
        players = [mk("Hurt", "RB", "IR", 0, status="IR"), mk("RB1", "RB", "RB", 12)]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert all(m.player.name != "Hurt" for m in plan.moves)

    def test_recovered_player_leaves_ir_and_can_start(self, cfg):
        """Status clears, so the IR slot no longer holds them."""
        players = [mk("Back", "RB", "IR", 19, status=""), mk("Scrub", "RB", "RB", 4)]
        plan = optimize(players, {"RB": 1, "BN": 3, "IR": 1}, week=3, cfg=cfg)
        assert slot_of(plan, "Back") == "RB"
        assert slot_of(plan, "Scrub") == BENCH


class TestScoring:
    def test_projection_is_preferred_when_present(self, cfg):
        p = mk("P", "RB", BENCH, 12.0, season_avg_points=3.0, recent_points=[1, 1, 1])
        assert score_player(p, 3, cfg) == 12.0

    def test_falls_back_to_blended_form_without_a_projection(self, cfg):
        p = mk("P", "RB", BENCH, None, season_avg_points=10.0, recent_points=[20, 20, 20])
        # 0.6 * 20 + 0.4 * 10
        assert score_player(p, 3, cfg) == 16.0

    def test_recency_window_limits_how_far_back_form_reaches(self, cfg):
        p = mk("P", "RB", BENCH, None, season_avg_points=0.0, recent_points=[99, 5, 5, 5])
        assert score_player(p, 3, cfg) == 3.0  # 0.6 * 5, the 99 falls outside

    def test_no_data_scores_zero_rather_than_crashing(self, cfg):
        assert score_player(mk("Rookie", "RB", BENCH, None), 3, cfg) == 0.0

    def test_out_player_has_no_score(self, cfg):
        assert score_player(mk("P", "RB", BENCH, 20, status="O"), 3, cfg) is None


class TestPayload:
    def test_changes_payload_shape(self, cfg):
        players = [mk("A", "RB", BENCH, 20), mk("B", "RB", "RB", 5)]
        plan = optimize(players, {"RB": 1, "BN": 1}, week=3, cfg=cfg)
        changes = plan.as_lineup_write()

        assert {c["selected_position"] for c in changes} == {"RB", BENCH}
        assert all(set(c) == {"player_id", "selected_position"} for c in changes)

    def test_both_sides_of_a_swap_are_included(self, cfg):
        """A real write API would need the full changed set, not just the
        incoming player, to accept the resulting roster (see
        LineupPlan.as_lineup_write's docstring)."""
        players = [mk("In", "RB", BENCH, 20), mk("Out", "RB", "RB", 5)]
        plan = optimize(players, {"RB": 1, "BN": 1}, week=3, cfg=cfg)
        assert len(plan.as_lineup_write()) == 2


class TestNoChurn:
    """Equivalent slot arrangements score the same, so don't shuffle for nothing.

    Every needless move is a write to Yahoo and a line of noise in the audit log.
    """

    def test_equivalent_slots_are_left_alone(self, cfg):
        players = [
            mk("WrInWr", "WR", "WR", 15),
            mk("WrInFlex", "WR", "W/R/T", 12),
        ]
        assert optimize(players, {"WR": 1, "W/R/T": 1}, week=3, cfg=cfg).is_noop()

    def test_left_alone_even_when_slot_order_would_favour_a_swap(self, cfg):
        """Flex listed first — a fresh matching would seat the top WR there."""
        players = [
            mk("WrInWr", "WR", "WR", 15),
            mk("WrInFlex", "WR", "W/R/T", 12),
        ]
        assert optimize(players, {"W/R/T": 1, "WR": 1}, week=3, cfg=cfg).is_noop()

    def test_only_the_necessary_players_move(self, cfg, standard_league):
        """One injured starter should mean one swap, not a rebuilt lineup."""
        players = [
            mk("QB1", "QB", "QB", 22),
            mk("RB1", "RB", "RB", 18),
            mk("RB2", "RB", "RB", 14, status="O"),
            mk("RB3", "RB", BENCH, 6),
            mk("WR1", "WR", "WR", 17),
            mk("WR2", "WR", "WR", 13),
            mk("TE1", "TE", "TE", 11),
            mk("FLEX", "WR", "W/R/T", 12),
            mk("K1", "K", "K", 8),
            mk("D1", "DEF", "DEF", 7),
        ]
        plan = optimize(players, standard_league, week=3, cfg=cfg)
        assert {m.player.name for m in plan.moves} == {"RB2", "RB3"}


class TestOptimalityAgainstBruteForce:
    """Cross-check the matroid argument against exhaustive search.

    The optimizer's correctness rests on a non-obvious claim, so verify it
    empirically over randomised rosters rather than trusting the reasoning.
    """

    @staticmethod
    def _brute_force_best(players, slots, cfg, week):
        import itertools

        from ffbot.models import slot_accepts as accepts

        scores = {p.player_id: score_player(p, week, cfg) for p in players}
        startable = [p for p in players if scores[p.player_id] is not None]

        # One None per slot, so leaving a slot empty is always representable —
        # a roster with no eligible kicker still has a best arrangement.
        n = len(slots)
        padded = list(startable) + [None] * n
        best = 0.0
        for combo in itertools.permutations(padded, n):
            total, ok = 0.0, True
            for slot, p in zip(slots, combo):
                if p is None:
                    continue
                if not accepts(slot, p):
                    ok = False
                    break
                total += scores[p.player_id]
            if ok:
                best = max(best, total)
        return best

    def test_matches_exhaustive_search_on_random_rosters(self, cfg):
        import random

        from ffbot.models import starting_slots

        rng = random.Random(20260806)
        layouts = [
            {"RB": 1, "WR": 1, "W/R/T": 1},
            {"QB": 1, "RB": 2, "W/R/T": 1},
            {"WR": 2, "W/R/T": 1, "TE": 1},
            {"Q/W/R/T": 1, "RB": 1, "WR": 1},
        ]
        pos_choices = ["QB", "RB", "WR", "TE", "RB,WR", "WR,TE", "K"]

        for trial in range(150):
            layout = rng.choice(layouts)
            slots = starting_slots(layout)
            players = [
                mk(
                    f"P{trial}_{i}",
                    rng.choice(pos_choices),
                    BENCH,
                    round(rng.uniform(0, 25), 1),
                    status=rng.choice(["", "", "", "", "O"]),
                )
                for i in range(rng.randint(3, 6))
            ]

            plan = optimize(players, layout, week=3, cfg=cfg)
            got = sum(score_player(p, 3, cfg) for _, p in plan.assignments)
            want = self._brute_force_best(players, slots, cfg, 3)

            assert got == want or abs(got - want) < 1e-9, (
                f"trial {trial}: optimizer scored {got}, best possible {want}"
            )


# --- Flex seating -----------------------------------------------------------
#
# Every perfect matching of the chosen starters scores identically, so points
# cannot pick between them. These pin the rule that does -- see
# `lineup._flex_seating_order`.

_FLEX_LAYOUT = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 5}


def _seated(plan) -> dict[str, str]:
    """{slot: player name}. The two WR slots collapse, which is the point:
    slots compare by NAME, so moving between them is not a move at all."""
    return {slot: p.name for slot, p in plan.assignments}


def _flex(plan) -> str:
    return _seated(plan)["W/R/T"]


def _full_roster(**overrides):
    """A legal starting nine plus the flex, every slot already filled, so the
    only thing left to decide is the seating."""
    base = dict(
        qb=mk("Quarterback", "QB", "QB", 22.0),
        star=mk("Star Receiver", "WR", "WR", 19.5, team="SEA"),
        mid=mk("Middling Receiver", "WR", "WR", 11.0, team="CHI"),
        marginal=mk("Marginal Receiver", "WR", "W/R/T", 9.1, team="NYG"),
        rb1=mk("Back One", "RB", "RB", 15.0, team="DAL"),
        rb2=mk("Back Two", "RB", "RB", 13.0, team="DAL"),
        te=mk("Tight End", "TE", "TE", 8.0, team="KC"),
        k=mk("Kicker", "K", "K", 8.0),
        dst=mk("Defense", "DEF", "DEF", 7.0),
    )
    base.update(overrides)
    return base


_MAIN_BLOCK = {
    "SEA": "2026-09-13T13:00",
    "CHI": "2026-09-13T13:00",
    "DAL": "2026-09-13T13:00",
    "KC": "2026-09-13T13:00",
}


class TestFlexSeating:
    def test_the_most_replaceable_starter_holds_the_flex(self, cfg):
        r = _full_roster()
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)
        assert _flex(plan) == "Marginal Receiver"

    def test_a_star_already_sitting_in_the_flex_is_moved_out(self, cfg):
        # THE regression. A roster imported from Sleeper arrives pre-seated,
        # so the old minimal-move pass just ratified whatever was there and
        # reported no moves at all -- silently endorsing the worst seating.
        r = _full_roster(
            star=mk("Star Receiver", "WR", "W/R/T", 19.5, team="SEA"),
            marginal=mk("Marginal Receiver", "WR", "WR", 9.1, team="NYG"),
        )
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)
        assert _flex(plan) == "Marginal Receiver"
        assert plan.moves, "a bad seating must not be silently endorsed"

    def test_the_reseat_is_labelled_as_costing_no_points(self, cfg):
        r = _full_roster(
            star=mk("Star Receiver", "WR", "W/R/T", 19.5, team="SEA"),
            marginal=mk("Marginal Receiver", "WR", "WR", 9.1, team="NYG"),
        )
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)
        reasons = {m.player.name: m.reason for m in plan.moves}
        assert "no points change" in reasons["Star Receiver"]
        assert "no points change" in reasons["Marginal Receiver"]
        # ...and never labelled with a projection, the way a genuine
        # promotion off the bench is.
        assert not any(text.startswith("proj ") for text in reasons.values())

    def test_seating_never_changes_who_starts_or_the_total(self, cfg):
        # The invariant the whole rule rests on: it reorders seats, never the
        # lineup. Same set, same points, whatever the incoming seating.
        good = _full_roster()
        bad = _full_roster(
            star=mk("Star Receiver", "WR", "W/R/T", 19.5, team="SEA"),
            marginal=mk("Marginal Receiver", "WR", "WR", 9.1, team="NYG"),
        )
        a = optimize(list(good.values()), _FLEX_LAYOUT, 5, cfg)
        b = optimize(list(bad.values()), _FLEX_LAYOUT, 5, cfg)
        assert {p.name for _, p in a.assignments} == {p.name for _, p in b.assignments}
        assert sum(p.projected_points for _, p in a.assignments) == sum(
            p.projected_points for _, p in b.assignments
        )

    def test_an_early_kickoff_is_kept_out_of_the_flex(self, cfg):
        # The conflict case. NYG is the most marginal starter, so the
        # projection rule alone would seat him in the flex -- but he plays
        # Thursday, and parking him there locks the only versatile slot for
        # the whole week. CHI is 1.9 points less marginal and takes it.
        r = _full_roster()
        kickoffs = dict(_MAIN_BLOCK, NYG="2026-09-10T20:15")
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg, kickoffs=kickoffs)
        assert _flex(plan) == "Middling Receiver"

    def test_a_late_kickoff_is_not_treated_as_early(self, cfg):
        # Sunday night and Monday night are LATER than the main block, so
        # they must not be swept up by the early-window rule.
        r = _full_roster()
        kickoffs = dict(_MAIN_BLOCK, NYG="2026-09-14T20:15")
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg, kickoffs=kickoffs)
        assert _flex(plan) == "Marginal Receiver"

    def test_the_early_reseat_says_why(self, cfg):
        r = _full_roster()
        kickoffs = dict(_MAIN_BLOCK, NYG="2026-09-10T20:15")
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg, kickoffs=kickoffs)
        reasons = {m.player.name: m.reason for m in plan.moves}
        assert "before the main block" in reasons["Marginal Receiver"]

    def test_no_kickoffs_falls_back_to_the_projection_rule(self, cfg):
        # Every season-long caller (draft.need, board, denial) passes none.
        r = _full_roster()
        assert _flex(optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)) == "Marginal Receiver"
        assert _flex(
            optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg, kickoffs={})
        ) == "Marginal Receiver"

    def test_a_correct_seating_is_left_alone(self, cfg):
        # The minimal-move preference still does its job: nothing to fix
        # means nothing to report.
        r = _full_roster()
        assert optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg).moves == []

    def test_seating_is_stable_across_runs(self, cfg):
        r = _full_roster()
        first = _seated(optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg))
        for _ in range(5):
            assert _seated(optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)) == first

    def test_a_forced_flex_occupant_is_the_least_valuable_starter(self, cfg):
        # Swap the second back for a fourth receiver: now four WRs compete
        # for two WR slots and the flex, and the second RB slot goes unfilled.
        # The flex must hold the least valuable player who actually STARTS
        # (Middling, 11.0) -- Marginal (9.1) is the one left out entirely,
        # since the empty RB slot cannot take him.
        r = _full_roster(rb2=mk("Third Receiver", "WR", "BN", 12.0, team="LAR"))
        plan = optimize(list(r.values()), _FLEX_LAYOUT, 5, cfg)
        assert _flex(plan) == "Middling Receiver"
        assert "Marginal Receiver" in {p.name for p in plan.bench}
        assert plan.unfilled_slots == ["RB"]


class TestEarlyWindowTeams:
    def test_the_modal_kickoff_is_the_main_block(self):
        early = early_window_teams({
            "NYG": "2026-09-10T20:15",   # Thursday night
            "JAX": "2026-09-13T09:30",   # London, Sunday morning
            "SEA": "2026-09-13T13:00",
            "CHI": "2026-09-13T13:00",
            "DAL": "2026-09-13T13:00",
            "KC": "2026-09-13T16:25",
            "SF": "2026-09-14T20:15",    # Monday night
        })
        assert early == frozenset({"NYG", "JAX"})

    def test_no_schedule_means_no_early_teams(self):
        assert early_window_teams(None) == frozenset()
        assert early_window_teams({}) == frozenset()
        assert early_window_teams({"NYG": "", "SEA": None}) == frozenset()

    def test_a_slate_with_one_kickoff_time_has_no_early_window(self):
        assert early_window_teams(
            {"A": "2026-09-13T13:00", "B": "2026-09-13T13:00"}
        ) == frozenset()

    def test_a_frequency_tie_breaks_toward_the_earlier_time(self):
        # A tie must only ever SHRINK the early set, never invent one: with no
        # clear main block, treating the earlier time as the block means
        # nothing gets flagged.
        assert early_window_teams(
            {"A": "2026-09-13T13:00", "B": "2026-09-13T16:25"}
        ) == frozenset()


class TestReseatBetweenDedicatedSlots:
    """A multi-position-eligible player can be reseated between two dedicated
    slots to free the flex for someone else. That move involves no flex on
    either end, so it must not be explained as one."""

    def test_the_reason_does_not_claim_a_flex_was_involved(self, cfg):
        layout = {"WR": 1, "RB": 1, "W/R/T": 1, "BN": 3}
        roster = [
            # Eligible at both RB and WR, currently in the RB slot.
            mk("Swing Back", "RB,WR", "RB", 14.0, team="DAL"),
            mk("Pure Back", "RB", "BN", 13.0, team="ATL"),
            mk("Spare Receiver", "WR", "W/R/T", 6.0, team="NYG"),
        ]
        plan = optimize(roster, layout, 5, cfg)
        reasons = {m.player.name: m.reason for m in plan.moves}
        swing = reasons.get("Swing Back")
        if swing is not None:
            assert "out of flex" not in swing
            assert "no points change" in swing
