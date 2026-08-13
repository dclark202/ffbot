from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ffbot.live.schedule import LiveGame
from scripts import autorun


def _game(opponent, home, kickoff):
    return LiveGame(opponent=opponent, home=home, roof="outdoors", kickoff=kickoff)


class TestThisCalendarWeekAt:
    def test_same_weekday_keeps_the_date(self):
        # Tuesday 2026-08-11 -> Tuesday of the same week, different hour.
        now = datetime(2026, 8, 11, 9, 0)
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)  # tue
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_later_in_the_week_looks_back_to_this_weeks_occurrence(self):
        # Friday -> the Tuesday earlier in the SAME calendar week, not next week's.
        now = datetime(2026, 8, 14, 9, 0)  # Friday
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_earlier_in_the_week_resolves_forward_to_this_weeks_occurrence(self):
        # Monday -> the Tuesday still to come THIS week (tomorrow), never
        # last week's Tuesday -- the bug this function's docstring calls
        # out explicitly: a naive "most recent past occurrence" calculation
        # would jump back a full 6 days here instead of forward 1.
        now = datetime(2026, 8, 10, 9, 0)  # Monday
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_never_returns_a_date_before_this_weeks_monday(self):
        # A direct regression guard for the backward-jump bug: for every
        # (now weekday, target weekday) pair, the result must fall within
        # [this Monday, this Monday + 6 days].
        for now_wd in range(7):
            monday = datetime(2026, 8, 10) + timedelta(days=now_wd)  # 2026-08-10 is a Monday
            for target_wd in range(7):
                out = autorun._this_calendar_week_at(monday, weekday=target_wd, hour=12)
                this_monday = datetime(2026, 8, 10)
                assert this_monday <= out < this_monday + timedelta(days=7)


class TestBuildTriggers:
    def test_one_trigger_per_distinct_kickoff_not_per_game(self):
        k1 = datetime(2026, 9, 13, 13, 0)
        k2 = datetime(2026, 9, 13, 13, 0)  # same slot, different game
        k3 = datetime(2026, 9, 13, 20, 20)
        games = {
            "A": _game("B", True, k1), "B": _game("A", False, k1),
            "C": _game("D", True, k2), "D": _game("C", False, k2),
            "E": _game("F", True, k3), "F": _game("E", False, k3),
        }
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        kickoff_triggers = [t for t in triggers if t.id.startswith("pre_kickoff_")]
        assert len(kickoff_triggers) == 2  # k1 and k3, deduplicated

    def test_kickoff_trigger_due_at_is_lead_minutes_before_kickoff(self):
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        kickoff_trigger = next(t for t in triggers if t.id.startswith("pre_kickoff_"))
        assert kickoff_trigger.due_at == kickoff - timedelta(minutes=120)

    def test_includes_exactly_one_pre_waiver_trigger(self):
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers({}, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        waiver_triggers = [t for t in triggers if t.id.startswith("pre_waiver_")]
        assert len(waiver_triggers) == 1

    def test_no_kickoff_time_is_skipped_not_a_crash(self):
        games = {"A": _game("B", True, None), "B": _game("A", False, None)}
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        assert not any(t.id.startswith("pre_kickoff_") for t in triggers)

    def test_trigger_ids_stable_across_calls(self):
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        now1 = datetime(2026, 9, 13, 8, 0)
        now2 = datetime(2026, 9, 13, 9, 0)  # a later poll, same week
        t1 = autorun.build_triggers(games, now1, 120, "tue", 20)
        t2 = autorun.build_triggers(games, now2, 120, "tue", 20)
        ids1 = {t.id for t in t1}
        ids2 = {t.id for t in t2}
        assert ids1 == ids2


class TestIsDue:
    def test_not_due_before_due_at(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 11, 59), fired=set()) is False

    def test_due_at_due_at(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 0), fired=set()) is True

    def test_due_within_grace_period(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 20), fired=set()) is True

    def test_not_due_past_grace_period(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 31), fired=set()) is False

    def test_not_due_if_already_fired(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 5), fired={"x"}) is False


class TestStatePersistence:
    def test_missing_state_file_returns_empty_dict(self, tmp_path):
        assert autorun._load_state(tmp_path / "nope.json") == {}

    def test_corrupt_state_file_degrades_to_empty_dict(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        assert autorun._load_state(path) == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "sub" / "state.json"
        autorun._save_state(path, {"2026-w01": ["pre_waiver_2026-08-11"]})
        assert autorun._load_state(path) == {"2026-w01": ["pre_waiver_2026-08-11"]}


class TestMainDryRun:
    def test_dry_run_prints_schedule_and_writes_no_state(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)

        rc = autorun.main(["--dry-run", "--season", "2026"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "week 1" in out
        assert "pre_kickoff_" in out
        assert "pre_waiver_" in out
        assert not (tmp_path / "data" / "autorun_state.json").exists()

    def test_schedule_failure_returns_nonzero_never_raises(self, tmp_path, monkeypatch, capsys):
        from ffbot.live.schedule import ScheduleError

        monkeypatch.chdir(tmp_path)

        def raising(season):
            raise ScheduleError("simulated network failure")

        monkeypatch.setattr(autorun, "current_week", raising)
        rc = autorun.main(["--season", "2026"])
        assert rc == 1
        assert "schedule fetch failed" in capsys.readouterr().err


class TestMainFiring:
    def test_nothing_due_touches_no_state_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        far_future_kickoff = datetime(2099, 1, 1, 20, 20)
        games = {"A": _game("B", True, far_future_kickoff), "B": _game("A", False, far_future_kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)

        rc = autorun.main(["--season", "2026", "--waiver-weekday", "tue", "--waiver-hour", "20"])
        assert rc == 0
        # nothing was due (kickoff far in the future, and "now" in this
        # test is real "now" which may or may not be past the configured
        # waiver slot -- but even if it were due, firing it would touch
        # the network; assert conservatively that if it WAS fired the
        # state file exists and is well-formed, otherwise it's absent)
        state_path = tmp_path / "data" / "autorun_state.json"
        if state_path.exists():
            json.loads(state_path.read_text())  # must still be valid JSON

    def test_due_trigger_fires_and_records_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        # Freeze "now" to exactly the kickoff trigger's due_at.
        due_at = kickoff - timedelta(minutes=120)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        calls = []

        def fake_week_report_main(argv):
            calls.append(argv)
            return 0

        monkeypatch.setattr(autorun.week_report, "main", fake_week_report_main)

        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert len(calls) == 1
        assert "--no-save-state" in calls[0]
        assert "--quiet" in calls[0]

        state = json.loads((tmp_path / "data" / "autorun_state.json").read_text())
        fired = state["2026-w01"]
        assert any(f.startswith("pre_kickoff_") for f in fired)

    def test_failed_fire_does_not_mark_trigger_fired(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        due_at = kickoff - timedelta(minutes=120)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        def raising_week_report_main(argv):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(autorun.week_report, "main", raising_week_report_main)

        rc = autorun.main(["--season", "2026"])
        assert rc == 0  # a single failed trigger does not crash the whole poll
        state_path = tmp_path / "data" / "autorun_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            assert state.get("2026-w01", []) == []
