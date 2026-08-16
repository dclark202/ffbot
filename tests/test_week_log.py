"""The per-run weekly recommendation record (`ffbot/week_log.py`).

Mirrors `tests/test_draft_report.py` and `tests/test_markets_kalshi_log.py`:
the builder is pure and must stay JSON-safe, and the single writer must never
raise into a report that already succeeded.
"""

from __future__ import annotations

import json

import pytest

from ffbot import week_log
from ffbot.gameplan import build_gameplan
from tests.test_gameplan import WEEK_NUM, _loaded


def _log(**kwargs):
    loaded = _loaded()
    plan = build_gameplan(loaded, WEEK_NUM, loaded.players, my_priority=6)
    return week_log.build_week_log(loaded, plan, season=2026, source="cli", **kwargs)


class TestBuildWeekLog:
    def test_is_json_serializable(self):
        json.dumps(_log())  # must not raise

    def test_carries_the_recommendations_with_their_metrics(self):
        log = _log()
        assert log["schema"] == 1
        assert log["week"] == WEEK_NUM
        rows = log["adds"] + log["claims"]
        assert rows, "fixture should produce at least one add or claim"
        for row in rows:
            assert row["add_metrics"] is not None
            assert row["decision"] is not None
            # The whole point of the log: the numbers, not just the sentence.
            assert row["add_metrics"]["ros_proj"] is not None
            assert row["decision"]["decision_scale"] > 0.0

    def test_records_every_rostered_player_not_just_recommended_ones(self):
        """"What did it think of the guy I left on the bench" is exactly the
        review question a recommendations-only record cannot answer."""
        log = _log()
        starters = log["lineup_recommended"]["starters"]
        assert starters
        for s in starters:
            assert s["metrics"]["name"]

    def test_stamps_which_live_source_produced_each_number(self):
        log = _log()
        assert set(log["sources"]) >= {
            "projection", "roster", "slots", "league_rosters", "season_ptd", "pool",
        }
        # The offline fixture has no live ROS board, so `ros_proj` on every
        # row is really the full-season number -- the log has to say so.
        assert log["sources"]["pool"] == "board"

    def test_stamps_the_tuning_dials(self):
        log = _log()
        assert "ros_blend" in log["config"]
        assert "recommend_count" in log["config"]

    def test_generated_at_is_injectable_for_determinism(self):
        import datetime as dt

        log = _log(now=dt.datetime(2026, 9, 10, 12, 0, 0))
        assert log["generated_at"] == "2026-09-10T12:00:00"

    def test_builder_does_no_io(self, tmp_path, monkeypatch):
        """Pure by construction, the same split `draft_report` uses."""
        monkeypatch.chdir(tmp_path)
        _log()
        assert list(tmp_path.iterdir()) == []


class TestWeekLogPath:
    def test_names_by_season_week_and_source(self, tmp_path):
        p = week_log.week_log_path(2026, 3, "cli", tmp_path)
        assert p.name == "2026-w03-cli.json"

    def test_sanitizes_a_source_with_windows_illegal_characters(self, tmp_path):
        """An autorun pre-kickoff trigger id embeds an ISO timestamp, whose
        colons cannot appear in a Windows filename."""
        p = week_log.week_log_path(2026, 3, "pre_kickoff_2026-09-10T13:00", tmp_path)
        assert ":" not in p.name
        assert p.name.startswith("2026-w03-")

    def test_distinct_sources_are_distinct_files(self, tmp_path):
        a = week_log.week_log_path(2026, 3, "gui", tmp_path)
        b = week_log.week_log_path(2026, 3, "pre_waiver", tmp_path)
        assert a != b


class TestWriteWeekLog:
    def test_writes_and_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "nested" / "deeper" / "log.json"
        assert week_log.write_week_log({"schema": 1}, dest) == dest
        assert json.loads(dest.read_text(encoding="utf-8")) == {"schema": 1}

    def test_repeat_run_of_the_same_source_overwrites_rather_than_accumulating(self, tmp_path):
        """The GUI soft-syncs every five minutes; a timestamped name would
        turn one open tab into hundreds of files a day."""
        path = week_log.week_log_path(2026, 3, "gui", tmp_path)
        week_log.write_week_log({"schema": 1, "run": 1}, path)
        week_log.write_week_log({"schema": 1, "run": 2}, path)
        assert len(list(tmp_path.glob("*.json"))) == 1
        assert json.loads(path.read_text(encoding="utf-8"))["run"] == 2

    def test_unwritable_destination_degrades_to_none_not_a_crash(self, tmp_path):
        # A FILE where the directory should be -- mkdir then fails.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        assert week_log.write_week_log({"schema": 1}, blocker / "log.json") is None


class TestModuleTouchesNoNetwork:
    def test_module_imports_no_networking_libraries(self):
        """This module records what a run decided; it must never itself be a
        reason a run reaches the network. Structural, matching
        `tests/test_markets_kalshi_log.py`'s guarantee."""
        from pathlib import Path

        source = Path("ffbot/week_log.py").read_text(encoding="utf-8")
        for lib in ("urllib", "http.client", "socket", "requests"):
            assert lib not in source, f"ffbot/week_log.py must not reference {lib}"
