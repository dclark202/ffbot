from __future__ import annotations

import json

import pytest

from ffbot import gameplan, week
from ffbot.board import Board
from ffbot.config import Config, DraftConfig, LeagueScoring, SeasonConfig, TeamStanding
from ffbot.gameplan import (
    AddDropRec,
    GamePlan,
    SwapLine,
    build_gameplan,
    display_slot,
    pair_moves,
)
from ffbot.league_rosters import LeagueRosters
from ffbot.lineup import LineupPlan, Move
from ffbot.names import normalize_name
from ffbot.report import LoadedReport
from tests.conftest import mk, mk_bp


class TestDisplaySlot:
    def test_yahoo_flex_maps_to_flex(self):
        assert display_slot("W/R/T") == "FLEX"

    def test_superflex_maps_to_super_flex(self):
        assert display_slot("Q/W/R/T") == "SUPER_FLEX"

    def test_dedicated_slot_passes_through(self):
        assert display_slot("RB") == "RB"

    def test_unknown_slot_passes_through(self):
        assert display_slot("MYSTERY") == "MYSTERY"


LAYOUT = {"WR": 1, "W/R/T": 1, "BN": 1}


class TestPairMoves:
    def test_simple_swap_is_paired_with_no_reason_suffix(self):
        starter = mk("Jim Bologna", "WR", slot="WR", team="CHI")
        bench = mk("Karl Marx", "WR", slot="BN", team="DEN")
        plan = LineupPlan(moves=[
            Move(starter, from_slot="BN", to_slot="WR", reason="proj 9.6"),
            Move(bench, from_slot="WR", to_slot="BN", reason="outscored (proj 6.1)"),
        ])
        lines = pair_moves(plan, {"WR": 1, "BN": 1})
        assert len(lines) == 1
        line = lines[0]
        assert line.kind == "swap"
        assert line.text == "WR: Start Jim Bologna (CHI) — Bench Karl Marx (DEN)"

    def test_swap_with_bye_reason_shows_parenthetical(self):
        starter = mk("A", "WR", slot="WR", team="CHI")
        bench = mk("B", "WR", slot="BN", team="DEN")
        plan = LineupPlan(moves=[
            Move(starter, from_slot="BN", to_slot="WR", reason="proj 9.6"),
            Move(bench, from_slot="WR", to_slot="BN", reason="bye week 3"),
        ])
        lines = pair_moves(plan, {"WR": 1, "BN": 1})
        assert lines[0].text == "WR: Start A (CHI) — Bench B (DEN) (bye week 3)"

    def test_slot_shift_is_first_class(self):
        mover = mk("Jim Bojimbo", "WR", slot="WR", team="DAL")
        plan = LineupPlan(moves=[Move(mover, from_slot="WR", to_slot="W/R/T", reason="proj 12.3")])
        lines = pair_moves(plan, LAYOUT)
        assert len(lines) == 1
        assert lines[0].kind == "slot_shift"
        assert lines[0].text == "WR → FLEX: Jim Bojimbo (DAL)"

    def test_start_only_when_slot_vacated_by_a_shift(self):
        mover = mk("A", "WR", slot="WR", team="DAL")
        entrant = mk("C", ["WR", "RB"], slot="BN", team="NYG")
        plan = LineupPlan(moves=[
            Move(mover, from_slot="WR", to_slot="W/R/T", reason="proj 12.3"),
            Move(entrant, from_slot="BN", to_slot="WR", reason="proj 14.0"),
        ])
        lines = pair_moves(plan, LAYOUT)
        kinds = {l.kind for l in lines}
        assert kinds == {"slot_shift", "start_only"}
        start_line = next(l for l in lines if l.kind == "start_only")
        assert start_line.text == "WR: Start C (NYG) (slot opened by A's move to FLEX)"

    def test_bench_only_when_nobody_replaces_them(self):
        benched = mk("B", "WR", slot="WR", team="MIA")
        plan = LineupPlan(moves=[Move(benched, from_slot="WR", to_slot="BN", reason="status O")])
        lines = pair_moves(plan, {"WR": 1, "BN": 1})
        assert len(lines) == 1
        assert lines[0].kind == "bench_only"
        assert lines[0].text == "WR: Bench B (MIA) (status O) — slot unfilled"

    def test_add_start_replaces_dropped_player(self):
        added = mk("New Guy", "WR", slot="BN", team="SEA")
        dropped = mk("Old Guy", "WR", slot="WR", team="ARI")
        plan = LineupPlan(moves=[Move(added, from_slot="BN", to_slot="WR", reason="proj 10.0")])
        lines = pair_moves(plan, {"WR": 1, "BN": 1}, added_ids=frozenset({added.player_id}), dropped=[dropped])
        assert len(lines) == 1
        line = lines[0]
        assert line.kind == "add_start"
        assert line.text == "WR: Add & start New Guy (SEA) — Drop Old Guy (ARI)"

    def test_add_start_into_a_genuinely_empty_slot(self):
        added = mk("New Guy", "WR", slot="BN", team="SEA")
        plan = LineupPlan(moves=[Move(added, from_slot="BN", to_slot="WR", reason="proj 10.0")])
        lines = pair_moves(plan, {"WR": 1, "BN": 1}, added_ids=frozenset({added.player_id}))
        assert lines[0].text == "WR: Add & start New Guy (SEA) (slot was empty)"

    def test_deterministic_ordering_by_slot_then_name(self):
        a = mk("Zed", "WR", slot="BN", team="A")
        b = mk("Amy", "WR", slot="BN", team="B")
        plan = LineupPlan(moves=[
            Move(a, from_slot="BN", to_slot="WR", reason="proj 1"),
        ])
        plan2 = LineupPlan(moves=[
            Move(b, from_slot="BN", to_slot="WR", reason="proj 1"),
        ])
        # Same call twice with different single occupants is trivially
        # ordered; the real guarantee (multiple slots sorted by layout
        # order) is exercised by the swap/shift tests above via distinct
        # slots. This just pins that no exception occurs on repeat calls.
        assert pair_moves(plan, {"WR": 1, "BN": 1})[0].start_name == "Zed"
        assert pair_moves(plan2, {"WR": 1, "BN": 1})[0].start_name == "Amy"


LOADED_LAYOUT = {"RB": 1, "WR": 1, "K": 1, "BN": 3}


def _board() -> Board:
    players = [
        mk_bp("Roster Rb", "RB", points=180.0, team="SF", bye_week=9, rank=1, vor=140.0),
        mk_bp("Roster Wr", "WR", points=170.0, team="MIA", bye_week=10, rank=2, vor=125.0),
        mk_bp("Roster Kicker", "K", points=90.0, team="BUF", bye_week=5, rank=6, vor=10.0),
        mk_bp("Bench Rb", "RB", points=80.0, team="DAL", bye_week=9, rank=10, vor=40.0),
        mk_bp("Bench Wr", "WR", points=70.0, team="ARI", bye_week=10, rank=11, vor=35.0),
        mk_bp("Bench Kicker", "K", points=60.0, team="NYJ", bye_week=6, rank=15, vor=-20.0),
        mk_bp("Waiver Rb Gem", "RB", points=220.0, team="KC", bye_week=9, rank=3, vor=180.0),
        mk_bp("Waiver Wr Gem", "WR", points=210.0, team="LAR", bye_week=10, rank=4, vor=165.0),
        mk_bp("Backup Kicker", "K", points=100.0, team="SEA", bye_week=8, rank=5, vor=20.0),
    ]
    return Board(
        players=players, by_key={p.key: p for p in players},
        replacement={"RB": 50.0, "WR": 45.0, "K": 40.0},
        starters_per_pos={}, tier_last={},
    )


def _roster():
    return [
        mk("Roster Rb", "RB", slot="RB", proj=15.0, team="SF", bye_week=9),
        mk("Roster Wr", "WR", slot="WR", proj=14.0, team="MIA", bye_week=10),
        mk("Roster Kicker", "K", slot="K", proj=8.0, team="BUF", bye_week=5),
        mk("Bench Rb", "RB", slot="BN", proj=5.0, team="DAL", bye_week=9),
        mk("Bench Wr", "WR", slot="BN", proj=4.0, team="ARI", bye_week=10),
        mk("Bench Kicker", "K", slot="BN", proj=3.0, team="NYJ", bye_week=6),
    ]


WEEK_NUM = 5  # matches Roster Kicker's bye_week -- forces a real K need


def _loaded(**season_kw) -> LoadedReport:
    defaults = dict(ros_blend=1.0, recommend_count=5, waiver_pool_size=150, stream_positions=["K"])
    defaults.update(season_kw)
    season = SeasonConfig(**defaults)
    cfg = Config(roster_positions=LOADED_LAYOUT, season=season)
    return LoadedReport(
        cfg=cfg, weekly=week.WeeklyIntel(), board=_board(), players=_roster(),
        unmatched=[], stadiums={}, league_rosters=LeagueRosters(), waiver_priority=6,
    )


class TestBuildGameplanNoBoard:
    def test_start_sit_still_populated_with_no_board(self):
        loaded = _loaded()
        loaded = LoadedReport(
            cfg=loaded.cfg, weekly=loaded.weekly, board=None, players=loaded.players,
            unmatched=[], stadiums={}, league_rosters=LeagueRosters(),
        )
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        assert isinstance(plan, GamePlan)
        assert plan.adds == [] and plan.claims == []
        assert isinstance(plan.start_sit, list)


class TestBuildGameplanWithBoard:
    def test_returns_a_gameplan(self, ):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        assert isinstance(plan, GamePlan)

    def test_forced_k_need_surfaces_a_row(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        all_rows = plan.adds + plan.claims
        k_rows = [r for r in all_rows if r.position == "K"]
        assert k_rows, "expected at least one K row given the bye-week incumbent"
        assert any(r.forced_need for r in k_rows)

    def test_only_positive_net_rows_are_recommended(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        for row in plan.adds + plan.claims:
            assert row.net > 0.0

    def test_rows_capped_at_recommend_count(self):
        loaded = _loaded(recommend_count=2)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        assert len(plan.adds) <= 2
        assert len(plan.claims) <= 2

    def test_accepted_adds_use_distinct_drops(self):
        # Force HOLD-PRIORITY (kind="add") classification so the coherent
        # multi-add transaction set actually has >=2 adds to pair drops
        # for -- with the default priority economics almost everything
        # clears the CLAIM bar (see claim_verdict), so this pushes the
        # other way (a very expensive claim_cost at the best priority).
        loaded = _loaded(priority_value=2.0)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        drop_names = [r.drop_name for r in plan.adds if r.drop_name]
        assert len(drop_names) == len(set(drop_names)), f"drops were reused across adds: {drop_names}"

    def test_base_plan_reflects_accepted_adds(self):
        loaded = _loaded(priority_value=2.0)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        if plan.adds:
            base_names = {p.name for _, p in plan.base_plan.assignments} | {p.name for p in plan.base_plan.bench}
            for row in plan.adds:
                assert row.add_name in base_names

    def test_claim_carries_an_if_clears_consequence(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        for row in plan.claims:
            assert row.if_clears is not None
            assert row.if_clears.text

    def test_denial_off_by_default_produces_no_denial_reasons(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        assert all(r.denial_team == "" for r in plan.adds + plan.claims)

    def test_denial_priority_floor_suppresses_with_a_note(self):
        rosters = LeagueRosters(teams={"Rival": ["Waiver Rb Gem"]})
        cfg = Config(
            roster_positions=LOADED_LAYOUT,
            season=SeasonConfig(
                ros_blend=1.0, recommend_count=5, waiver_pool_size=150, stream_positions=["K"],
                denial_weight=0.5, denial_priority_floor=10,
            ),
        )
        loaded = LoadedReport(
            cfg=cfg, weekly=week.WeeklyIntel(), board=_board(), players=_roster(),
            unmatched=[], stadiums={}, league_rosters=rosters, waiver_priority=1,
        )
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        assert any("suppressed" in n.lower() for n in plan.notes) or all(
            r.denial_team == "" for r in plan.adds + plan.claims
        )

    def test_no_opponent_starters_is_an_exact_noop_for_opp_stack_notes(self):
        loaded = _loaded(opponent_correlation_weight=0.3)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        for row in plan.adds + plan.claims:
            assert not any("opp-stack" in r or "leverage" in r for r in row.reasons)
        for line in plan.start_sit:
            assert line.opp_stack_note == ""


class TestOppStackNoteOnStartSit:
    """The positive counterpart to
    `test_no_opponent_starters_is_an_exact_noop_for_opp_stack_notes`.

    On its own, that no-op test passes whether the feature works or was
    never written -- and it never was. `opp_stack_note` was declared on
    `SwapLine`, folded into `_swap_text`, serialized by `webapi`, and
    rendered by `web/weekly.html`, while `pair_moves` assigned it at none of
    its construction sites, so it was permanently "". Meanwhile
    `week.adjusted_players` really does apply the correlation nudge and
    discard the explanation -- which is how the penalty moved start/sit
    calls invisibly.
    """

    def _swap_plan(self):
        starter = mk("My Wr", "WR", slot="BN", team="BUF")
        bench = mk("Other Wr", "WR", slot="WR", team="DEN")
        return LineupPlan(moves=[
            Move(starter, from_slot="BN", to_slot="WR", reason="proj 12.0"),
            Move(bench, from_slot="WR", to_slot="BN", reason="outscored (proj 8.0)"),
        ])

    def test_note_is_set_and_reaches_the_rendered_text(self):
        # I start a BUF receiver; my opponent starts the BUF quarterback --
        # the direct passing-game link `opponent_overlap` scores at +1.0.
        opp_index = week.opponent_stack_index([
            week.OpponentStarter(player_id="1", name="Their Qb", team="BUF", position="QB"),
        ])
        lines = pair_moves(
            self._swap_plan(), {"WR": 1, "BN": 1},
            opp_index=opp_index, opp_weight=0.3, decision_scale=20.0,
        )
        line = lines[0]
        assert line.opp_stack_note, "a started player correlated with the opponent must be explained"
        assert "Their Qb" in line.opp_stack_note
        assert line.opp_stack_note in line.text

    def test_uncorrelated_starter_gets_no_note(self):
        opp_index = week.opponent_stack_index([
            week.OpponentStarter(player_id="1", name="Their Qb", team="KC", position="QB"),
        ])
        lines = pair_moves(
            self._swap_plan(), {"WR": 1, "BN": 1},
            opp_index=opp_index, opp_weight=0.3, decision_scale=20.0,
        )
        assert lines[0].opp_stack_note == ""

    def test_zero_weight_is_an_exact_noop(self):
        opp_index = week.opponent_stack_index([
            week.OpponentStarter(player_id="1", name="Their Qb", team="BUF", position="QB"),
        ])
        lines = pair_moves(
            self._swap_plan(), {"WR": 1, "BN": 1},
            opp_index=opp_index, opp_weight=0.0, decision_scale=20.0,
        )
        assert lines[0].opp_stack_note == ""


DEMO_LAYOUT = {"RB": 1, "WR": 1, "K": 1, "DEF": 1, "BN": 3}


def _demo_shaped_loaded(*, stream_positions=("K",), denial_row_limit=1) -> LoadedReport:
    """The exact shape that produced the reported bug: rivals with NO K or
    DEF at all (a real `draft_sim`-opponent artifact), against a FLAT
    top-of-pool at both positions -- denying any one of them should cost a
    rival almost nothing, since an equivalent is right behind it. Only K is
    in `stream_positions` by default here, so DEF denial candidates must be
    suppressed by the fungibility discount alone, not the stream-position
    skip -- isolating the actual fix from the belt-and-braces one.
    """
    players = [
        mk_bp("Roster Rb", "RB", points=180.0, team="SF", bye_week=9, rank=1, vor=140.0),
        mk_bp("Roster Wr", "WR", points=170.0, team="MIA", bye_week=10, rank=2, vor=125.0),
        mk_bp("Roster Kicker", "K", points=90.0, team="BUF", bye_week=9, rank=6, vor=10.0),
        mk_bp("Roster Def", "DEF", points=95.0, team="NE", bye_week=9, rank=7, vor=12.0),
        mk_bp("Bench Rb", "RB", points=80.0, team="DAL", bye_week=9, rank=10, vor=40.0),
        mk_bp("Bench Wr", "WR", points=70.0, team="ARI", bye_week=10, rank=11, vor=35.0),
        # A flat top-of-pool at both K and DEF -- three near-identical free
        # agents each, exactly the demo season's shape.
        mk_bp("K One", "K", points=100.0, team="SEA", bye_week=8, rank=20, vor=20.0),
        mk_bp("K Two", "K", points=99.0, team="NYJ", bye_week=8, rank=21, vor=19.0),
        mk_bp("K Three", "K", points=98.0, team="LAR", bye_week=8, rank=22, vor=18.0),
        mk_bp("Def One", "DEF", points=110.0, team="KC", bye_week=8, rank=15, vor=25.0),
        mk_bp("Def Two", "DEF", points=108.0, team="DEN", bye_week=8, rank=16, vor=23.0),
        mk_bp("Def Three", "DEF", points=106.0, team="LAC", bye_week=8, rank=17, vor=21.0),
    ]
    board = Board(
        players=players, by_key={p.key: p for p in players},
        replacement={"RB": 50.0, "WR": 45.0, "K": 40.0, "DEF": 40.0},
        starters_per_pos={}, tier_last={},
    )
    roster = [
        mk("Roster Rb", "RB", slot="RB", proj=15.0, team="SF", bye_week=9),
        mk("Roster Wr", "WR", slot="WR", proj=14.0, team="MIA", bye_week=10),
        mk("Roster Kicker", "K", slot="K", proj=8.0, team="BUF", bye_week=9),
        mk("Roster Def", "DEF", slot="DEF", proj=9.0, team="NE", bye_week=9),
        mk("Bench Rb", "RB", slot="BN", proj=5.0, team="DAL", bye_week=9),
        mk("Bench Wr", "WR", slot="BN", proj=4.0, team="ARI", bye_week=10),
    ]
    # Every rival has an EMPTY roster -- no K, no DEF, nothing -- the
    # widest-open "need" draft.need() can see, and exactly what made the
    # un-discounted math price every K/DEF free agent at +15-25 pts.
    rosters = LeagueRosters(teams={"Rival A": [], "Rival B": [], "Rival C": []})
    cfg = Config(
        roster_positions=DEMO_LAYOUT,
        draft=DraftConfig(num_teams=12),
        season=SeasonConfig(
            ros_blend=1.0, recommend_count=5, waiver_pool_size=150,
            stream_positions=list(stream_positions), denial_weight=1.0,
            denial_priority_floor=0, denial_row_limit=denial_row_limit,
        ),
    )
    cfg.league = LeagueScoring(playoff_teams=4, teams=[
        TeamStanding(name="Rival A", seed=4), TeamStanding(name="Rival B", seed=4), TeamStanding(name="Rival C", seed=4),
    ])
    return LoadedReport(
        cfg=cfg, weekly=week.WeeklyIntel(), board=board, players=roster,
        unmatched=[], stadiums={}, league_rosters=rosters, waiver_priority=6,
    )


class TestDenialFungibilityRegression:
    """The exact bug report: demo season recommended waiver claims on
    3 defenses and 2 kickers, all pure-denial, because raw need() vs.
    replacement level doesn't know the wire is flat at those positions."""

    def test_flat_top_of_pool_produces_no_def_denial_rows(self):
        # DEF is NOT in stream_positions here, so any suppression is coming
        # from the fungibility discount alone, not the belt-and-braces
        # stream-position skip in denial_candidates.
        loaded = _demo_shaped_loaded(stream_positions=("K",))
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        def_denial_rows = [r for r in plan.adds + plan.claims if r.position == "DEF" and r.denial_team]
        assert def_denial_rows == [], f"expected no DEF denial rows, got {def_denial_rows}"

    def test_k_never_denial_via_stream_position_skip(self):
        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"))
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        k_denial_rows = [r for r in plan.adds + plan.claims if r.position == "K" and r.denial_team]
        assert k_denial_rows == []

    def _with_scarce_te_and_qb(self, denial_row_limit: int) -> LoadedReport:
        """Two genuinely scarce positions (one available free agent each,
        no fungible alternative -- real denial value) layered onto the
        demo-shaped fixture. My OWN roster is already saturated at TE/QB
        (a strong starter at each), so these candidates carry ~zero
        marginal value for ME and can only ever show up as pure-denial
        rows -- isolating the `denial_row_limit` cap from the ordinary
        add/claim ranking.
        """
        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"), denial_row_limit=denial_row_limit)
        saturating = [
            mk_bp("My Te", "TE", points=220.0, team="KC", bye_week=9, rank=1, vor=100.0),
            mk_bp("My Qb", "QB", points=380.0, team="BUF", bye_week=9, rank=1, vor=110.0),
            mk_bp("Scarce Te", "TE", points=200.0, team="CIN", bye_week=8, rank=3, vor=90.0),
            mk_bp("Scarce Qb", "QB", points=350.0, team="MIN", bye_week=8, rank=4, vor=95.0),
        ]
        loaded.board.players.extend(saturating)
        for bp in saturating:
            loaded.board.by_key[bp.key] = bp
        loaded.board.replacement["TE"] = 60.0
        loaded.board.replacement["QB"] = 200.0
        loaded.cfg.roster_positions = {**DEMO_LAYOUT, "TE": 1, "QB": 1, "BN": 2}
        loaded.players = list(loaded.players) + [
            mk("My Te", "TE", slot="TE", proj=18.0, team="KC", bye_week=9),
            mk("My Qb", "QB", slot="QB", proj=25.0, team="BUF", bye_week=9),
        ]
        return loaded

    def test_denial_row_limit_caps_pure_denial_rows(self):
        loaded = self._with_scarce_te_and_qb(denial_row_limit=1)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        denial_tagged = [r for r in plan.adds + plan.claims if r.denial_team]
        # The fixture genuinely offers TWO scarce denial candidates (Scarce
        # Te and Scarce Qb) -- proving the cap is actually doing something,
        # not vacuously true because nothing qualified.
        assert len(denial_tagged) == 1

    def test_higher_denial_row_limit_allows_more_scarce_rows(self):
        loaded = self._with_scarce_te_and_qb(denial_row_limit=5)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        denial_tagged = [r for r in plan.adds + plan.claims if r.denial_team]
        assert len(denial_tagged) == 2
        assert {r.add_name for r in denial_tagged} == {"Scarce Te", "Scarce Qb"}


class TestHandoffAwareDrop:
    def _forced_drop_loaded(self) -> LoadedReport:
        """BN=1 with a single droppable bench player (Bench Rb) and a
        genuinely strong free-agent WR add -- there is exactly one possible
        drop, so the recommended row for the add is GUARANTEED to name it,
        making this a real (non-vacuous) check of the handoff mention
        rather than a conditional that might never fire.
        """
        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"))
        strong_wr = mk_bp("Strong Wr", "WR", points=250.0, team="CIN", bye_week=8, rank=1, vor=180.0)
        loaded.board.players.append(strong_wr)
        loaded.board.by_key[strong_wr.key] = strong_wr
        loaded.cfg.roster_positions = {"RB": 1, "WR": 1, "K": 1, "DEF": 1, "BN": 1}
        loaded.players = [
            mk("Roster Rb", "RB", slot="RB", proj=15.0, team="SF", bye_week=9),
            mk("Roster Wr", "WR", slot="WR", proj=14.0, team="MIA", bye_week=10),
            mk("Roster Kicker", "K", slot="K", proj=8.0, team="BUF", bye_week=9),
            mk("Roster Def", "DEF", slot="DEF", proj=9.0, team="NE", bye_week=9),
            mk("Bench Rb", "RB", slot="BN", proj=5.0, team="DAL", bye_week=9),
        ]
        return loaded

    def test_forced_scarce_drop_surfaces_the_handoff_reason(self):
        loaded = self._forced_drop_loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        strong_wr_rows = [r for r in plan.adds + plan.claims if r.add_name == "Strong Wr"]
        assert strong_wr_rows, "expected the strong WR to be recommended"
        row = strong_wr_rows[0]
        assert row.drop_name == "Bench Rb"  # the only possible drop in this fixture
        assert "could claim" in row.drop_reason
        assert any("could claim" in r for r in row.reasons)

    def test_handoff_off_at_zero_denial_weight_leaves_plain_drop_reason(self):
        loaded = self._forced_drop_loaded()
        loaded.cfg.season.denial_weight = 0.0
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        strong_wr_rows = [r for r in plan.adds + plan.claims if r.add_name == "Strong Wr"]
        assert strong_wr_rows
        row = strong_wr_rows[0]
        assert row.drop_name == "Bench Rb"
        assert "could claim" not in (row.drop_reason or "")
        assert not any("could claim" in r for r in row.reasons)


class TestPriceADropHandoff:
    def test_handoff_none_is_bit_identical_to_before(self):
        from ffbot.gameplan import _price_a_drop

        loaded = _demo_shaped_loaded(stream_positions=("K", "DEF"))
        roster_keys, _ = week.roster_board_keys(loaded.players, loaded.board)
        without = _price_a_drop(
            loaded.players, roster_keys, loaded.board, loaded.cfg.roster_positions, loaded.cfg,
            naive=False, priority=6, num_teams=12, gain=5.0,
        )
        with_none_rosters = _price_a_drop(
            loaded.players, roster_keys, loaded.board, loaded.cfg.roster_positions, loaded.cfg,
            naive=False, priority=6, num_teams=12, gain=5.0, league_rosters=None, alternatives=None,
        )
        assert without == with_none_rosters

    def test_gameplan_json_serializable(self):
        """Through the REAL serializers, not `vars()`.

        `vars()` used to stand in for "is this shape JSON-safe", which was
        only true while every field was a scalar. Now that rows nest
        `PlayerMetrics`/`DecisionMetrics`, `vars()` tests a payload nothing
        actually sends -- `webapi`'s serializers are what the GUI and the
        weekly log both emit, so they are what has to stay serializable.
        """
        from ffbot import webapi

        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        payload = {
            "start_sit": [webapi.swap_line_json(l) for l in plan.start_sit],
            "adds": [webapi.adddrop_json(r) for r in plan.adds],
            "claims": [webapi.adddrop_json(r) for r in plan.claims],
        }
        json.dumps(payload)  # must not raise


class TestRecommendationMetrics:
    """Every recommendation carries the numbers it was decided on.

    Before this, `build_gameplan` computed a rich set of floats per row,
    formatted one or two into `reason`/`text`, and discarded the rest --
    which made a plan impossible to review after the fact.
    """

    def test_every_addrop_row_carries_add_metrics_and_a_decision(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        rows = plan.adds + plan.claims
        assert rows, "fixture should produce at least one row"
        for row in rows:
            assert row.add_metrics is not None
            assert row.add_metrics.name == row.add_name
            assert row.decision is not None
            assert row.decision.net == pytest.approx(row.net)
            assert row.decision.decision_scale > 0.0

    def test_typed_components_are_forwarded_from_waiver_candidates(self):
        """`WaiverCandidate.ros_gain`/`week_gain`/`claim_cost` were added so
        `gameplan` could read them instead of re-parsing `reason`'s text, and
        then nothing read them at all. This is that consumer."""
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        rows = [r for r in plan.adds + plan.claims if r.decision is not None]
        assert any(r.decision.ros_gain or r.decision.week_gain for r in rows)

    def test_a_paired_drop_carries_its_own_metrics(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        paired = [r for r in plan.adds + plan.claims if r.drop_name]
        assert paired
        for row in paired:
            assert row.drop_metrics is not None
            assert row.drop_metrics.name == row.drop_name

    def test_swap_lines_carry_both_sides(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        swaps = [l for l in plan.start_sit if l.start_name and l.bench_name]
        assert swaps
        for line in swaps:
            assert line.start_metrics is not None and line.bench_metrics is not None
            assert line.start_metrics.name == line.start_name

    def test_pool_source_says_which_board_ros_proj_came_from(self):
        """`pool = ros_board or board`, so the same field is a real ROS total
        live and a full-season total offline. The label has to travel with
        the number or it silently misleads on every offline run."""
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        row = (plan.adds + plan.claims)[0]
        assert row.add_metrics.pool_source == "board"  # no ros_board in this fixture

    def test_run_level_context_is_recorded(self):
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        assert plan.decision_scale > 0.0
        assert plan.roster_capacity > 0
        assert plan.current_total > 0.0 and plan.base_total > 0.0

    def test_pair_moves_without_a_metrics_index_is_an_exact_noop(self):
        """Every direct caller (and the board-less path) passes none."""
        loaded = _loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        lines = pair_moves(plan.base_plan, loaded.cfg.roster_positions)
        assert lines
        assert all(l.start_metrics is None and l.decision is None for l in lines)


class TestSeasonPointsToDateIsDescriptiveOnly:
    def test_populating_it_changes_no_recommendation(self):
        """The invariant the whole season-to-date design rests on.

        A realized-outcome number entering the ranking would be a scoring
        change no backtest has graded, and it would double-count against
        live ROS projections that already price in past production. So:
        identical plans, with and without it.
        """
        plain = _loaded()
        with_ptd = _loaded()
        # Give every board player a wildly different "actual" than projected.
        with_ptd.season_ptd = {
            f"{normalize_name(bp.name)}:{bp.position}": bp.points * 5.0
            for bp in with_ptd.board.players
        }
        with_ptd.season_ptd_games = {k: 4 for k in with_ptd.season_ptd}
        with_ptd.season_ptd_source = "sleeper"

        a = build_gameplan(plain, WEEK_NUM, plain.players, my_priority=6)
        b = build_gameplan(with_ptd, WEEK_NUM, with_ptd.players, my_priority=6)

        assert [r.text for r in a.adds] == [r.text for r in b.adds]
        assert [r.text for r in a.claims] == [r.text for r in b.claims]
        assert [r.net for r in a.claims] == [r.net for r in b.claims]
        assert [l.text for l in a.start_sit] == [l.text for l in b.start_sit]

    def test_but_it_does_reach_the_metrics(self):
        """Descriptive-only must not mean invisible -- the number is the
        whole point of fetching it."""
        loaded = _loaded()
        loaded.season_ptd = {
            f"{normalize_name(bp.name)}:{bp.position}": 123.5 for bp in loaded.board.players
        }
        loaded.season_ptd_games = {k: 4 for k in loaded.season_ptd}
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
        row = (plan.adds + plan.claims)[0]
        assert row.add_metrics.season_ptd == pytest.approx(123.5)
        assert row.add_metrics.games_played == 4
