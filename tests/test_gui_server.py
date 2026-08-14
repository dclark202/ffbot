from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from scripts.gui import GuiServer, Handler, parse_args


def _write_board_csv(tmp_path: Path) -> Path:
    rows = []
    counts = {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}
    n = 0
    for pos, c in counts.items():
        for i in range(c):
            n += 1
            rows.append(f"P{pos}{i},XXX,{pos},{5 + n % 10},{300 - n},{n}")
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS,AVG\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_config(tmp_path: Path, board_csv: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "sleeper:\n  league_id: \"\"\n"
        "roster_positions:\n"
        "  QB: 1\n  WR: 2\n  RB: 2\n  TE: 1\n  W/R/T: 1\n  K: 1\n  DEF: 1\n  BN: 6\n  IR: 1\n"
        "draft:\n"
        "  num_teams: 12\n"
        "  my_slot: 1\n"
        "  rounds: 15\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n",
        encoding="utf-8",
    )
    return path


def _write_roster(tmp_path: Path) -> Path:
    path = tmp_path / "roster.yml"
    path.write_text("players:\n  - PQB0\n  - PRB0\n", encoding="utf-8")
    return path


class _LiveServer:
    """Runs a `GuiServer` on an OS-assigned port in a background thread, for
    exercising the real HTTP protocol path end to end."""

    def __init__(self, tmp_path: Path, extra_args: list[str] | None = None):
        self.tmp_path = tmp_path
        board_csv = _write_board_csv(tmp_path)
        self.config_path = _write_config(tmp_path, board_csv)
        self.roster_path = _write_roster(tmp_path)
        self.log_path = tmp_path / "draft_log.jsonl"
        args = parse_args(
            [
                "--config", str(self.config_path),
                "--roster", str(self.roster_path),
                "--log", str(self.log_path),
                "--state", str(tmp_path / "lineup_state.yml"),
                "--league-rosters", str(tmp_path / "league_rosters.yml"),
            ]
            + (extra_args or [])
        )
        self.server = GuiServer(("127.0.0.1", 0), Handler, args)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            payload = json.loads(raw) if raw else {}
            return res.status, payload
        finally:
            conn.close()

    def raw_get(self, path: str) -> tuple[int, str, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, res.getheader("Content-Type", ""), res.read()
        finally:
            conn.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    # draft_store's save/load default to a cwd-relative "draft/saves" (same
    # as scripts/draft.py's local "save"/"load" commands) -- chdir into
    # tmp_path so a test save doesn't land in the real repo's draft/ dir.
    monkeypatch.chdir(tmp_path)
    server = _LiveServer(tmp_path)
    yield server
    server.close()


class TestStaticPages:
    def test_pages_serve_html(self, live):
        for path in ("/", "/draft", "/weekly", "/settings"):
            status, ctype, body = live.raw_get(path)
            assert status == 200
            assert "text/html" in ctype
            assert b"<html" in body.lower() or b"<!doctype" in body.lower()

    def test_style_and_common_js_serve(self, live):
        status, ctype, body = live.raw_get("/style.css")
        assert status == 200 and "css" in ctype
        status, ctype, body = live.raw_get("/common.js")
        assert status == 200 and "javascript" in ctype

    def test_unknown_path_is_404(self, live):
        status, _, _ = live.raw_get("/nope")
        assert status == 404


class _FakeSync:
    """Stands in for `ffbot.draft_sync.DraftSync` -- only the interface
    `scripts/gui.py` actually calls (`start`/`stop`/`drain`/`status`/
    `unmapped_count`), matching the real class's public surface."""

    def __init__(self, items=None, status="live", unmapped=0):
        self._items = list(items or [])
        self._status = status
        self._unmapped = unmapped
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def drain(self):
        items, self._items = self._items, []
        return items

    def status(self):
        return self._status

    def unmapped_count(self):
        return self._unmapped


class TestDraftSyncApi:
    """`--sync` in the GUI: no TUI blocking input() to wait on, so a synced
    pick must surface on the very next request rather than only after a
    keystroke -- see scripts/gui.py's `_drain_sync`."""

    def test_sync_flag_without_ids_file_leaves_sync_off(self, tmp_path, monkeypatch):
        # _build_sync's own existing "ids file missing" fallback -- no
        # network touched, server still starts fine.
        monkeypatch.chdir(tmp_path)
        server = _LiveServer(tmp_path, extra_args=["--sync", "--ids-file", str(tmp_path / "nope.json")])
        try:
            status, data = server.request("GET", "/api/draft/state")
            assert status == 200
            assert data["header"]["sync_status"] == "off"
        finally:
            server.close()

    def test_sync_flag_starts_sync_and_drains_a_queued_pick_on_next_request(self, tmp_path, monkeypatch):
        import scripts.gui as gui_module
        from ffbot.draft_sync import SyncedPick

        monkeypatch.chdir(tmp_path)

        # _build_sync itself is exercised directly in test_draft_script.py;
        # here we only need scripts/gui.py's OWN wiring (construct on
        # startup, drain on every request) to be correct, so stub it.
        fake = _FakeSync()
        monkeypatch.setattr(gui_module, "_build_sync", lambda args, state: fake)

        server = _LiveServer(tmp_path, extra_args=["--sync"])
        try:
            assert fake.started is True
            status, data = server.request("GET", "/api/draft/state")
            assert data["header"]["sync_status"] == "live"

            key = data["recommendations"][0]["key"]
            fake._items = [SyncedPick(number=1, key=key, mine=True)]
            status, data = server.request("GET", "/api/draft/state")
            assert status == 200
            assert data["header"]["pick"] == 2
            assert any(p["key"] == key for p in data["roster"])
            assert '"sync"' in server.log_path.read_text(encoding="utf-8")
        finally:
            server.close()
        assert fake.stopped is True

    def test_unmapped_count_surfaces_in_state(self, tmp_path, monkeypatch):
        import scripts.gui as gui_module

        monkeypatch.chdir(tmp_path)
        fake = _FakeSync(unmapped=3)
        monkeypatch.setattr(gui_module, "_build_sync", lambda args, state: fake)

        server = _LiveServer(tmp_path, extra_args=["--sync"])
        try:
            status, data = server.request("GET", "/api/draft/state")
            assert data["header"]["sync_unmapped"] == 3
        finally:
            server.close()

    def test_no_sync_flag_leaves_sync_none_and_status_off(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        server = _LiveServer(tmp_path)
        try:
            assert server.server.sync is None
            status, data = server.request("GET", "/api/draft/state")
            assert data["header"]["sync_status"] == "off"
        finally:
            server.close()


class TestReportsApi:
    def test_page_serves(self, live):
        status, ctype, body = live.raw_get("/reports")
        assert status == 200 and "text/html" in ctype

    def test_empty_list_when_no_reports_dir(self, live):
        status, data = live.request("GET", "/api/reports")
        assert status == 200
        assert data["reports"] == []

    def test_lists_written_reports_newest_first(self, live):
        reports_dir = live.tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "old.md").write_text("old content", encoding="utf-8")
        import time
        time.sleep(0.01)
        (reports_dir / "new.md").write_text("new content", encoding="utf-8")

        status, data = live.request("GET", "/api/reports")
        assert status == 200
        assert [r["filename"] for r in data["reports"]] == ["new.md", "old.md"]

    def test_content_returns_file_body(self, live):
        reports_dir = live.tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "a.md").write_text("# Week 1 Report\n\nhello", encoding="utf-8")

        status, data = live.request("GET", "/api/reports/content?file=a.md")
        assert status == 200
        assert data["content"] == "# Week 1 Report\n\nhello"

    def test_content_missing_file_is_404(self, live):
        status, data = live.request("GET", "/api/reports/content?file=nope.md")
        assert status == 404

    def test_content_path_traversal_is_404_not_a_leak(self, live):
        secret = live.tmp_path / "config.yml"
        status, data = live.request("GET", "/api/reports/content?file=..%2Fconfig.yml")
        assert status == 404

    def test_content_missing_query_param_is_400(self, live):
        status, data = live.request("GET", "/api/reports/content")
        assert status == 400


class TestDraftApi:
    def test_state_reflects_a_fresh_draft(self, live):
        status, data = live.request("GET", "/api/draft/state")
        assert status == 200
        assert data["header"]["pick"] == 1
        assert len(data["recommendations"]) > 0

    def test_command_records_a_pick_and_persists_to_log(self, live):
        status, data = live.request("POST", "/api/draft/command", {"line": "PQB0"})
        assert status == 200
        assert data["header"]["pick"] == 2
        assert live.log_path.exists()
        assert '"line": "PQB0"' in live.log_path.read_text(encoding="utf-8")

    def test_pick_by_exact_key(self, live):
        status, state = live.request("GET", "/api/draft/state")
        key = state["recommendations"][0]["key"]
        status, data = live.request("POST", "/api/draft/pick", {"key": key, "mine": True})
        assert status == 200
        assert any(p["key"] == key for p in data["roster"])
        assert '"pick"' in live.log_path.read_text(encoding="utf-8")

    def test_duplicate_pick_returns_400(self, live):
        status, state = live.request("GET", "/api/draft/state")
        key = state["recommendations"][0]["key"]
        live.request("POST", "/api/draft/pick", {"key": key, "mine": True})
        status, data = live.request("POST", "/api/draft/pick", {"key": key, "mine": True})
        assert status == 400
        assert "error" in data

    def test_search_returns_ranked_matches(self, live):
        status, data = live.request("GET", "/api/draft/search?q=PQB")
        assert status == 200
        assert data["matches"]
        assert all("PQB" in m["name"] for m in data["matches"])

    def test_search_excludes_taken_players(self, live):
        status, state = live.request("GET", "/api/draft/state")
        key = state["recommendations"][0]["key"]
        name = state["recommendations"][0]["name"]
        live.request("POST", "/api/draft/pick", {"key": key, "mine": True})
        status, data = live.request("GET", f"/api/draft/search?q={name[:4]}")
        assert status == 200
        assert key not in {m["key"] for m in data["matches"]}

    def test_search_respects_limit(self, live):
        status, data = live.request("GET", "/api/draft/search?q=P&limit=2")
        assert status == 200
        assert len(data["matches"]) <= 2

    def test_search_empty_query_returns_no_matches(self, live):
        status, data = live.request("GET", "/api/draft/search?q=")
        assert status == 200
        assert data["matches"] == []

    def test_reset_archives_log_and_starts_fresh(self, live):
        live.request("POST", "/api/draft/command", {"line": "PQB0"})
        status, data = live.request("POST", "/api/draft/reset", {})
        assert status == 200
        assert data["header"]["pick"] == 1
        assert not live.log_path.exists()
        archives = list(live.tmp_path.glob("draft_log.*.jsonl"))
        assert len(archives) == 1

    def test_save_and_load_round_trip(self, live):
        live.request("POST", "/api/draft/command", {"line": "PQB0"})
        status, data = live.request("POST", "/api/draft/save", {"name": "mydraft"})
        assert status == 200
        status, saves = live.request("GET", "/api/draft/saves")
        assert "mydraft" in saves["saves"]

        live.request("POST", "/api/draft/command", {"line": "PRB0"})
        status, state = live.request("GET", "/api/draft/state")
        assert state["header"]["pick"] == 3

        status, data = live.request("POST", "/api/draft/load", {"name": "mydraft"})
        assert status == 200
        assert data["header"]["pick"] == 2

    def test_load_unknown_save_returns_400(self, live):
        status, data = live.request("POST", "/api/draft/load", {"name": "nope"})
        assert status == 400


class TestRosterApi:
    def test_get_returns_seeded_roster(self, live):
        status, data = live.request("GET", "/api/roster")
        assert status == 200
        assert [e["name"] for e in data["entries"]] == ["PQB0", "PRB0"]

    def test_post_round_trips_flags(self, live):
        entries = [{"name": "PQB0", "undroppable": True}, {"name": "PWR0"}]
        status, data = live.request("POST", "/api/roster", {"entries": entries})
        assert status == 200
        status, data = live.request("GET", "/api/roster")
        assert data["entries"][0]["undroppable"] is True
        assert data["entries"][1]["name"] == "PWR0"


class TestWeeklyIntelApi:
    def test_missing_week_query_param_is_400(self, live):
        status, data = live.request("GET", "/api/weekly-intel")
        assert status == 400

    def test_matchup_round_trips_mirrored(self, live):
        payload = {
            "week": 3,
            "matchups": [{"home_team": "BUF", "away_team": "MIA", "wind_mph": 12}],
            "players": {},
        }
        status, data = live.request("POST", "/api/weekly-intel?week=3", payload)
        assert status == 200
        status, data = live.request("GET", "/api/weekly-intel?week=3")
        assert status == 200
        assert len(data["matchups"]) == 1
        assert data["matchups"][0]["home_team"] == "BUF"
        assert data["matchups"][0]["wind_mph"] == 12


class TestWeeklyRunApi:
    def test_run_without_week_is_400(self, live):
        status, data = live.request("POST", "/api/weekly/run", {})
        assert status == 400

    def test_run_produces_a_lineup(self, live):
        status, data = live.request("POST", "/api/weekly/run", {"week": 1})
        assert status == 200
        assert data["week"] == 1
        assert "lineup" in data
        assert not (live.tmp_path / "lineup_state.yml").exists()  # commit defaulted False

    def test_commit_true_writes_lineup_state(self, live):
        status, data = live.request("POST", "/api/weekly/run", {"week": 1, "commit": True})
        assert status == 200
        assert data["committed"] is True
        assert (live.tmp_path / "lineup_state.yml").exists()


class TestSettingsApi:
    def test_get_reflects_config(self, live):
        status, data = live.request("GET", "/api/settings")
        assert status == 200
        assert data["draft"]["num_teams"] == 12
        assert data["draft"]["order"] == "snake"
        assert data["data_source"] == "manual"

    def test_post_writes_overlay_and_get_reflects_it(self, live):
        status, data = live.request("POST", "/api/settings", {"draft": {"num_teams": 10}})
        assert status == 200
        assert data["draft"]["num_teams"] == 10
        overlay = live.tmp_path / "config.local.yml"
        assert overlay.exists()
        status, data = live.request("GET", "/api/settings")
        assert data["draft"]["num_teams"] == 10

    def test_structural_change_rejected_once_picks_recorded(self, live):
        live.request("POST", "/api/draft/command", {"line": "PQB0"})
        status, data = live.request("POST", "/api/settings", {"draft": {"num_teams": 10}})
        assert status == 409
        assert "reset" in data["error"]

    def test_non_structural_change_allowed_with_picks_recorded(self, live):
        live.request("POST", "/api/draft/command", {"line": "PQB0"})
        status, data = live.request("POST", "/api/settings", {"season": {"spice_level": 4}})
        assert status == 200
        assert data["season"]["spice_level"] == 4

    def test_season_spice_level_out_of_range_is_rejected_before_writing_overlay(self, live):
        # The 1-5 scale became 1-4 in B7 -- a stale client (or a saved
        # config from before the rescale) posting "5" must be refused
        # BEFORE it lands in config.local.yml, not accepted and left to
        # crash the next Config.load call.
        status, data = live.request("POST", "/api/settings", {"season": {"spice_level": 5}})
        assert status == 400
        assert "1-4" in data["error"]
        overlay = live.tmp_path / "config.local.yml"
        assert not overlay.exists() or "spice_level: 5" not in overlay.read_text(encoding="utf-8")

    def test_season_spice_level_non_integer_is_rejected(self, live):
        status, data = live.request("POST", "/api/settings", {"season": {"spice_level": "chaos"}})
        assert status == 400

    def test_draft_spice_level_round_trips(self, live):
        status, data = live.request("POST", "/api/settings", {"draft": {"spice_level": 2}})
        assert status == 200
        assert data["draft"]["spice_level"] == 2
        status, data = live.request("GET", "/api/settings")
        assert data["draft"]["spice_level"] == 2

    def test_draft_spice_level_out_of_range_is_rejected(self, live):
        status, data = live.request("POST", "/api/settings", {"draft": {"spice_level": 5}})
        assert status == 400
        assert "1-4" in data["error"]
