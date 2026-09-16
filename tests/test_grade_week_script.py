from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import Config, GradeConfig
from ffbot.live.schedule import LiveGame, ScheduleError
from scripts import grade_week

WIND = "weather: wind 40 mph"
WEEK1 = datetime(2026, 9, 13, 13, 0)
WEEK2 = datetime(2026, 9, 20, 13, 0)
TUESDAY = datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc)


def _m(name, team, sleeper, ours, adjustments=()):
    return {
        "name": name, "position": "WR", "team": team, "board_key": f"{name.lower()}:WR",
        "sleeper_proj": sleeper, "week_proj": ours,
        "adjustments": [{"label": l, "delta": d} for l, d in adjustments], "game_state": "", "live_pts": None,
    }


def _write_log(log_dir: Path, week: int, metrics: list[dict]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = {
        "season": 2026, "week": week, "generated_at": f"2026-09-{6 + 7 * week:02d}T10:00",
        "lineup_current": {"starters": [{"slot": "WR", "metrics": m} for m in metrics], "bench": []},
    }
    (log_dir / f"2026-w{week:02d}-test.json").write_text(json.dumps(log), encoding="utf-8")


def _games(home, away, kickoff):
    return {
        home: LiveGame(opponent=away, home=True, roof="outdoors", kickoff=kickoff),
        away: LiveGame(opponent=home, home=False, roof="outdoors", kickoff=kickoff),
    }


class FakeClient:
    def __init__(self, points: dict[str, float]):
        self._names = list(points)
        self._points = list(points.values())

    def players(self):
        return {str(i): {"full_name": n} for i, n in enumerate(self._names)}

    def matchups(self, league_id, week):
        return [{"players_points": {str(i): p for i, p in enumerate(self._points)}}]


def _cfg(**grade):
    cfg = Config()
    return dataclasses.replace(cfg, grade=GradeConfig(**grade), sleeper=dataclasses.replace(cfg.sleeper, league_id="L"))


def _two_week_schedule(season, week, refresh=True):
    return _games("JAX", "CLE", WEEK1) if week == 1 else _games("SEA", "NE", WEEK2)


class TestScheduledGrade:
    def test_grades_the_latest_finished_week_and_writes_both_files(self, tmp_path):
        logs, out = tmp_path / "logs", tmp_path / "grades"
        _write_log(logs, 1, [_m("Trevor Lawrence", "JAX", 18.9, 15.2, [(WIND, -3.9)])])
        _write_log(logs, 2, [_m("Geno Smith", "SEA", 15.0, 15.0)])  # not played yet
        client = FakeClient({"Trevor Lawrence": 26.1, "Geno Smith": 0.0})

        result = grade_week.scheduled_grade(
            2026, 2, _cfg(), log_dir=logs, out_dir=out, client=client,
            schedule_fn=_two_week_schedule, now_utc=TUESDAY,
        )

        assert result.week == 1
        title, body = result.message
        assert "W1" in title and "proposed" not in title
        assert "weather" in body and "No change proposed" in body
        season = json.loads((out / "2026-season.json").read_text(encoding="utf-8"))
        assert season["weeks"] == [1]
        assert [p["name"] for p in season["players"]] == ["Trevor Lawrence"]
        assert season["proposals"] == []
        assert (out / "2026-w01.json").exists()

    def test_nothing_finished_means_no_grade_so_autorun_retries(self, tmp_path):
        _write_log(tmp_path / "logs", 1, [_m("Trevor Lawrence", "JAX", 18.9, 15.2, [(WIND, -3.9)])])
        result = grade_week.scheduled_grade(
            2026, 1, _cfg(), log_dir=tmp_path / "logs", out_dir=tmp_path / "grades",
            client=FakeClient({}), schedule_fn=_two_week_schedule,
            now_utc=datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc),
        )
        assert (result.week, result.message) == (None, None)
        assert any("no logged week has finished" in a for a in result.alerts)

    def test_schedule_failure_is_an_alert_not_a_crash(self, tmp_path):
        _write_log(tmp_path / "logs", 1, [_m("Trevor Lawrence", "JAX", 18.9, 15.2, [(WIND, -3.9)])])

        def down(season, week, refresh=True):
            raise ScheduleError("simulated outage")

        result = grade_week.scheduled_grade(
            2026, 2, _cfg(), log_dir=tmp_path / "logs", out_dir=tmp_path / "grades",
            client=FakeClient({}), schedule_fn=down, now_utc=TUESDAY,
        )
        assert result.week is None
        assert any("schedule unavailable" in a for a in result.alerts)

    def test_consistent_evidence_becomes_a_proposal_in_the_push(self, tmp_path):
        logs = tmp_path / "logs"
        _write_log(logs, 1, [
            _m("Trevor Lawrence", "JAX", 10.0, 8.0, [(WIND, -2.0)]),
            _m("Geno Smith", "SEA", 10.0, 8.0, [(WIND, -2.0)]),
        ])
        schedule = lambda season, week, refresh=True: {**_games("JAX", "CLE", WEEK1), **_games("SEA", "NE", WEEK1)}  # noqa: E731
        result = grade_week.scheduled_grade(
            2026, 2, _cfg(min_weeks=1, min_games=2), log_dir=logs, out_dir=tmp_path / "grades",
            client=FakeClient({"Trevor Lawrence": 11.0, "Geno Smith": 11.0}), schedule_fn=schedule, now_utc=TUESDAY,
        )
        title, body = result.message
        assert "proposed" in title
        assert "PROPOSAL" in body and "weather_weight" in body
        assert [(p.dial, p.direction) for p in result.proposals] == [("weather_weight", "weaker")]

    def test_schedule_file_downloaded_once_per_run(self, tmp_path):
        logs = tmp_path / "logs"
        _write_log(logs, 1, [_m("Trevor Lawrence", "JAX", 18.9, 15.2)])
        _write_log(logs, 2, [_m("Geno Smith", "SEA", 15.0, 15.0)])
        refreshes = []

        def counting(season, week, refresh=True):
            refreshes.append(refresh)
            return _two_week_schedule(season, week)

        grade_week.scheduled_grade(
            2026, 2, _cfg(), log_dir=logs, out_dir=tmp_path / "grades",
            client=FakeClient({}), schedule_fn=counting, now_utc=TUESDAY,
        )
        assert refreshes == [True, False]


class TestGradeConfig:
    def test_off_in_code(self):
        assert GradeConfig().enabled is False
        assert Config().grade == GradeConfig()

    def test_shipped_config_grades_tuesday_morning(self):
        cfg = Config.load(Path(__file__).resolve().parents[1] / "config.yml")
        assert cfg.grade.enabled is True
        assert (cfg.grade.weekday, cfg.grade.hour) == ("tue", 8)
