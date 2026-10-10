"""IR eligibility comes from the league, and the plan recommends IR moves.

2026-09-24: the manager asked why the push never said "move Jayden Reed to IR
(he's Out) and pick someone up". In this league Sleeper refuses it -- every
`reserve_allow_*` flag is 0, so only an IR-tagged player fits the slot -- and
ffbot was silent for the right answer by accident: its IR-eligible statuses
were a hardcoded list (which wrongly admitted SUSP and NA), and nothing ever
recommended moving a rostered player to IR at all.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

from ffbot import report, week
from ffbot.availability import Availability
from ffbot.config import Config, IrRulesConfig, SeasonConfig
from ffbot.gameplan import AddDropRec, build_gameplan
from ffbot.league_rosters import LeagueRosters
from ffbot.lineup import optimize
from ffbot.models import slot_accepts
from ffbot.report import LoadedReport
from ffbot.sleeper.cache import SleeperFetchError
from ffbot.sleeper.scoring_import import ir_rules_from_sleeper
from ffbot.sleeper_roster import SleeperRosterPlayer, apply_sleeper_identity
from scripts import autorun
from scripts import week_report as wr_module
from tests.conftest import mk
from tests.test_gameplan import WEEK_NUM, _board
from tests.test_report import _FakeSleeperClientForSlots, _write_board_csv

# This league's real settings (data/sleeper/league_1393066447042797568.json).
OUR_LEAGUE = {
    "reserve_slots": 1, "reserve_allow_out": 0, "reserve_allow_doubtful": 0, "reserve_allow_sus": 0,
    "reserve_allow_na": 0, "reserve_allow_dnr": 0, "reserve_allow_cov": 0,
}


class TestLeagueRules:
    def test_our_league_admits_only_the_ir_tag(self):
        rules = IrRulesConfig(**ir_rules_from_sleeper(OUR_LEAGUE))
        assert not any(getattr(rules, f.name) for f in dataclasses.fields(rules) if f.name.startswith("allow_"))
        assert rules.allows_sleeper_status("IR")
        for word in ("Out", "Doubtful", "Sus", "NA", "DNR", "COV", "Questionable", "PUP", ""):
            assert not rules.allows_sleeper_status(word), word
        for code in ("O", "D", "SUSP", "NA", "PUP", "Q", ""):
            assert not rules.allows_code(code), code

    def test_each_flag_maps_and_a_missing_key_is_false(self):
        assert ir_rules_from_sleeper({}) == {k: False for k in ir_rules_from_sleeper(OUR_LEAGUE)}
        rules = IrRulesConfig(**ir_rules_from_sleeper({"reserve_allow_out": 1, "reserve_allow_sus": 1}))
        assert rules.allows_sleeper_status("Out") and rules.allows_code("O")
        assert rules.allows_sleeper_status("Sus") and rules.allows_code("SUSP")
        assert not rules.allows_sleeper_status("Doubtful")

    def test_dnr_alone_never_makes_a_doubtful_code_eligible(self):
        # Doubtful and DNR both normalize to "D"; the raw word tells them apart.
        rules = IrRulesConfig(allow_dnr=True)
        assert rules.allows_sleeper_status("DNR")
        assert not rules.allows_sleeper_status("Doubtful")
        assert not rules.allows_code("D")

    def test_offline_default_and_shipped_config(self):
        assert Config().ir_rules.source == "config"
        shipped = Config.load("config.yml")
        assert shipped.ir_rules.source == "sleeper"
        assert shipped.notify.ir_moves_wake is True


class TestTheStamp:
    def test_the_stamp_overrides_status_both_ways(self):
        assert not slot_accepts("IR", mk("A", "WR", status="IR", ir_eligible=False))
        assert slot_accepts("IR", mk("B", "WR", status="O", ir_eligible=True))

    def test_unstamped_fallback_is_the_ir_tag_only(self):
        assert mk("A", "WR", status="IR").is_ir_eligible()
        for status in ("O", "D", "SUSP", "NA", "PUP", "NFI"):
            assert not mk("A", "WR", status=status).is_ir_eligible(), status

    def test_stamped_from_sleepers_raw_word_and_a_research_override_does_not_restamp(self):
        sp = SleeperRosterPlayer(
            sleeper_id="1", name="Jayden Reed", team="GB", position="WR", status="D", injury_status_raw="DNR",
        )
        [p] = apply_sleeper_identity([mk("Jayden Reed", "WR")], [sp], ir_rules=IrRulesConfig(allow_dnr=True))
        assert p.ir_eligible is True
        weekly = week.WeeklyIntel(players={"jayden reed": week.WeeklyPlayerIntel(name="Jayden Reed", status="Q")})
        [after] = week.apply_status_overrides([p], weekly)
        assert after.status == "Q" and after.is_ir_eligible()

    def test_optimizer_releases_a_stamped_ineligible_ir_occupant(self):
        layout = {"WR": 1, "BN": 1, "IR": 1}
        held = mk("Held", "WR", slot="IR", proj=10.0, status="O", ir_eligible=True)
        freed = mk("Freed", "WR", slot="IR", proj=10.0, status="O", ir_eligible=False)
        assert held in optimize([held], layout, None, Config(roster_positions=layout)).held_in_ir
        assert freed not in optimize([freed], layout, None, Config(roster_positions=layout)).held_in_ir


class TestPlanIrMoves:
    LAYOUT = {"WR": 1, "BN": 2, "IR": 1}

    def _cfg(self, **rules):
        return Config(roster_positions=self.LAYOUT, ir_rules=IrRulesConfig(**rules))

    def test_an_eligible_bench_player_is_parked_and_frees_a_spot(self):
        roster = [mk("Starter", "WR", slot="WR"), mk("Hurt", "WR", status="IR"), mk("Depth", "WR")]
        moves, parked = week.plan_ir_moves(roster, self.LAYOUT, self._cfg())
        assert [(m.kind, m.name, m.to_slot) for m in moves] == [("to_ir", "Hurt", "IR")]
        assert week.roster_space(parked, self.LAYOUT).open_spots == week.roster_space(roster, self.LAYOUT).open_spots + 1

    def test_out_is_never_parked_unless_the_league_allows_it(self):
        # The Reed question itself.
        for allow, expected in ((False, []), (True, ["Jayden Reed"])):
            cfg = self._cfg(allow_out=allow)
            reed = mk("Jayden Reed", "WR", status="O", ir_eligible=cfg.ir_rules.allows_code("O"))
            moves, _ = week.plan_ir_moves([reed], self.LAYOUT, cfg)
            assert [m.name for m in moves] == expected

    def test_locked_doubtful_and_no_slot_are_never_moved(self):
        locked = mk("Locked", "WR", status="IR", game_locked=True)
        doubtful = mk("Doubtful", "WR", status="D", ir_eligible=True)
        assert week.plan_ir_moves([locked, doubtful], self.LAYOUT, self._cfg())[0] == []
        assert [m.name for m in week.plan_ir_moves([doubtful], self.LAYOUT, self._cfg(recommend_doubtful=True))[0]] == ["Doubtful"]
        assert week.plan_ir_moves([mk("Hurt", "WR", status="IR")], {"WR": 1, "BN": 2}, self._cfg())[0] == []

    def test_surest_absence_first_then_name(self):
        roster = [
            mk("Zed", "WR", status="O", ir_eligible=True),
            mk("Amy", "WR", status="O", ir_eligible=True),
            mk("Tag", "WR", status="IR"),
        ]
        two_slots = {"WR": 1, "BN": 2, "IR": 2}
        moves, _ = week.plan_ir_moves(roster, two_slots, Config(roster_positions=two_slots))
        assert [m.name for m in moves] == ["Tag", "Amy"]

    def test_a_lapsed_occupant_is_activated_and_frees_the_slot(self):
        roster = [mk("Healthy", "WR", slot="IR"), mk("Hurt", "WR", status="IR")]
        moves, after = week.plan_ir_moves(roster, self.LAYOUT, self._cfg())
        assert [(m.kind, m.name) for m in moves] == [("activate", "Healthy"), ("to_ir", "Hurt")]
        assert {p.name: p.selected_position for p in after} == {"Healthy": "BN", "Hurt": "IR"}

    def test_ranked_droppable_never_offers_an_ir_occupant(self):
        board = _board()
        roster = [mk("Bench Rb", "RB", slot="IR", status="IR"), mk("Bench Wr", "WR")]
        keys = ["bench rb:RB", "bench wr:WR"]
        assert week.ranked_droppable(roster, keys, board, Config(), naive=True) == ["bench wr:WR"]


def _ir_loaded(reed_status="IR", **rules) -> LoadedReport:
    """A full 7/7 roster (plus an IR slot) with one hurt bench player and a
    free agent worth adding."""
    layout = {"RB": 1, "WR": 1, "K": 1, "BN": 3, "IR": 1}
    cfg = Config(
        roster_positions=layout,
        season=SeasonConfig(ros_blend=1.0, recommend_count=5, waiver_pool_size=150, stream_positions=["K"]),
        ir_rules=IrRulesConfig(**rules),
    )
    roster = [
        mk("Roster Rb", "RB", slot="RB", proj=15.0, team="SF", bye_week=9),
        mk("Roster Wr", "WR", slot="WR", proj=14.0, team="MIA", bye_week=10),
        mk("Roster Kicker", "K", slot="K", proj=8.0, team="BUF", bye_week=4),
        mk("Bench Rb", "RB", slot="BN", proj=5.0, team="DAL", bye_week=9),
        mk("Bench Wr", "WR", slot="BN", proj=0.0, team="ARI", bye_week=10,
           status=reed_status, ir_eligible=cfg.ir_rules.allows_code(reed_status)),
        mk("Bench Kicker", "K", slot="BN", proj=3.0, team="NYJ", bye_week=6),
    ]
    return LoadedReport(
        cfg=cfg, weekly=week.WeeklyIntel(), board=_board(), players=roster, unmatched=[], stadiums={},
        league_rosters=LeagueRosters(), waiver_priority=6, availability=Availability(now=datetime.now(timezone.utc)),
    )


class TestIrMoveFundsAnOpenSpotAdd:
    def test_the_add_needs_no_drop_and_names_the_ir_move(self):
        full = build_gameplan(_ir_loaded(reed_status=""), WEEK_NUM, _ir_loaded(reed_status="").players, my_priority=1)
        assert full.ir_moves == []
        # Control: the full-roster version needs a drop for its top add.
        assert all(r.drop_name for r in full.adds if r.kind == "add")

        loaded = _ir_loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        assert [(m.kind, m.name) for m in plan.ir_moves] == [("to_ir", "Bench Wr")]
        assert plan.ir_freed == 1
        funded = [r for r in plan.adds if r.via_ir]
        assert len(funded) == 1
        row = funded[0]
        assert row.drop_name is None and row.via_ir == "Bench Wr"
        assert "moves to IR" in row.text
        assert plan.ir_moves[0].funds == row.add_name
        post = {p.name for _, p in plan.base_plan.assignments} | {p.name for p in plan.base_plan.bench}
        assert len(post) <= 6  # active capacity, IR excluded

    def test_the_parked_player_is_never_a_drop(self):
        loaded = _ir_loaded()
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        rows = plan.adds + plan.claims
        rows += [b for r in rows for b in r.backups]
        assert all(r.drop_name != "Bench Wr" for r in rows)

    def test_league_rules_gate_the_move(self):
        # Reed, Out, in a league whose flags are all off: no IR move.
        loaded = _ir_loaded(reed_status="O")
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        assert plan.ir_moves == []
        # The same roster in a league that admits Out players: the move is made.
        loaded = _ir_loaded(reed_status="O", allow_out=True)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        assert [m.name for m in plan.ir_moves] == ["Bench Wr"]

    def test_the_no_board_branch_still_reports_ir_moves(self):
        loaded = dataclasses.replace(_ir_loaded(), board=None)
        plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=1)
        assert [m.name for m in plan.ir_moves] == ["Bench Wr"]


def _run(waivers=(), ir_moves=(), start_sit=()):
    brief = SimpleNamespace(lineup=SimpleNamespace(assignments=[], moves=[]))
    plan = SimpleNamespace(start_sit=list(start_sit), current_plan=brief.lineup, ir_moves=list(ir_moves))
    loaded = SimpleNamespace(
        waiver_priority=7, availability=None, availability_source="off", claim_outcomes=[],
        projection_source="sleeper", roster_source="sleeper", slots_source="sleeper",
        league_rosters_source="sleeper", season_ptd_source="off", ros_board=None, board=None,
        cfg=SimpleNamespace(league=None),
    )
    return wr_module.ReportRun(week=3, loaded=loaded, brief=brief, waivers=list(waivers), sections=[], plan=plan)


def _cfg(ir_moves_wake=True):
    return SimpleNamespace(
        notify=SimpleNamespace(min_waiver_net=2.0, heartbeat=True, ir_moves_wake=ir_moves_wake),
        draft=SimpleNamespace(num_teams=12),
        autorun=SimpleNamespace(post_waiver_enabled=True),
    )


TO_IR = week.IrMove(kind="to_ir", name="Jayden Reed", status="IR", to_slot="IR")
ACTIVATE = week.IrMove(kind="activate", name="Christian Watson", to_slot="BN")


def _add(via_ir=""):
    from ffbot.availability import PlayerAvailability

    return AddDropRec(
        kind="add", position="WR", add_name="Tre Tucker", net=5.0, via_ir=via_ir,
        availability=PlayerAvailability(),
    )


class TestThePush:
    def test_an_ir_move_alone_is_an_add_drop_section(self):
        [body] = autorun.actionable_summary(_run(ir_moves=[TO_IR]), 2.0, cfg=_cfg())
        assert body.startswith("ADD/DROP\n")
        assert "IR Jayden Reed (IR)  frees a bench spot" in body
        headers = [l for l in body.splitlines() if l and not l.startswith(" ")]
        assert set(headers) <= set(autorun._SECTION_ORDER)

    def test_an_unfunded_ir_spot_names_the_best_free_fill_even_under_the_noise_floor(self):
        from ffbot.availability import PlayerAvailability

        def miss(name, pos, wk, drop=None, status="free"):
            return SimpleNamespace(
                add_name=name, position=pos, team="X", week_gain=wk, ros_gain_per_week=0.0, floor=2.3,
                drop_name=drop, availability=PlayerAvailability(status=status) if status != "free" else PlayerAvailability(),
            )

        run = _run(ir_moves=[TO_IR])
        run.plan.near_misses = [miss("Kicker Guy", "K", 0.8, drop="Cam Little"), miss("Jacoby Brissett", "QB", 0.9),
                                miss("Jake Ferguson", "TE", 0.7)]
        [body] = autorun.actionable_summary(run, 2.0, cfg=_cfg())
        assert "IR Jayden Reed (IR)  frees a bench spot  ->  ADD QB Jacoby Brissett  +0.9 wk" in body
        # named once: MONITOR does not repeat the fill
        assert body.count("Jacoby Brissett") == 1

    def test_the_funded_add_carries_the_move_once(self):
        [body] = autorun.actionable_summary(
            _run(waivers=[_add(via_ir="Jayden Reed")], ir_moves=[dataclasses.replace(TO_IR, funds="Tre Tucker")]),
            2.0, cfg=_cfg(),
        )
        assert "  IR Jayden Reed  ADD WR Tre Tucker" in body
        assert body.count("Jayden Reed") == 1

    def test_ir_moves_wake_false_rides_along_but_never_wakes(self):
        assert autorun.actionable_summary(_run(ir_moves=[TO_IR]), 2.0, cfg=_cfg(ir_moves_wake=False)) == []
        [body] = autorun.actionable_summary(_run(waivers=[_add()], ir_moves=[TO_IR]), 2.0, cfg=_cfg(ir_moves_wake=False))
        assert "IR Jayden Reed" in body

    def test_an_activation_always_wakes_and_reaches_tuesday(self):
        [body] = autorun.actionable_summary(_run(ir_moves=[ACTIVATE]), 2.0, cfg=_cfg(ir_moves_wake=False))
        assert "ACTIVATE Christian Watson from IR" in body
        [tuesday] = autorun.waiver_summary(_run(ir_moves=[ACTIVATE]), _cfg())
        assert "ACTIVATE Christian Watson" in tuesday
        assert autorun.waiver_summary(_run(ir_moves=[TO_IR]), _cfg()) == []

    def test_wednesday_includes_ir_lines(self):
        [body] = autorun.post_waiver_summary(_run(ir_moves=[TO_IR]), _cfg())
        assert "IR Jayden Reed" in body


class _IrClient(_FakeSleeperClientForSlots):
    settings = {"reserve_slots": 1, "reserve_allow_out": 1}
    PLAYERS = {
        "1": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "injury_status": None},
        "2": {"full_name": "Waiver Wr", "position": "WR", "team": "MIA", "injury_status": "Out"},
    }

    def league(self, league_id, **kwargs):
        return {"roster_positions": ["QB", "WR", "BN", "BN"], "settings": dict(self.settings)}


def _config(tmp_path, *, source="sleeper", ir_slots=1):
    path = tmp_path / "config.yml"
    path.write_text(
        f"roster_positions:\n  QB: 1\n  WR: 1\n  BN: 2\n  IR: {ir_slots}\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 4\n"
        f"  board_csv: [\"{_write_board_csv(tmp_path).as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        "roster_source:\n  source: sleeper\n"
        f"ir_rules:\n  source: {source}\n"
        "sleeper:\n  league_id: \"L1\"\n  roster_id: 4\n",
        encoding="utf-8",
    )
    return path


class TestLiveSeam:
    def _load(self, tmp_path, config):
        return report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no.yml"), week_num=1)

    def test_the_live_flags_are_applied_and_stamped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _IrClient)
        loaded = self._load(tmp_path, _config(tmp_path))
        assert loaded.ir_rules_source == "sleeper" and loaded.ir_rules_alerts == []
        assert loaded.cfg.ir_rules.allow_out is True
        by_name = {p.name: p for p in loaded.players}
        assert by_name["Waiver Wr"].ir_eligible is True
        assert by_name["Josh Allen"].ir_eligible is False

    def test_a_failed_fetch_falls_back_with_an_alert(self, tmp_path, monkeypatch):
        class _Broken(_IrClient):
            def league(self, league_id, **kwargs):
                raise SleeperFetchError("simulated network failure")

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _Broken)
        loaded = self._load(tmp_path, _config(tmp_path))
        assert loaded.ir_rules_source == "fallback"
        assert any("Live IR rules" in a for a in loaded.ir_rules_alerts)
        assert loaded.cfg.ir_rules.allow_out is False
        # Stamped from the fallback rules: Out is not eligible.
        assert {p.name: p for p in loaded.players}["Waiver Wr"].ir_eligible is False
        from ffbot import week_log

        assert week_log.live_sources(loaded)["ir_rules"] == "fallback"

    def test_a_slot_count_mismatch_uses_sleepers_with_an_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _IrClient)
        loaded = self._load(tmp_path, _config(tmp_path, ir_slots=2))
        assert loaded.cfg.roster_positions["IR"] == 1
        assert any("IR slot" in a for a in loaded.ir_rules_alerts)

    def test_source_config_never_asks(self, tmp_path, monkeypatch):
        class _Counting(_IrClient):
            calls = 0

            def league(self, league_id, **kwargs):
                type(self).calls += 1
                return super().league(league_id, **kwargs)

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _Counting)
        loaded = self._load(tmp_path, _config(tmp_path, source="config"))
        assert loaded.ir_rules_source == "config"
        assert loaded.cfg.ir_rules.allow_out is False
        # The roster seam still reads league() for its slot map -- once.
        assert _Counting.calls == 1


class TestOutputs:
    def test_week_log_and_webapi_share_one_serializer(self):
        from ffbot import webapi, week_log

        assert week_log.ir_move_json is webapi.ir_move_json
        assert webapi.adddrop_json(_add(via_ir="Jayden Reed"))["via_ir"] == "Jayden Reed"
        assert webapi.ir_move_json(TO_IR)["text"].startswith("Move Jayden Reed (IR) to IR")

    def test_the_cli_renders_ir_moves(self):
        text = wr_module.render_ir_moves([TO_IR, ACTIVATE])
        assert text.startswith("IR MOVES")
        assert "Activate Christian Watson from IR" in text

    def test_init_league_writes_the_leagues_flags(self, tmp_path):
        import yaml

        from scripts import init_league

        init_league._write_config_local(
            tmp_path, {"league_id": "L1"}, dry_run=False, extra={"ir_rules": ir_rules_from_sleeper(OUR_LEAGUE)},
        )
        written = yaml.safe_load((tmp_path / "config.local.yml").read_text(encoding="utf-8"))
        assert written["sleeper"] == {"league_id": "L1"}
        assert written["ir_rules"]["allow_out"] is False
        assert Config.from_dict(written).ir_rules.allow_out is False


class TestNoiseFloorNearMisses:
    """2026-09-27: every add sat inside the noise floor, MONITOR was empty for
    four checks, and the manager's answer was that those players are exactly
    what MONITOR is for."""

    def _loaded(self, noise_floor_weight):
        loaded = _ir_loaded(reed_status="")
        season = dataclasses.replace(loaded.cfg.season, noise_floor_weight=noise_floor_weight)
        return dataclasses.replace(loaded, cfg=dataclasses.replace(loaded.cfg, season=season))

    def test_floored_players_become_near_misses_and_change_no_move(self):
        open_plan = build_gameplan(self._loaded(0.0), WEEK_NUM, self._loaded(0.0).players, my_priority=1)
        floored = build_gameplan(self._loaded(50.0), WEEK_NUM, self._loaded(50.0).players, my_priority=1)
        assert open_plan.adds or open_plan.claims
        assert floored.near_misses, "a huge floor must turn real gains into near misses"
        named = {r.add_name for r in floored.adds + floored.claims}
        for m in floored.near_misses:
            assert m.add_name not in named
            assert m.week_gain > 0 or m.ros_gain_per_week > 0 or m.position == "K"
            if m.position == "K":  # a streamed position swaps for its incumbent
                assert m.drop_name == "Roster Kicker"

    def test_monitor_shows_them_one_per_position(self):
        near = [
            week.NearMiss("Matt Gay", "K", ros_gain_per_week=0.7, floor=4.4, drop_name="Cam Little"),
            week.NearMiss("Spencer Shrader", "K", ros_gain_per_week=0.6, floor=4.4, drop_name="Cam Little"),
            week.NearMiss("Baltimore Ravens", "DEF", ros_gain_per_week=0.3, floor=3.8, drop_name="Kansas City Chiefs"),
        ]
        run = _run()
        run.plan.near_misses = near
        lines = autorun.monitor_lines(run, _cfg())
        assert lines == [
            "  ADD K Matt Gay  DROP Cam Little  +0.0 wk  +0.7/wk ROS  under the 4.4 noise floor",
            "  ADD DEF Baltimore Ravens  DROP Kansas City Chiefs  +0.0 wk  +0.3/wk ROS  under the 3.8 noise floor",
        ]
        # And a quiet check carries them.
        assert "Matt Gay" in autorun.render_sections(autorun._quiet_sections(run, _cfg(), pre_run=True))
