"""What a human sees must be checkable against the Sleeper app, and a number
too small to mean anything must not read like a real call.

Traced from 2026-09-13: Trevor Lawrence showed 15.2 where Sleeper showed 18.9
(a researched 40 mph wind cut, invisible), a finished game still showed a
projection, and a +0.1 DEF sidegrade was typed "Add & start".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ffbot import week
from ffbot.availability import Availability
from ffbot.config import SeasonConfig
from ffbot.gameplan import MetricsIndex, build_gameplan, pair_moves
from ffbot.lineup import LineupPlan, Move
from ffbot.names import normalize_name
from tests.conftest import mk, mk_bp
from tests.test_gameplan import WEEK_NUM, _demo_shaped_loaded, _loaded


def _windy_week() -> week.WeeklyIntel:
    return week.WeeklyIntel(games={
        "JAX": week.GameInfo(opponent="CLE", home=True, wind_mph=40, precip_pct=40, team_total=24.0, opp_total=15.5),
        "CLE": week.GameInfo(opponent="JAX", home=False, wind_mph=40, precip_pct=40, team_total=15.5, opp_total=24.0),
    })


class TestAdjustmentBreakdown:
    def _run(self):
        qb = mk("Trevor Lawrence", "QB", slot="QB", team="JAX", proj=18.91)
        stadiums = {"JAX": week.StadiumInfo(dome=False)}
        cfg = SeasonConfig(weather_weight=0.25, vegas_weight=0.20)
        return [qb], week.adjusted_players_with_breakdown([qb], _windy_week(), cfg, stadiums), cfg, stadiums

    def test_deltas_sum_to_the_adjustment(self):
        [qb], (adjusted, breakdown), _cfg, _st = self._run()
        steps = breakdown[normalize_name(qb.name)]
        assert sum(d for _, d in steps) == pytest.approx(adjusted[0].projected_points - qb.projected_points)

    def test_the_wind_cut_is_named_and_flagged_out_of_range(self):
        [qb], (_adjusted, breakdown), _cfg, _st = self._run()
        labels = [label for label, _ in breakdown[normalize_name(qb.name)]]
        assert any("wind 40 mph" in l and "above calibrated range" in l for l in labels)

    def test_breakdown_leaves_the_optimizer_input_bit_identical(self):
        [qb], (adjusted, _b), cfg, stadiums = self._run()
        plain = week.adjusted_players([qb], _windy_week(), cfg, stadiums)
        assert [p.projected_points for p in plain] == [p.projected_points for p in adjusted]


class TestMetricsCarrySleepersNumber:
    def test_rostered_player_shows_sleeper_and_ours(self):
        raw = mk("Trevor Lawrence", "QB", slot="QB", team="JAX", proj=18.91)
        adjusted = mk("Trevor Lawrence", "QB", slot="QB", team="JAX", proj=15.16)
        index = MetricsIndex(
            raw_week_points={normalize_name(raw.name): 18.91},
            adjustments={normalize_name(raw.name): [("weather: wind 40 mph", -3.75)]},
        )
        m = index.for_player(adjusted, week=1)
        assert m.sleeper_proj == 18.91
        assert m.week_proj == 15.16
        assert m.adjustments == (("weather: wind 40 mph", -3.75),)

    def test_a_finished_game_shows_its_real_score(self):
        now = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)
        avail = Availability(now=now, kickoffs={"SEA": now - timedelta(days=4), "KC": now + timedelta(days=1)})
        index = MetricsIndex(
            live_points={normalize_name("Jaxon Smith-Njigba"): 13.2, normalize_name("Rashee Rice"): 0.0},
            game_states=avail.game_states(),
        )
        jsn = index.for_player(mk("Jaxon Smith-Njigba", "WR", slot="WR", team="SEA", proj=19.2), week=1)
        rice = index.for_player(mk("Rashee Rice", "WR", slot="WR", team="KC", proj=12.9), week=1)
        assert (jsn.game_state, jsn.live_pts) == ("FINAL", 13.2)
        assert (rice.game_state, rice.live_pts) == ("", None)  # not kicked off: no "0.0 live"


class TestLivePointsAreDescriptiveOnly:
    def test_populating_them_changes_no_recommendation(self):
        plain = _loaded()
        live = _loaded()
        live.live_points = {normalize_name(bp.name): bp.points * 3.0 for bp in live.board.players}
        a = build_gameplan(plain, WEEK_NUM, plain.players, my_priority=6)
        b = build_gameplan(live, WEEK_NUM, live.players, my_priority=6)
        assert [r.text for r in a.adds + a.claims] == [r.text for r in b.adds + b.claims]
        assert [l.text for l in a.start_sit] == [l.text for l in b.start_sit]


class TestTossUpLabel:
    def _plan(self, start_pts: float, bench_pts: float) -> LineupPlan:
        starter = mk("Jalen Coker", "WR", slot="BN", team="CAR", proj=start_pts)
        benched = mk("Parker Washington", "WR", slot="WR", team="JAX", proj=bench_pts)
        return LineupPlan(moves=[
            Move(starter, from_slot="BN", to_slot="WR", reason=f"proj {start_pts}"),
            Move(benched, from_slot="WR", to_slot="BN", reason=f"outscored (proj {bench_pts})"),
        ])

    def test_a_sub_floor_swap_is_labelled_and_kept(self):
        [line] = pair_moves(self._plan(11.9, 11.6), {"WR": 1, "BN": 1}, noise_floor_for=lambda pos: 1.7)
        assert line.kind == "swap"
        assert "toss-up (+0.3 this week, inside the 1.7-point noise floor)" in line.text

    def test_a_real_swap_is_not(self):
        [line] = pair_moves(self._plan(15.0, 11.6), {"WR": 1, "BN": 1}, noise_floor_for=lambda pos: 1.7)
        assert line.toss_up_note == ""

    def test_no_floor_means_no_label(self):
        [line] = pair_moves(self._plan(11.9, 11.6), {"WR": 1, "BN": 1})
        assert line.toss_up_note == ""


class TestTheFloorAtTheShippedValue:
    def _floored(self, weight: float):
        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"))
        loaded.cfg.season.noise_floor_weight = weight
        loaded.board.predictiveness = {
            "QB": 0.385, "RB": 0.406, "WR": 0.423, "TE": 0.502, "K": 0.199, "DEF": 0.232,
        }
        return loaded

    def test_a_legitimate_claim_survives_it(self):
        loaded = self._floored(0.10)
        gem = mk_bp("Waiver Gem", "RB", points=300.0, team="CHI", bye_week=8, rank=3, vor=250.0)
        loaded.board.players.append(gem)
        loaded.board.by_key[gem.key] = gem
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=5)
        assert any(r.add_name == "Waiver Gem" for r in plan.claims + plan.adds)

    def test_the_flat_def_sidegrade_does_not(self):
        loaded = self._floored(0.10)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=5)
        assert not [r for r in plan.adds + plan.claims if r.position == "DEF" and not r.forced_need]
        assert any("noise floor" in n for n in plan.notes)


class TestLockedPlayersAreNotMoved:
    """2026-09-13, verifying this change live: "Add & start Matt Gay — Drop
    Cam Little" while Little's game was in progress. Sleeper locks a player
    at kickoff; a move it won't allow is not a recommendation."""

    def test_can_drop_refuses_a_locked_player(self):
        from ffbot import policy
        from ffbot.config import Config

        little = mk("Cam Little", "K", slot="K", team="JAX", proj=6.5, game_locked=True)
        verdict = policy.can_drop(little, Config())
        assert not verdict.allowed
        assert "kicked off" in verdict.reason

    def test_no_row_drops_a_player_whose_game_has_started(self):
        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"))
        now = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        started = {p.team.upper(): now - timedelta(hours=1) for p in loaded.players if p.team}
        loaded.availability = Availability(now=now, kickoffs=started)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=5)
        locked_names = {p.name for p in loaded.players if p.team}
        assert locked_names
        assert not [r for r in plan.adds + plan.claims if r.drop_name in locked_names]

    def test_start_sit_never_moves_a_player_whose_team_has_kicked_off(self):
        from dataclasses import replace

        def plan_for(lock: bool):
            loaded = _loaded(stream_positions=[])
            # Bench Wr (ARI) now outscores the starter, so an unlocked plan swaps them.
            loaded.players = [replace(p, projected_points=20.0) if p.name == "Bench Wr" else p for p in loaded.players]
            if lock:
                now = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
                loaded.availability = Availability(now=now, kickoffs={"ARI": now - timedelta(hours=1)})
            return build_gameplan(loaded, 1, loaded.players, my_priority=6)

        def names_in(plan):
            return {n for line in plan.start_sit for n in (line.start_name, line.bench_name) if n}

        assert "Bench Wr" in names_in(plan_for(lock=False)), "fixture must produce the swap unlocked"
        locked = plan_for(lock=True)
        assert "Bench Wr" not in names_in(locked)
        assert all(p.name != "Bench Wr" for _, p in locked.base_plan.assignments)
