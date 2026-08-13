#!/usr/bin/env python3
"""The web GUI: draft room, weekly manager, roster/weekly-intel editors,
and settings, all in one local server.

    python scripts/gui.py                     # http://127.0.0.1:8321/
    python scripts/gui.py --slot 4 --port 9000
    python scripts/gui.py --resume            # continue a draft after a crash/restart

Reuses the exact same engine as `scripts/draft.py` and
`scripts/week_report.py` — this is a second front end on the same pure
compute layer (`ffbot/draft_ui.py`, `ffbot/report.py`, `ffbot/week.py`),
not a parallel implementation. Draft commands are appended to the same
`draft_log.jsonl`, so a session started in the terminal can be resumed in
the browser and vice versa.

Single-threaded `http.server.HTTPServer` on purpose: the draft session is
one shared, mutable `UiState` with no lock, matching the "DraftState is
main-thread-only" invariant `ffbot/draft_sync.py` documents — serializing
every request through one thread keeps that safe without adding one.

This module must not import `ffbot.sleeper` at module level — same offline
invariant as `scripts/draft.py` and `scripts/week_report.py`.
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot import draft_store  # noqa: E402
from ffbot import report  # noqa: E402
from ffbot import reports_index  # noqa: E402
from ffbot import roster_editor  # noqa: E402
from ffbot import weekly_editor  # noqa: E402
from ffbot import webapi  # noqa: E402
from ffbot.config import Config, _deep_merge  # noqa: E402
from ffbot.draft_sync import apply_synced_picks  # noqa: E402  (no yahoo_fantasy_api/requests import in this module)
from ffbot.draft_ui import UiState, handle  # noqa: E402
from scripts.draft import (  # noqa: E402
    _append_log,
    _append_pick_log,
    _append_sync_log,
    _build_sync,
    build_state as build_draft_state,
    handle_local_command,
    replay_log,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_PAGE_FOR = {
    "/": WEB_DIR / "index.html",
    "/draft": WEB_DIR / "draft.html",
    "/weekly": WEB_DIR / "weekly.html",
    "/settings": WEB_DIR / "settings.html",
    "/reports": WEB_DIR / "reports.html",
}

_SETTINGS_KEYS = {"sleeper", "draft", "season", "roster_positions"}


class GuiError(Exception):
    """An action couldn't complete — carries the HTTP status to report."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml (default: config.yml)")
    p.add_argument("--roster", default="roster.yml", help="path to roster.yml (default: roster.yml)")
    p.add_argument("--board", action="append", default=None, help="FantasyPros CSV path (repeatable); overrides config.yml's draft.board_csv")
    p.add_argument("--slot", type=int, default=None, help="your draft slot (1-indexed)")
    p.add_argument("--teams", type=int, default=None, help="override draft.num_teams")
    p.add_argument("--rounds", type=int, default=None, help="override draft.rounds")
    p.add_argument("--order", choices=["snake", "linear"], default=None, help="override draft.order")
    p.add_argument("--log", default="draft_log.jsonl", help="draft command log path (default: draft_log.jsonl)")
    p.add_argument("--resume", action="store_true", help="replay --log before serving")
    p.add_argument("--state", default="weekly/lineup_state.yml", help="remembered lineup slots (default: weekly/lineup_state.yml)")
    p.add_argument("--league-rosters", default="league_rosters.yml", help="path to league_rosters.yml")
    p.add_argument("--weeks-in-season", type=int, default=17, help="for season-board fallback scaling (default: 17)")
    p.add_argument("--reports-dir", default="reports", help="where scripts/autorun.py's generated reports live (default: reports/)")
    p.add_argument("--sync", action="store_true", help="poll Sleeper's live draft picks in the background (no auth needed) -- same as scripts/draft.py --sync")
    p.add_argument("--draft-id", default=None, help="Sleeper draft id for --sync (default: resolved from sleeper.league_id's current draft)")
    p.add_argument("--ids-file", default="draft/sleeper_ids.json", help="board-key -> Sleeper player id map from `draft_export.py --reconcile` (default: draft/sleeper_ids.json)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8321, help="bind port (default: 8321)")
    return p.parse_args(argv)


class GuiServer(http.server.HTTPServer):
    def __init__(self, addr: tuple[str, int], handler_cls, args: argparse.Namespace):
        super().__init__(addr, handler_cls)
        self.args = args
        self.draft_log_path = Path(args.log)
        self.draft_ui_state: UiState | None = None
        self.sync = None
        try:
            self.draft_ui_state = build_draft_state(args)
            if args.resume:
                self.draft_ui_state = replay_log(self.draft_ui_state, self.draft_log_path)
        except SystemExit:
            # build_state() already printed why (no board configured) --
            # the draft room just stays unavailable until one is.
            pass

        # Reuses scripts/draft.py's exact _build_sync -- same best-effort
        # contract (any failure prints a warning and leaves self.sync=None,
        # never takes down the server). Unlike the TUI, nothing here blocks
        # on user input, so _drain_sync (below) runs on every request rather
        # than once per keystroke -- a synced pick reaches the browser on
        # its next poll, not only after someone presses Enter in a terminal.
        if args.sync and self.draft_ui_state is not None:
            self.sync = _build_sync(args, self.draft_ui_state)
            if self.sync is not None:
                self.sync.start()
                self.draft_ui_state.sync_status = "live"

    def server_close(self) -> None:
        if self.sync is not None:
            self.sync.stop()
        super().server_close()


# --- Actions: pure(ish) functions the request handler calls into ----------
# Kept separate from the handler class so they're testable without spinning
# up a live HTTP server, and so the handler stays a thin protocol adapter.


def _require_draft(server: GuiServer) -> UiState:
    if server.draft_ui_state is None:
        raise GuiError(400, "no draft board configured — set draft.board_csv in config.yml, or pass --board")
    return server.draft_ui_state


def _drain_sync(server: GuiServer) -> None:
    """Apply any Sleeper picks that arrived since the last request. Called
    at the top of every GET/POST, mirroring scripts/draft.py's run_loop
    draining once per iteration -- except this server has no blocking
    input() to wait on, so a synced pick is visible on the very next
    request rather than only after the next keystroke. Safe with no lock:
    this server is single-threaded by design (see the module docstring),
    and DraftSync's background thread only ever pushes onto a thread-safe
    queue -- DraftState itself is still touched only from this thread.
    """
    if server.sync is None or server.draft_ui_state is None:
        return
    for pick in apply_synced_picks(server.draft_ui_state.draft, server.sync.drain()):
        _append_sync_log(server.draft_log_path, pick)
    server.draft_ui_state.sync_status = server.sync.status()
    server.draft_ui_state.sync_unmapped = server.sync.unmapped_count()


def draft_command_action(server: GuiServer, body: dict) -> dict:
    state = _require_draft(server)
    line = str(body.get("line", ""))
    new_state, handled = handle_local_command(state, line, server.args, server.draft_log_path)
    if not handled:
        new_state = handle(new_state, line)
        _append_log(server.draft_log_path, line)
    server.draft_ui_state = new_state
    return webapi.draft_state_json(new_state)


def draft_pick_action(server: GuiServer, body: dict) -> dict:
    state = _require_draft(server)
    key = body.get("key")
    mine = body.get("mine")
    try:
        state.draft.record(key, mine=mine)
    except ValueError as exc:
        raise GuiError(400, str(exc)) from exc
    _append_pick_log(server.draft_log_path, key, mine)
    return webapi.draft_state_json(state)


def draft_reset_action(server: GuiServer) -> dict:
    _require_draft(server)
    archived = draft_store.archive_log(server.draft_log_path)
    server.draft_ui_state = build_draft_state(server.args)
    return {"archived": str(archived) if archived else None, **webapi.draft_state_json(server.draft_ui_state)}


def draft_save_action(server: GuiServer, body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    if not name:
        raise GuiError(400, "name is required")
    try:
        dest = draft_store.save_snapshot(server.draft_log_path, name)
    except draft_store.DraftStoreError as exc:
        raise GuiError(400, str(exc)) from exc
    return {"saved": str(dest)}


def draft_load_action(server: GuiServer, body: dict) -> dict:
    _require_draft(server)
    name = str(body.get("name", "")).strip()
    try:
        snap_path = draft_store.load_snapshot(name)
    except draft_store.DraftStoreError as exc:
        raise GuiError(400, str(exc)) from exc
    archived = draft_store.archive_log(server.draft_log_path)
    fresh = build_draft_state(server.args)
    fresh = replay_log(fresh, snap_path)
    shutil.copyfile(snap_path, server.draft_log_path)
    server.draft_ui_state = fresh
    return {"archived": str(archived) if archived else None, **webapi.draft_state_json(fresh)}


def draft_saves_action(server: GuiServer) -> dict:
    return {"saves": draft_store.list_snapshots()}


def draft_search_action(server: GuiServer, query: dict[str, list[str]]) -> dict:
    state = _require_draft(server)
    q = (query.get("q") or [""])[0]
    limit_raw = (query.get("limit") or ["8"])[0]
    try:
        limit = max(1, min(25, int(limit_raw)))
    except ValueError:
        limit = 8
    return webapi.draft_search_json(state, q, limit)


def weekly_run_action(server: GuiServer, body: dict) -> dict:
    try:
        week_num = int(body["week"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiError(400, "week (int) is required") from exc

    try:
        loaded = report.load_everything(
            config_path=server.args.config,
            roster_path=server.args.roster,
            week_num=week_num,
            proj_csv_paths=None,
            weekly_path=body.get("weekly_path"),
            weeks_in_season=server.args.weeks_in_season,
            league_rosters_path=server.args.league_rosters,
        )
    except report.ReportError as exc:
        raise GuiError(400, str(exc)) from exc

    return webapi.weekly_report_json(
        loaded,
        week_num=week_num,
        lineup_state_path=server.args.state,
        stream_positions=body.get("stream") or None,
        show_waivers=bool(body.get("waivers", False)),
        remaining_faab=body.get("faab"),
        my_priority=body.get("priority"),
        weeks_in_season=server.args.weeks_in_season,
        commit_lineup=bool(body.get("commit", False)),
    )


def reports_list_action(server: GuiServer) -> dict:
    summaries = reports_index.list_reports(server.args.reports_dir)
    return {
        "reports": [
            {"filename": r.filename, "modified": r.modified, "size": r.size}
            for r in summaries
        ]
    }


def reports_content_action(server: GuiServer, query: dict[str, list[str]]) -> dict:
    values = query.get("file")
    if not values:
        raise GuiError(400, "file query parameter is required")
    try:
        content = reports_index.read_report(server.args.reports_dir, values[0])
    except reports_index.ReportNotFoundError as exc:
        raise GuiError(404, str(exc)) from exc
    return {"filename": values[0], "content": content}


def roster_get_action(server: GuiServer) -> dict:
    return {"entries": roster_editor.roster_entries_json(server.args.roster)}


def roster_post_action(server: GuiServer, body: dict) -> dict:
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise GuiError(400, "entries must be a list")
    roster_editor.write_roster_entries(server.args.roster, entries)
    return {"saved": True, "entries": roster_editor.roster_entries_json(server.args.roster)}


def _week_num_from_query(query: dict[str, list[str]]) -> int:
    values = query.get("week")
    if not values:
        raise GuiError(400, "week query parameter is required")
    try:
        return int(values[0])
    except ValueError as exc:
        raise GuiError(400, f"invalid week: {values[0]!r}") from exc


def weekly_intel_get_action(query: dict[str, list[str]]) -> dict:
    week_num = _week_num_from_query(query)
    return weekly_editor.weekly_intel_editor_json(report.default_weekly_path(week_num))


def weekly_intel_post_action(query: dict[str, list[str]], body: dict) -> dict:
    week_num = _week_num_from_query(query)
    path = report.default_weekly_path(week_num)
    weekly_editor.write_weekly_intel(path, body)
    return weekly_editor.weekly_intel_editor_json(path)


def settings_get_action(server: GuiServer) -> dict:
    cfg = Config.load(server.args.config)
    return {
        "sleeper": {
            "league_id": cfg.sleeper.league_id,
            "username": cfg.sleeper.username,
            "roster_id": cfg.sleeper.roster_id,
        },
        # "sleeper" once a league_id is configured (roster/status/ownership
        # all live), "manual" (roster.yml, the pre-Sleeper baseline)
        # otherwise. Unlike Yahoo this was never gated on API approval --
        # Sleeper's read API needs no auth at all.
        "data_source": "sleeper" if cfg.sleeper.league_id else "manual",
        "roster_positions": cfg.roster_positions,
        "draft": {
            "num_teams": cfg.draft.num_teams,
            "my_slot": cfg.draft.my_slot,
            "rounds": cfg.draft.rounds,
            "order": cfg.draft.order,
            "position_caps": cfg.draft.position_caps,
            "position_targets": cfg.draft.position_targets,
        },
        "season": {"spice_level": cfg.season.spice_level},
    }


def _overlay_path(server: GuiServer) -> Path:
    return Path(server.args.config).with_name("config.local.yml")


def _drop_empty_strings(value):
    """Recursively drop `""` values from a posted settings dict.

    A blank text field (e.g. a cleared `sleeper.league_id` input) must never
    write an empty string into config.local.yml: the overlay deep-merges ON
    TOP of config.yml, so `''` there would silently blank out a real id
    typed into the main file, with no error pointing at the cause — the
    exact trap this repo shipped once already (see CLAUDE.md/docs/SETUP.md).
    Dropping the key instead means "leave whatever's already there alone",
    which is what a cleared field on a settings form should mean.
    """
    if isinstance(value, dict):
        cleaned = {k: _drop_empty_strings(v) for k, v in value.items() if v != ""}
        return {k: v for k, v in cleaned.items() if v != {}}
    return value


def settings_post_action(server: GuiServer, body: dict) -> dict:
    posted = {k: v for k, v in body.items() if k in _SETTINGS_KEYS}
    posted = _drop_empty_strings(posted)
    structural = "roster_positions" in posted or (
        "draft" in posted and any(k in posted["draft"] for k in ("num_teams", "order"))
    )
    if structural and server.draft_ui_state is not None and server.draft_ui_state.draft.picks:
        raise GuiError(409, "reset the draft before changing teams, order, or roster shape")

    overlay_path = _overlay_path(server)
    existing = {}
    if overlay_path.exists():
        existing = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, posted)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")

    if structural and server.draft_ui_state is not None:
        server.draft_ui_state = build_draft_state(server.args)

    return settings_get_action(server)


# --- HTTP protocol adapter --------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server: GuiServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # the draft/weekly CLIs are quiet by default too; avoid the noise

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GuiError(400, "invalid JSON body") from exc

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            _drain_sync(self.server)
            if path in _PAGE_FOR:
                self._send_file(_PAGE_FOR[path], "text/html; charset=utf-8")
            elif path == "/style.css":
                self._send_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            elif path == "/common.js":
                self._send_file(WEB_DIR / "common.js", "application/javascript; charset=utf-8")
            elif path == "/api/draft/state":
                self._send_json(200, webapi.draft_state_json(_require_draft(self.server)))
            elif path == "/api/draft/saves":
                self._send_json(200, draft_saves_action(self.server))
            elif path == "/api/draft/search":
                self._send_json(200, draft_search_action(self.server, query))
            elif path == "/api/roster":
                self._send_json(200, roster_get_action(self.server))
            elif path == "/api/weekly-intel":
                self._send_json(200, weekly_intel_get_action(query))
            elif path == "/api/settings":
                self._send_json(200, settings_get_action(self.server))
            elif path == "/api/reports":
                self._send_json(200, reports_list_action(self.server))
            elif path == "/api/reports/content":
                self._send_json(200, reports_content_action(self.server, query))
            else:
                self.send_error(404)
        except GuiError as exc:
            self._send_json(exc.status, {"error": exc.message})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            _drain_sync(self.server)
            body = self._read_json_body()
            if path == "/api/draft/command":
                self._send_json(200, draft_command_action(self.server, body))
            elif path == "/api/draft/pick":
                self._send_json(200, draft_pick_action(self.server, body))
            elif path == "/api/draft/reset":
                self._send_json(200, draft_reset_action(self.server))
            elif path == "/api/draft/save":
                self._send_json(200, draft_save_action(self.server, body))
            elif path == "/api/draft/load":
                self._send_json(200, draft_load_action(self.server, body))
            elif path == "/api/weekly/run":
                self._send_json(200, weekly_run_action(self.server, body))
            elif path == "/api/roster":
                self._send_json(200, roster_post_action(self.server, body))
            elif path == "/api/weekly-intel":
                self._send_json(200, weekly_intel_post_action(query, body))
            elif path == "/api/settings":
                self._send_json(200, settings_post_action(self.server, body))
            else:
                self.send_error(404)
        except GuiError as exc:
            self._send_json(exc.status, {"error": exc.message})


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = GuiServer((args.host, args.port), Handler, args)
    print(f"ffbot GUI running at http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    if server.draft_ui_state is None:
        print("  draft room: unavailable (no board configured)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
