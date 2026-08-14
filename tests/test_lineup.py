from __future__ import annotations

from ffbot.config import Config
from ffbot.lineup import optimize, score_player
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
