from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ffbot import week_grade as wg


def _m(name, pos, sleeper, ours, adjustments=(), state="", live=None, team="JAX"):
    return {
        "name": name, "position": pos, "team": team, "board_key": f"{name.lower()}:{pos}",
        "sleeper_proj": sleeper, "week_proj": ours,
        "adjustments": [{"label": l, "delta": d} for l, d in adjustments],
        "game_state": state, "live_pts": live,
    }


def _log(stamp, starters=(), bench=(), week=1):
    return {
        "season": 2026, "week": week, "generated_at": stamp,
        "lineup_current": {"starters": [{"slot": "X", "metrics": m} for m in starters], "bench": [{"metrics": m} for m in bench]},
    }


WIND = "weather: wind 40 mph, 40% rain"


class TestSnapshotSelection:
    def test_last_pre_kickoff_snapshot_wins_not_a_later_one(self):
        early = _log("2026-09-13T08:00", [_m("Trevor Lawrence", "QB", 18.9, 18.0)])
        late_pre = _log("2026-09-13T12:00", [_m("Trevor Lawrence", "QB", 18.9, 15.2, [(WIND, -3.9)])])
        after = _log("2026-09-13T18:00", [_m("Trevor Lawrence", "QB", 18.9, 99.0, state="FINAL", live=26.1)])
        rows, finals = wg.collect([early, late_pre, after])
        assert rows["trevor lawrence:QB"].week_proj == 15.2
        assert rows["trevor lawrence:QB"].snapshot == "2026-09-13T12:00"
        assert finals["trevor lawrence:QB"] == 26.1

    def test_schema_less_pre_kickoff_log_yields_to_first_post_kickoff_breakdown(self):
        """2026 week 1: the only JAX pre-kickoff log predates `adjustments`, so
        the wind cut is visible only in the first snapshot after kickoff."""
        legacy_m = {k: v for k, v in _m("Trevor Lawrence", "QB", None, 15.2).items() if k not in ("adjustments", "game_state", "live_pts")}
        logs = [
            _log("2026-09-13T10:49", [legacy_m]),
            _log("2026-09-13T13:08", [_m("Trevor Lawrence", "QB", 18.9, 15.2, [(WIND, -3.9)], state="LIVE", live=18.8)]),
            _log("2026-09-13T18:02", [_m("Trevor Lawrence", "QB", 18.9, 15.2, [(WIND, -3.9)], state="FINAL", live=26.1)]),
        ]
        rows, finals = wg.collect(logs)
        row = rows["trevor lawrence:QB"]
        assert (row.source, row.snapshot, row.adjustments) == ("post_kickoff", "2026-09-13T13:08", ((WIND, -3.9),))
        assert finals["trevor lawrence:QB"] == 26.1

    def test_legacy_log_used_only_when_nothing_newer_exists(self):
        legacy_m = {k: v for k, v in _m("A", "WR", None, 10.0).items() if k not in ("adjustments", "game_state", "live_pts")}
        rows, _ = wg.collect([_log("a", [legacy_m])])
        assert rows["a:WR"].source == "legacy"

    def test_final_log_points_preferred_over_fetched(self):
        logs = [
            _log("a", [_m("Cam Little", "K", 6.5, 5.2)]),
            _log("b", [_m("Cam Little", "K", 6.5, 5.2, state="FINAL", live=12.0)]),
        ]
        g = wg.grade(2026, {1: logs}, {1: {"cam little": 3.0}}, finished_by_week={1: {"JAX"}})
        assert g.players[0].actual == 12.0

    def test_fetched_points_used_only_once_the_game_is_finished(self):
        logs = {1: [_log("a", [_m("Rashee Rice", "WR", 12.8, 12.9, team="KC")])]}
        fetched = {1: {"rashee rice": 9.0}}
        assert wg.grade(2026, logs, fetched, finished_by_week={1: {"KC"}}).players[0].actual == 9.0
        mid_game = wg.grade(2026, logs, fetched, finished_by_week={1: {"JAX"}})
        assert mid_game.players == [] and [r.name for r in mid_game.ungraded] == ["Rashee Rice"]

    def test_no_schedule_trusts_no_fetched_score(self):
        g = wg.grade(2026, {1: [_log("a", [_m("Rashee Rice", "WR", 12.8, 12.9, team="KC")])]}, {1: {"rashee rice": 9.0}})
        assert g.players == []

    def test_finished_teams_needs_the_full_game_window(self):
        from datetime import datetime, timedelta

        class G:
            def __init__(self, kickoff):
                self.kickoff = kickoff

        kick = datetime(2026, 9, 14, 20, 15)
        games = {"KC": G(kick), "DEN": G(kick), "SEA": G(datetime(2026, 9, 9, 20, 20)), "BYE": G(None)}
        identity = lambda t: t  # noqa: E731 -- treat ET as UTC for the test
        assert wg.finished_teams(games, kick + timedelta(hours=2), identity) == {"SEA"}
        assert wg.finished_teams(games, kick + timedelta(hours=wg.GAME_HOURS), identity) == {"SEA", "KC", "DEN"}

    def test_player_without_actual_is_ungraded(self):
        g = wg.grade(2026, {1: [_log("a", [_m("Cam Ward", "QB", 17.4, 17.4, team="TEN")])]})
        assert g.players == []
        assert [r.name for r in g.ungraded] == ["Cam Ward"]


class TestFamilyAggregation:
    def test_jax_cle_week_1_wind_hurt_three_of_four(self):
        pre = _log("2026-09-13T12:00", [
            _m("Trevor Lawrence", "QB", 18.9, 15.2, [(WIND, -3.9), ("Vegas: JAX implied 24", 0.2)]),
            _m("Cam Little", "K", 6.5, 5.2, [(WIND, -1.4), ("Vegas: JAX implied 24", 0.1)]),
        ], bench=[
            _m("Parker Washington", "WR", 12.1, 9.8, [(WIND, -2.5), ("Vegas: JAX implied 24", 0.1)]),
            _m("Quinshon Judkins", "RB", 11.1, 8.9, [(WIND, -1.2), ("Vegas: CLE implied 15.5", -0.6), ("opponent", -0.5)], team="CLE"),
        ])
        actuals = {"trevor lawrence": 26.1, "cam little": 12.0, "parker washington": 19.3, "quinshon judkins": 7.0}
        g = wg.grade(2026, {1: [pre]}, {1: actuals}, finished_by_week={1: {"JAX", "CLE"}})
        weather = next(f for f in g.families if f.family == "weather")
        assert (weather.rows, weather.helped, weather.hurt) == (4, 1, 3)
        assert weather.total_delta == pytest.approx(-9.0)
        assert weather.net_points < 0
        assert {f.family for f in g.families} == {"weather", "Vegas", "opponent"}

    def test_family_gain_holds_other_adjustments_fixed(self):
        row = wg.ProjectedRow("k", "P", "WR", "X", 10.0, 7.0, (("weather: w", -4.0), ("Vegas: v", 1.0)), "s")
        p = wg.PlayerGrade(row=row, actual=11.0, week=1)
        # without weather: 11.0 -> error 0; with it: 7.0 -> error 4
        assert p.family_gain("weather") == pytest.approx(-4.0)
        # without Vegas: 6.0 -> error 5; with it: error 4
        assert p.family_gain("Vegas") == pytest.approx(1.0)

    def test_position_mae(self):
        g = wg.grade(
            2026, {1: [_log("a", [_m("A", "WR", 10.0, 8.0, [("weather: w", -2.0)], state="", live=None)])]},
            {1: {"a": 12.0}}, finished_by_week={1: {"JAX"}},
        )
        assert g.positions[0].baseline_mae == pytest.approx(2.0)
        assert g.positions[0].ours_mae == pytest.approx(4.0)


class TestLoadAndRender:
    def test_load_filters_season_and_week_and_sorts(self, tmp_path):
        for name, log in {
            "2026-w01-b.json": _log("2026-09-13T12:00"),
            "2026-w01-a.json": _log("2026-09-09T12:00"),
            "2026-w02-a.json": _log("2026-09-20T12:00", week=2),
        }.items():
            (tmp_path / name).write_text(json.dumps(log), encoding="utf-8")
        (tmp_path / "2026-w01-broken.json").write_text("{", encoding="utf-8")
        logs = wg.load_week_logs(2026, 1, tmp_path)
        assert list(logs) == [1]
        assert [l["generated_at"] for l in logs[1]] == ["2026-09-09T12:00", "2026-09-13T12:00"]
        assert set(wg.load_week_logs(2026, None, tmp_path)) == {1, 2}

    def test_render_and_json_are_consistent(self):
        g = wg.grade(
            2026, {1: [_log("a", [_m("A", "WR", 10.0, 8.0, [("weather: w", -2.0)])])]},
            {1: {"a": 12.0}}, finished_by_week={1: {"JAX"}},
        )
        text = "\n".join(wg.render(g))
        assert "weather" in text and "hypothesis" in text
        assert json.loads(json.dumps(wg.to_json(g)))["families"][0]["hurt"] == 1


class TestFetchActuals:
    def test_fetch_failure_is_an_alert_not_a_crash(self):
        from ffbot.sleeper.cache import SleeperFetchError

        class Broken:
            def players(self):
                raise SleeperFetchError("simulated outage")

            def matchups(self, *a, **k):
                raise SleeperFetchError("simulated outage")

        points, alerts = wg.fetch_actuals(Broken(), "L", 1)
        assert points == {}
        assert len(alerts) == 1 and "unavailable" in alerts[0]

    def test_fetch_maps_players_points_by_name(self):
        class Fake:
            def players(self):
                return {"1": {"first_name": "Trevor", "last_name": "Lawrence", "position": "QB"}}

            def matchups(self, league_id, week):
                return [{"players_points": {"1": 26.1}}]

        points, alerts = wg.fetch_actuals(Fake(), "L", 1)
        assert alerts == []
        assert points == {"trevor lawrence": 26.1}


def _pg(week, team, actual, label="weather: w"):
    """Sleeper 10, ours 8 via a -2 adjustment: actual 11 -> that family added
    2 points of error (hurt); actual 7 -> removed 2 (helped)."""
    row = wg.ProjectedRow(f"{team}{week}{actual}", f"P{team}", "WR", team, 10.0, 8.0, ((label, -2.0),), "s")
    return wg.PlayerGrade(row=row, actual=actual, week=week)


class TestEvidenceAndProposals:
    def test_one_game_is_one_observation_however_many_rows(self):
        players = [_pg(1, "JAX", 26.0), _pg(1, "JAX", 19.0), _pg(1, "JAX", 12.0), _pg(1, "CLE", 11.0)]
        ev = wg.family_evidence(players, {1: {"JAX": "CLE", "CLE": "JAX"}})
        weather = next(e for e in ev if e.family == "weather")
        assert (weather.rows, weather.games, weather.ci_low) == (4, 1, None)
        assert wg.proposals(ev, min_weeks=1, min_games=1) == []

    def test_consistent_harm_over_enough_games_proposes_weaker(self):
        players = [_pg(wk, f"T{i}", 11.0) for i in range(10) for wk in [1 + i // 2]]
        ev = wg.family_evidence(players)
        props = wg.proposals(ev, min_weeks=4, min_games=8)
        assert [(p.dial, p.direction) for p in props] == [("weather_weight", "weaker")]
        assert "nothing was changed" in props[0].text
        assert props[0].evidence.mean_per_game == pytest.approx(-2.0)

    def test_consistent_help_proposes_stronger(self):
        players = [_pg(1 + i // 2, f"T{i}", 7.0) for i in range(10)]
        props = wg.proposals(wg.family_evidence(players), min_weeks=4, min_games=8)
        assert [p.direction for p in props] == ["stronger"]

    def test_mixed_results_propose_nothing(self):
        players = [_pg(1 + i // 2, f"T{i}", 11.0 if i % 2 else 7.0) for i in range(10)]
        ev = wg.family_evidence(players)
        assert ev[0].ci_low < 0 < ev[0].ci_high
        assert wg.proposals(ev, min_weeks=4, min_games=8) == []

    def test_too_few_weeks_proposes_nothing(self):
        players = [_pg(1, f"T{i}", 11.0) for i in range(10)]
        assert wg.proposals(wg.family_evidence(players), min_weeks=4, min_games=8) == []

    def test_a_family_without_a_dial_is_evidence_only(self):
        players = [_pg(1 + i // 2, f"T{i}", 11.0, label="research trend/volatility") for i in range(10)]
        ev = wg.family_evidence(players)
        assert ev[0].dial is None
        assert wg.proposals(ev, min_weeks=4, min_games=8) == []

    def test_notification_says_why_nothing_was_proposed(self):
        g = wg.WeekGrade(season=2026, weeks=(1,), players=[_pg(1, "JAX", 26.0)], ungraded=[])
        g.families, g.positions = wg.summarize(g.players)
        ev = wg.family_evidence(g.players)
        lines = wg.notification_lines(g, ev, [], 4, 8)
        assert any("No change proposed" in l for l in lines)
        assert any(l.startswith("weather:") for l in lines)


class TestDescriptiveOnly:
    def test_nothing_writes_a_proposal_into_configuration(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("ffbot/week_grade.py", "scripts/grade_week.py"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "safe_dump" not in text and "config.local" not in text, rel

    def test_nothing_in_ffbot_imports_the_grader(self):
        """Grades are evidence for a human and a backtest; no valuation path may
        read them back (the season_ptd rule)."""
        root = Path(__file__).resolve().parents[1] / "ffbot"
        import_re = re.compile(
            r"^\s*(?:from\s+[\w.]*week_grade\s+import|import\s+[\w.]*week_grade\b|from\s+[\w.]+\s+import\s+.*\bweek_grade\b)",
            re.MULTILINE,
        )
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if p.name != "week_grade.py" and import_re.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == []
