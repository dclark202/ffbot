from __future__ import annotations

import datetime as dt
import json

from ffbot.markets import kalshi_log


class TestModuleTouchesNoNetwork:
    """Structural guarantee, not just by inspection -- mirrors
    tests/test_history_index.py::TestAsOfLeakageGuarantee's pattern of
    proving a live-adjacent module can't reach the network, rather than
    merely asserting it in prose. This module is pure file I/O: it must
    import nothing network-capable at all.
    """

    def test_module_imports_no_networking_libraries(self):
        import ffbot.markets.kalshi_log as mod

        source = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("urllib", "http.client", "socket", "requests"):
            assert forbidden not in source, f"kalshi_log.py must never import {forbidden!r}"


class TestLogWeeklySnapshot:
    def test_empty_scores_returns_none_and_writes_nothing(self, tmp_path):
        result = kalshi_log.log_weekly_snapshot(2026, 3, {}, log_dir=tmp_path)
        assert result is None
        assert list(tmp_path.iterdir()) == []

    def test_nonempty_scores_writes_one_jsonl_line(self, tmp_path):
        when = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)
        path = kalshi_log.log_weekly_snapshot(
            2026, 3, {"josh allen:QB": 0.72}, {"BUF": {"team_total": 27.5, "opp_total": 20.0}},
            log_dir=tmp_path, now=when,
        )
        assert path == tmp_path / "2026.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["season"] == 2026
        assert rows[0]["week"] == 3
        assert rows[0]["player_prop_scores"] == {"josh allen:QB": 0.72}
        assert rows[0]["game_odds"] == {"BUF": {"team_total": 27.5, "opp_total": 20.0}}
        assert rows[0]["logged_at"] == when.isoformat()

    def test_game_odds_defaults_to_empty_dict(self, tmp_path):
        path = kalshi_log.log_weekly_snapshot(2026, 3, {"josh allen:QB": 0.72}, log_dir=tmp_path)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["game_odds"] == {}

    def test_successive_calls_append_not_overwrite(self, tmp_path):
        kalshi_log.log_weekly_snapshot(2026, 3, {"a:QB": 0.1}, log_dir=tmp_path)
        kalshi_log.log_weekly_snapshot(2026, 4, {"a:QB": 0.2}, log_dir=tmp_path)
        path = tmp_path / "2026.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [r["week"] for r in rows] == [3, 4]

    def test_different_seasons_write_different_files(self, tmp_path):
        kalshi_log.log_weekly_snapshot(2025, 1, {"a:QB": 0.1}, log_dir=tmp_path)
        kalshi_log.log_weekly_snapshot(2026, 1, {"a:QB": 0.1}, log_dir=tmp_path)
        assert (tmp_path / "2025.jsonl").exists()
        assert (tmp_path / "2026.jsonl").exists()

    def test_unwritable_log_dir_degrades_to_none_not_a_crash(self, tmp_path, monkeypatch):
        # A file where a directory is expected -- Path.mkdir(parents=True)
        # fails with FileExistsError/OSError; log_weekly_snapshot must
        # never let that escape, same never-crash contract as every other
        # live seam in this repo.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        result = kalshi_log.log_weekly_snapshot(2026, 1, {"a:QB": 0.1}, log_dir=blocker / "nested")
        assert result is None
