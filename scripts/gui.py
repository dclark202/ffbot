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
import dataclasses
import json
import random
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot import draft_store  # noqa: E402
from ffbot import report  # noqa: E402
from ffbot import reports_index  # noqa: E402
from ffbot import webapi  # noqa: E402
from ffbot import week_log  # noqa: E402
from ffbot.config import (  # noqa: E402
    DRAFT_BASELINE,
    DRAFT_UNTESTED_DIALS,
    SEASON_BASELINE,
    SEASON_UNTESTED_DIALS,
    Config,
    DraftConfig,
    _deep_merge,
)
from ffbot.draft import team_slot_at  # noqa: E402
from ffbot.draft_report import DraftReporter  # noqa: E402
from ffbot.draft_sync import apply_synced_picks  # noqa: E402  (no yahoo_fantasy_api/requests import in this module)
from ffbot.draft_ui import _SORT_ORDER, UiState, _replace, handle  # noqa: E402
from scripts.mock_draft import _bot_pick  # noqa: E402
from scripts.draft import (  # noqa: E402
    _append_draft_id,
    _append_log,
    _append_pick_log,
    _append_sync_log,
    _build_sync,
    _fetch_kalshi_draft_signal,
    apply_cli_overrides,
    build_state as build_draft_state,
    handle_local_command,
    replay_log,
    resume_conflict,
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

# --- Tuning dials: what the Settings page can move -------------------------
#
# B10 replaced the 1-4 spice ladder with `DRAFT_BASELINE`/`SEASON_BASELINE`
# plus a per-dial slider for each key in them. These tables carry everything
# the page needs to render and validate one slider, in one place, so
# web/settings.html holds no duplicated copy of thirty-one dials.
#
# `(kind, lo, hi, step, group, label)`. Bounds are the SERVER's guarantee,
# not the page's: an out-of-range value must be refused before it reaches
# config.local.yml, because a bad number there breaks every later
# `Config.load` -- the GUI's own next request included. That was
# `_validate_spice_level`'s reason for existing and it carries over intact.
#
# Ranges are mostly 0.0-1.0 because these dials are FRACTIONS of the
# decision at hand (see DraftConfig's "How contrarian to be" block). Two
# deliberate exceptions:
#   - the three draft variance dials go to 1.5, so the old level-4 values
#     (upside 0.65) sit mid-track rather than pinned at the maximum;
#   - `blocking_hold_bonus` is FLAT SEASON POINTS, not a fraction -- a 0-1
#     slider would put its own default of 1.5 out of reach.
_DRAFT_DIALS: dict[str, tuple] = {
    "upside_weight": ("float", 0.0, 1.5, 0.05, "Player valuation", "Upside (researched breakout)"),
    "risk_weight": ("float", 0.0, 1.5, 0.05, "Player valuation", "Availability risk"),
    "volatility_weight": ("float", 0.0, 1.5, 0.05, "Player valuation", "ADP disagreement"),
    "stack_bonus": ("float", 0.0, 1.0, 0.05, "Player valuation", "QB/receiver stack"),
    "scoring_arbitrage_weight": ("float", 0.0, 0.5, 0.01, "Player valuation", "League-scoring edge"),
    "balance_weight": ("float", 0.0, 1.0, 0.05, "Roster construction", "Roster balance urgency"),
    "bye_collision_weight": ("float", 0.0, 1.0, 0.05, "Roster construction", "Bye-week collision"),
    "team_concentration_weight": ("float", 0.0, 0.5, 0.01, "Roster construction", "Same-team penalty"),
    "same_team_position_weight": ("float", 0.0, 0.5, 0.01, "Roster construction", "Same-team, same-position"),
    "block_weight": ("float", 0.0, 1.0, 0.05, "Roster construction", "Positional blocking"),
    "risk_ramp_start": ("int", 1, 15, 1, "Risk ramp", "No extra risk before round"),
    "risk_ramp_full": ("int", 1, 15, 1, "Risk ramp", "Full risk appetite from round"),
    "kalshi_weight": ("float", 0.0, 0.5, 0.01, "Untested features", "Kalshi prediction markets"),
}

_SEASON_DIALS: dict[str, tuple] = {
    "weather_weight": ("float", 0.0, 1.0, 0.01, "Game conditions", "Weather"),
    "vegas_weight": ("float", 0.0, 1.0, 0.01, "Game conditions", "Vegas implied total"),
    "usage_weight": ("float", 0.0, 1.0, 0.01, "Player trend", "Usage trend"),
    "momentum_weight": ("float", 0.0, 1.0, 0.01, "Player trend", "Recent momentum"),
    "divergence_weight": ("float", 0.0, 1.0, 0.01, "Player trend", "Projection divergence"),
    "volatility_weight": ("float", 0.0, 1.0, 0.05, "Variance lean", "Volatility"),
    "upside_lean_weight": ("float", 0.0, 1.0, 0.05, "Variance lean", "Upside lean"),
    "streaming_weight": ("float", 0.0, 1.0, 0.05, "Waivers & streaming", "Matchup vs. floor"),
    "waiver_value_mode": ("enum", None, None, None, "Waivers & streaming", "Waiver valuation"),
    "priority_value": ("float", 0.0, 1.0, 0.05, "Waivers & streaming", "Cost of spending priority"),
    "blocking_hold_bonus": ("float", 0.0, 5.0, 0.1, "Blocking & denial", "Blocking hold bonus (pts)"),
    "denial_weight": ("float", 0.0, 1.0, 0.05, "Blocking & denial", "Tactical denial"),
    "denial_opponent_boost": ("float", 0.0, 1.0, 0.05, "Blocking & denial", "Rival-threat boost"),
    "denial_seed_window": ("int", 0, 6, 1, "Blocking & denial", "Playoff-seed window"),
    "denial_priority_floor": ("int", 0, 12, 1, "Blocking & denial", "Protect priority above rank"),
    "matchup_variance_weight": ("float", 0.0, 1.0, 0.05, "Untested features", "Matchup-conditioned variance"),
    "kalshi_weight": ("float", 0.0, 0.5, 0.01, "Untested features", "Kalshi player props"),
    "venue_disruption_weight": ("float", 0.0, 0.5, 0.01, "Untested features", "Venue disruption"),
}

_DIAL_ENUMS: dict[str, tuple[str, ...]] = {"waiver_value_mode": ("points", "marginal")}

_DIAL_TABLES = {
    "draft": (_DRAFT_DIALS, DRAFT_BASELINE, DRAFT_UNTESTED_DIALS),
    "season": (_SEASON_DIALS, SEASON_BASELINE, SEASON_UNTESTED_DIALS),
}


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
    p.add_argument("--week-log-dir", default=None, help=f"where the per-run weekly recommendation log is written (default: {week_log.WEEK_LOG_DIR})")
    p.add_argument("--no-week-log", action="store_true", help="don't write the per-run weekly recommendation log")
    p.add_argument(
        "--sync", action=argparse.BooleanOptionalAction, default=True,
        help="poll Sleeper's live draft picks in the background (no auth needed) -- same as scripts/draft.py --sync; on by default, pass --no-sync for a fully offline session",
    )
    p.add_argument(
        "--mock", action="store_true",
        help="local mock draft: bots fill every other seat instantly, no Sleeper and no network. "
             "Implies --no-sync. Their picks land in the Draft Log and their rosters in the "
             "Opponents panel, exactly as a synced draft's would",
    )
    p.add_argument("--bot-spice", type=int, default=1, help="--mock: spice level the bots draft at (default: %(default)s)")
    p.add_argument(
        "--bot-window", type=int, default=3,
        help="--mock: bots pick among their top N recommendations, so runs differ (1 = deterministic; default: %(default)s)",
    )
    p.add_argument("--seed", type=int, default=None, help="--mock: reproducible bot behaviour")
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
        self.resume_conflict_reason = ""
        try:
            self.draft_ui_state = build_draft_state(args)
            if args.resume:
                # Replaying a log that belongs to a DIFFERENT draft fills the
                # board with stale picks and then silently swallows the live
                # feed -- see resume_conflict's docstring. Refuse rather than
                # start a session that can never sync.
                self.resume_conflict_reason = resume_conflict(self.draft_log_path, args.draft_id)
                if self.resume_conflict_reason:
                    print(f"--resume: {self.resume_conflict_reason}", file=sys.stderr)
                else:
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
        # --mock replaces the Sleeper feed entirely; polling a live draft
        # while bots drive the board locally would fight over the same
        # pick numbers.
        self.mock_driver = None
        if getattr(args, "mock", False) and self.draft_ui_state is not None:
            bot_draft = DraftConfig.from_spice_level(args.bot_spice)
            bot_cfg = Config.load(args.config)
            bot_cfg.draft = dataclasses.replace(
                bot_draft,
                num_teams=self.draft_ui_state.draft.num_teams,
                rounds=self.draft_ui_state.draft.rounds,
                order=self.draft_ui_state.draft.order,
                position_caps=dict(self.draft_ui_state.cfg.draft.position_caps),
                position_targets=dict(self.draft_ui_state.cfg.draft.position_targets),
                board_csv=list(self.draft_ui_state.cfg.draft.board_csv),
                my_picks=[],
            )
            self.mock_driver = {
                "cfg": bot_cfg,
                "rng": random.Random(args.seed),
                "window": max(1, args.bot_window),
            }
            self.draft_ui_state.sync_status = "off"
            self.draft_ui_state.sync_reason = (
                f"local mock draft — bots at spice {args.bot_spice}, no Sleeper"
            )
            args.sync = False

        if args.sync and self.draft_ui_state is not None:
            self.sync = _build_sync(args, self.draft_ui_state)
            if self.sync is not None:
                _append_draft_id(self.draft_log_path, self.sync.draft_id)
                self.sync.start()
                self.draft_ui_state.sync_status = "live"
                if self.resume_conflict_reason:
                    # Surface it in the UI too, not just on a stderr line the
                    # user may never see -- a refused --resume is exactly the
                    # sort of "why is my roster empty?" surprise sync_note exists for.
                    prefix = self.draft_ui_state.sync_note
                    note = f"--resume skipped: {self.resume_conflict_reason}"
                    self.draft_ui_state.sync_note = f"{prefix}; {note}" if prefix else note

        # Every draft writes a tuning report, no flag required. Ownership is
        # recorded as ground truth only when Sleeper's own roster_id
        # resolved it -- otherwise the report says it was inferred from
        # snake order, so a report built on a guess never reads like one
        # built on fact. See ffbot/draft_report.py.
        self.reporter = None
        if self.draft_ui_state is not None:
            # getattr: `self.sync` is duck-typed (tests substitute their own
            # minimal sync objects), and a sync that can't report a roster id
            # simply means ownership stays unresolved -- which is exactly
            # what the "snake_order_guess" label below records.
            my_roster_id = getattr(self.sync, "my_roster_id", None) if self.sync else None
            self.reporter = DraftReporter(
                cfg=self.draft_ui_state.cfg,
                draft_id=self.sync.draft_id if self.sync else None,
                ownership_source=(
                    "sleeper_roster_id" if my_roster_id is not None else "snake_order_guess"
                ),
                my_roster_id=my_roster_id,
            )
            self.reporter.kalshi_scores = self.draft_ui_state.kalshi_scores

    def server_close(self) -> None:
        if self.sync is not None:
            self.sync.stop()
        super().server_close()


# --- Actions: pure(ish) functions the request handler calls into ----------
# Kept separate from the handler class so they're testable without spinning
# up a live HTTP server, and so the handler stays a thin protocol adapter.


def _require_draft(server: GuiServer) -> UiState:
    if server.draft_ui_state is None:
        raise GuiError(
            400,
            "no draft board loaded — download the 5 FantasyPros CSVs into "
            "draft/ (README, step 2), then restart the server. Or set "
            "draft.board_csv in config.yml, or pass --board.",
        )
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
    reporter = getattr(server, "reporter", None)
    for pick in apply_synced_picks(
        server.draft_ui_state.draft,
        server.sync.drain(),
        on_my_pick=(lambda draft, key: reporter.capture(draft, key)) if reporter else None,
    ):
        _append_sync_log(server.draft_log_path, pick)
    server.draft_ui_state.sync_status = server.sync.status()
    server.draft_ui_state.sync_unmapped = server.sync.unmapped_count()


def _advance_bots(server: GuiServer) -> None:
    """Let the bots take every seat that isn't mine, up to my next pick.

    The mock-draft counterpart to `_drain_sync`, called from the same two
    places for the same reason: this server has no background loop, so the
    board advances on request. A bot pick is a `recommend()` call (tens of
    milliseconds), so a whole round resolves well inside one page load and
    the user simply sees the log and the opponent rosters already filled in
    when it comes back to them.

    Runs on the request thread, mutating `DraftState` there -- the same
    main-thread-only discipline `_drain_sync` observes, and the reason this
    server stays single-threaded.

    Bot picks are appended to the draft log exactly like a synced pick, so
    `--resume`, `scripts/draft_report.py`, and `scripts/draft_counterfactual.py`
    all read a mock draft with no idea it was one.
    """
    driver = getattr(server, "mock_driver", None)
    if driver is None or server.draft_ui_state is None:
        return
    draft = server.draft_ui_state.draft
    total = draft.num_teams * draft.rounds
    mine = set(draft.my_picks())
    while draft.current_pick() <= total and draft.current_pick() not in mine:
        pick_no = draft.current_pick()
        slot = team_slot_at(pick_no, draft.num_teams, draft.order)
        key = _bot_pick(draft, driver["cfg"], slot, driver["rng"], driver["window"])
        if key is None:
            break
        draft.record(key, mine=False)
        _append_pick_log(server.draft_log_path, key, False)


def draft_resync_action(server: GuiServer) -> dict:
    """The hardest refresh available without restarting the process — what
    the GUI's Refresh button fires.

    A plain `GET /api/draft/state` only drains what the background thread
    has ALREADY queued, so it is worth nothing precisely when it matters
    most: when that thread is behind, wedged, or was never built because
    Sleeper happened to be unreachable at startup. On draft day the button
    has to be able to fix all three without the user restarting a server
    mid-round. In order, this:

      1. rebuilds sync outright if it never came up (a startup-time network
         blip otherwise costs you sync for the whole draft),
      2. restarts the poll thread if it isn't alive,
      3. forces a read of Sleeper that ignores every skip heuristic, and
         waits for it to actually land,
      4. applies whatever came back.

    Reports what it did, so a no-op refresh is visibly a no-op rather than
    indistinguishable from a broken button.
    """
    state = _require_draft(server)
    rebuilt = False
    restarted = False
    completed = True

    if server.sync is None and server.args.sync:
        server.sync = _build_sync(server.args, state)
        if server.sync is not None:
            _append_draft_id(server.draft_log_path, server.sync.draft_id)
            server.sync.start()
            rebuilt = True

    before = state.draft.current_pick()
    if server.sync is not None:
        restarted = server.sync.ensure_running()
        completed = server.sync.force_poll()
        _drain_sync(server)
        state.sync_status = server.sync.status()

    applied = state.draft.current_pick() - before
    if server.sync is None:
        message = f"sync is off — {state.sync_reason}" if state.sync_reason else "sync is off"
    elif not completed:
        message = "Sleeper is slow to answer — still working in the background, refresh again shortly"
    elif applied:
        message = f"pulled {applied} new pick{'s' if applied != 1 else ''}"
    else:
        message = "up to date"
    if rebuilt:
        message = f"sync reconnected; {message}"
    elif restarted:
        message = f"sync thread restarted; {message}"

    state.message = message
    return webapi.draft_state_json(state)


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
    # Capture BEFORE recording -- the report's whole value is the pre-pick
    # view of the table, which cannot be reconstructed afterwards.
    reporter = getattr(server, "reporter", None)
    is_mine = mine if mine is not None else (state.draft.current_pick() in state.draft.my_picks())
    if reporter is not None and is_mine and key:
        reporter.capture(state.draft, key)
    try:
        state.draft.record(key, mine=mine)
    except ValueError as exc:
        raise GuiError(400, str(exc)) from exc
    _append_pick_log(server.draft_log_path, key, mine)
    return webapi.draft_state_json(state)


def draft_view_action(server: GuiServer, body: dict) -> dict:
    """Set the recommendations panel's sort/position-filter directly,
    bypassing `handle()`'s command grammar entirely.

    View state (how the SAME data is sorted/filtered) is not draft state
    (what actually happened), so unlike `draft_command_action` this never
    calls `_append_log` -- nothing here belongs in `draft_log.jsonl`, and a
    resumed session has no reason to replay a sort preference. This also
    replaces the sort dropdown's previous hack of firing up to six `"s"`
    cycle-commands to reach a target sort (draft_ui's own grammar only
    supports "cycle to next," never "jump to X") -- six single-threaded
    round-trips, and six junk log lines, for what is really one state
    change.
    """
    state = _require_draft(server)
    changes: dict = {}
    if "sort" in body:
        sort = str(body["sort"])
        if sort not in _SORT_ORDER:
            raise GuiError(400, f"invalid sort: {sort!r} (must be one of {', '.join(_SORT_ORDER)})")
        changes["sort"] = sort
    if "filter_pos" in body:
        pos = str(body["filter_pos"] or "").strip().upper()
        changes["filter_pos"] = pos or None
    new_state = _replace(state, **changes) if changes else state
    server.draft_ui_state = new_state
    return webapi.draft_state_json(new_state)


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


def _resolve_week(server: GuiServer, cfg: Config, body: dict, refresh: bool) -> tuple[int, "int | None", str]:
    """`(week_num, season, week_source)`. The GUI's weekly page sends no
    week input at all -- it always wants "whatever week it is right now."
    Explicit `week` in the body still wins when given (used by the page's
    prev/next arrows to look at a different week on demand).

    `"sleeper"` needs `roster_source: sleeper` (the same config gate every
    other live-Sleeper feature here uses) and a genuine in-season regular
    week from `SleeperClient.nfl_state()` -- an off-season/preseason read,
    or the fetch itself failing, raises a clear 502 rather than silently
    resolving to something meaningless. `"league_file"` is the fallback for
    the manual roster_source route (and the offline demo, whose replay
    clock writes the current week into league.yml): league.yml's own
    `week:` field, when set. Neither source available -> 400, same as
    before this resolution existed.
    """
    if body.get("week") is not None:
        try:
            return int(body["week"]), None, "explicit"
        except (TypeError, ValueError) as exc:
            raise GuiError(400, "week (int) is required") from exc

    if cfg.roster_source.source == "sleeper":
        from ffbot.sleeper.cache import DEFAULT_CACHE_DIR as SLEEPER_DEFAULT_CACHE_DIR
        from ffbot.sleeper.cache import SleeperFetchError
        from ffbot.sleeper.client import SleeperClient

        try:
            client = SleeperClient(cache_dir=cfg.sleeper.cache_dir or SLEEPER_DEFAULT_CACHE_DIR, force_refresh=refresh)
            state = client.nfl_state()
        except SleeperFetchError as exc:
            raise GuiError(502, f"couldn't resolve the current week from Sleeper ({exc}) — pass week explicitly") from exc
        season_type = state.get("season_type")
        raw_week = state.get("week")
        if season_type != "regular" or not raw_week or int(raw_week) < 1:
            raise GuiError(
                502,
                f"Sleeper's live state isn't a resolvable regular-season week "
                f"(season_type={season_type!r}, week={raw_week!r}) — pass week explicitly",
            )
        return int(raw_week), int(state["season"]), "sleeper"

    if cfg.league is not None and cfg.league.week:
        return int(cfg.league.week), None, "league_file"

    raise GuiError(400, "week (int) is required — no live week source is configured")


def weekly_run_action(server: GuiServer, body: dict) -> dict:
    cfg = Config.load(server.args.config)
    refresh = bool(body.get("refresh", False))
    week_num, season, week_source = _resolve_week(server, cfg, body, refresh)

    try:
        loaded = report.load_everything(
            config_path=server.args.config,
            roster_path=server.args.roster,
            week_num=week_num,
            proj_csv_paths=None,
            weekly_path=body.get("weekly_path"),
            weeks_in_season=server.args.weeks_in_season,
            league_rosters_path=server.args.league_rosters,
            season=season,
            refresh=refresh,
        )
    except report.ReportError as exc:
        raise GuiError(400, str(exc)) from exc

    def _log_plan(plan, alerts, priority) -> None:
        """Persist the run for later review. Failures are swallowed: the
        page must still render if the log can't be written."""
        if server.args.no_week_log:
            return
        try:
            from ffbot import projections

            # `_resolve_week` leaves `season` None whenever the week came
            # from league.yml rather than Sleeper's own clock -- fine for the
            # loader, which only needs it for live fetches, but it would name
            # the log file "None-wNN-gui.json".
            log_season = season if season is not None else projections.current_nfl_season()
            log = week_log.build_week_log(
                loaded, plan, season=log_season, source="gui", alerts=alerts,
                week_source=week_source, refreshed=refresh, waiver_priority=priority,
            )
            week_log.write_week_log(
                log,
                week_log.week_log_path(
                    log_season, week_num, "gui",
                    server.args.week_log_dir or week_log.WEEK_LOG_DIR,
                ),
            )
        except Exception:
            pass

    return webapi.weekly_report_json(
        loaded,
        week_num=week_num,
        lineup_state_path=server.args.state,
        on_plan=_log_plan,
        # Waivers default ON -- the page is read-only and auto-runs with no
        # form, so there's no checkbox left to gate this; the recommendation
        # panel just always includes them when a board is configured.
        show_waivers=bool(body.get("waivers", True)),
        my_priority=body.get("priority"),
        weeks_in_season=server.args.weeks_in_season,
        # The read-only page never commits -- live Sleeper starters (or,
        # under the file route, weekly/lineup_state.yml via the CLI) are
        # the only lineup baseline now. A stray `commit` in the body (an
        # old client, a stale bookmark) is silently ignored rather than
        # honored, since commit as a side effect of this endpoint predates
        # this page having no editors at all.
        commit_lineup=False,
        week_source=week_source,
        refreshed=refresh,
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
            "use_untested_features": cfg.draft.use_untested_features,
            "dials": _dial_values(cfg.draft, "draft"),
        },
        "season": {
            "use_untested_features": cfg.season.use_untested_features,
            "dials": _dial_values(cfg.season, "season"),
        },
        "dial_meta": _dial_meta(),
    }


def _dial_values(block, name: str) -> dict:
    """This block's EFFECTIVE dial values -- what the engine will actually
    use, post-gate. So an untested dial reads back 0.0 while its checkbox is
    unticked, which is the truth about what the page is configured to do,
    rather than the value it would spring back to if ticked."""
    return {key: getattr(block, key) for key in _DIAL_TABLES[name][0]}


def _dial_meta() -> dict:
    """Type/bounds/step/default/group/label for every dial, both blocks.

    Shipped to the page so `web/settings.html` renders and resets sliders
    from the server's own tables rather than keeping a second copy of
    thirty-one dials in JavaScript -- the kind of hand-maintained duplicate
    this repo keeps discovering has gone stale (docs/dev/BACKTEST.md's B9).

    `default` is what the page's reset button restores: the baseline for an
    ordinary dial, and the level-4 value for a gated one (a gated dial's
    baseline is 0.0, which would make "reset" mean "switch me off" -- not
    what the button means when the feature is deliberately on).
    """
    meta: dict[str, dict] = {}
    for name, (table, baseline, untested) in _DIAL_TABLES.items():
        block: dict[str, dict] = {}
        for key, (kind, lo, hi, step, group, label) in table.items():
            entry = {
                "type": kind,
                "group": group,
                "label": label,
                "untested": key in untested,
                "default": untested.get(key, baseline[key]),
            }
            if kind == "enum":
                entry["options"] = list(_DIAL_ENUMS[key])
            else:
                entry.update(min=lo, max=hi, step=step)
            block[key] = entry
        meta[name] = block
    return meta


def _overlay_path(server: GuiServer) -> Path:
    return Path(server.args.config).with_name("config.local.yml")


def _drop_empty_strings(value):
    """Recursively drop `""` values from a posted settings dict.

    A blank text field (e.g. a cleared `sleeper.league_id` input) must never
    write an empty string into config.local.yml: the overlay deep-merges ON
    TOP of config.yml, so `''` there would silently blank out a real id
    typed into the main file, with no error pointing at the cause — the
    exact trap this repo shipped once already (see CLAUDE.md/docs/REFERENCE.md).
    Dropping the key instead means "leave whatever's already there alone",
    which is what a cleared field on a settings form should mean.
    """
    if isinstance(value, dict):
        cleaned = {k: _drop_empty_strings(v) for k, v in value.items() if v != ""}
        return {k: v for k, v in cleaned.items() if v != {}}
    return value


def _validate_dials(posted_block: dict, name: str) -> None:
    """Reject a malformed tuning dial BEFORE it reaches config.local.yml.

    This is the same guarantee `_validate_spice_level` used to make for the
    old ladder, generalized to thirty-one sliders: a bad value written to the
    overlay would load successfully here and then break every subsequent
    `Config.load` -- the GUI's own next request included -- with nothing
    pointing at the cause. Refusing on the way in keeps a stale or hand-rolled
    client from bricking the app.

    Keys that aren't dials are left alone: `draft:` legitimately also carries
    `num_teams`, `position_caps` and friends, and `_construct`'s ConfigError
    is still the backstop for a genuinely unknown key.
    """
    table = _DIAL_TABLES[name][0]
    flag = posted_block.get("use_untested_features")
    if flag is not None and not isinstance(flag, bool):
        raise GuiError(400, f"{name}.use_untested_features must be true or false, got {flag!r}")

    for key, value in posted_block.items():
        if key not in table:
            continue
        kind, lo, hi, _step, _group, _label = table[key]
        label = f"{name}.{key}"
        if kind == "enum":
            if value not in _DIAL_ENUMS[key]:
                allowed = " or ".join(repr(o) for o in _DIAL_ENUMS[key])
                raise GuiError(400, f"{label} must be {allowed}, got {value!r}")
            continue
        # bool first: `isinstance(True, int)` is True in Python, so a posted
        # `true` would otherwise sail through every numeric check below as 1.
        if isinstance(value, bool):
            raise GuiError(400, f"{label} must be a number, got {value!r}")
        if kind == "int":
            if not isinstance(value, int):
                raise GuiError(400, f"{label} must be a whole number, got {value!r}")
        elif not isinstance(value, (int, float)):
            # JSON sends a bare `0` for 0.0, so int is a valid float here.
            raise GuiError(400, f"{label} must be a number, got {value!r}")
        if not lo <= value <= hi:
            raise GuiError(400, f"{label} must be between {lo} and {hi}, got {value}")


def _strip_overlay_keys(merged: dict, posted: dict) -> None:
    """Remove keys that should no longer be in config.local.yml, in place.

    `_deep_merge` can only add or replace, never delete, so anything the
    overlay must STOP carrying has to be popped here on the way to disk:

    - `spice_level`, unconditionally. The dial is gone from the user surface,
      and a leftover key would silently pin the baseline away from the
      shipped defaults for every dial the user hasn't moved. Popping it
      unconditionally means a pre-B10 config.local.yml self-heals on the
      first save, with no migration step for the user to remember.
    - a block's untested dials, when that block is posting the checkbox off.
      The loader already forces them to 0.0 either way, so this is belt and
      braces -- but it keeps the file honest rather than leaving an inert
      `kalshi_weight: 0.15` sitting under an unticked box.
    """
    for name, (_table, _baseline, untested) in _DIAL_TABLES.items():
        block = merged.get(name)
        if not isinstance(block, dict):
            continue
        block.pop("spice_level", None)
        if posted.get(name, {}).get("use_untested_features") is False:
            for key in untested:
                block.pop(key, None)


def _structural_change(server: GuiServer, posted: dict) -> bool:
    """Is this save actually changing the draft's shape?

    Team count, draft order, and roster shape can't change under a draft
    that already has picks -- every pick number and every slot assignment is
    computed against them. But the Settings page posts the WHOLE form on
    every save, so those keys are present whether or not they were touched.
    Treating "key present" as "shape changed" refused every save once a
    single pick was recorded, which made the tuning sliders unusable in the
    one place they matter: mid-draft, with the board in front of you.

    So compare against what's loaded and only call it structural if a value
    genuinely differs.
    """
    cfg = Config.load(server.args.config)
    if "roster_positions" in posted and posted["roster_positions"] != cfg.roster_positions:
        return True
    draft = posted.get("draft") or {}
    return any(
        key in draft and draft[key] != getattr(cfg.draft, key)
        for key in ("num_teams", "order")
    )


def _reload_draft_cfg(server: GuiServer) -> None:
    """Swap an open draft room onto a freshly loaded Config, keeping its picks.

    Tuning is useless without a feedback loop, so a slider saved on the
    Settings page has to reach the recommendation table without a restart.
    That is safe here for one specific reason: not one of the tuning dials
    feeds `board.load_board_from_config`, so the frozen board stays frozen
    and only `recommend()` -- which reads `state.cfg` fresh on every request
    -- sees the change. `tests/test_gui_server.py` pins both halves of that.

    `draft_ui._replace` carries `state.draft` through by reference, so
    recorded picks survive by construction rather than by care. Runs on the
    request thread like every other action; `DraftState` is never mutated, so
    the single-threaded, main-thread-only contract in CLAUDE.md holds.
    """
    state = server.draft_ui_state
    if state is None:
        return
    cfg = Config.load(server.args.config)
    apply_cli_overrides(cfg, server.args)
    # The LIVE draft's shape always wins over whatever the file now says --
    # num_teams/order are refused mid-draft anyway (see settings_post_action),
    # and my_slot/rounds still need a restart, so re-pinning them here keeps
    # a reload from quietly disagreeing with the board on screen.
    cfg.draft = dataclasses.replace(
        cfg.draft,
        num_teams=state.draft.num_teams,
        rounds=state.draft.rounds,
        order=state.draft.order,
        my_slot=state.draft.my_slot,
    )
    # Turning the draft's untested box on mid-session would otherwise leave
    # kalshi_weight a silent no-op: the signal is fetched once at startup and
    # skipped entirely at weight 0. Fetch it now instead. Best-effort, same as
    # at startup. Never cleared on the way back down -- the weight already
    # makes stale scores an exact no-op, so off->on costs no second fetch.
    scores = state.kalshi_scores
    if cfg.draft.kalshi_weight != 0.0 and not scores:
        scores = _fetch_kalshi_draft_signal(cfg, state.draft.board)
    server.draft_ui_state = _replace(state, cfg=cfg, kalshi_scores=scores)
    if server.reporter is not None:
        # The reporter captured its own cfg at startup; leaving it stale would
        # make the tuning record describe a configuration the picks weren't
        # actually made under.
        server.reporter.cfg = cfg
        server.reporter.kalshi_scores = scores


def settings_post_action(server: GuiServer, body: dict) -> dict:
    posted = {k: v for k, v in body.items() if k in _SETTINGS_KEYS}
    posted = _drop_empty_strings(posted)
    for name in _DIAL_TABLES:
        if isinstance(posted.get(name), dict):
            _validate_dials(posted[name], name)
    structural = _structural_change(server, posted)
    if structural and server.draft_ui_state is not None and server.draft_ui_state.draft.picks:
        raise GuiError(409, "reset the draft before changing teams, order, or roster shape")

    overlay_path = _overlay_path(server)
    existing = {}
    if overlay_path.exists():
        existing = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, posted)
    _strip_overlay_keys(merged, posted)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")

    if structural and server.draft_ui_state is not None:
        server.draft_ui_state = build_draft_state(server.args)
    elif server.draft_ui_state is not None:
        # Non-structural: keep the board and every recorded pick, and just
        # re-read the config so a moved slider lands on the next poll.
        _reload_draft_cfg(server)

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
            _advance_bots(self.server)
            if path in _PAGE_FOR:
                self._send_file(_PAGE_FOR[path], "text/html; charset=utf-8")
            elif path == "/style.css":
                self._send_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            elif path == "/common.js":
                self._send_file(WEB_DIR / "common.js", "application/javascript; charset=utf-8")
            elif path == "/draft_render.js":
                self._send_file(WEB_DIR / "draft_render.js", "application/javascript; charset=utf-8")
            elif path == "/api/draft/state":
                self._send_json(200, webapi.draft_state_json(_require_draft(self.server)))
            elif path == "/api/draft/saves":
                self._send_json(200, draft_saves_action(self.server))
            elif path == "/api/draft/search":
                self._send_json(200, draft_search_action(self.server, query))
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
        path = urllib.parse.urlsplit(self.path).path
        try:
            _drain_sync(self.server)
            _advance_bots(self.server)
            body = self._read_json_body()
            if path == "/api/draft/resync":
                self._send_json(200, draft_resync_action(self.server))
            elif path == "/api/draft/command":
                self._send_json(200, draft_command_action(self.server, body))
            elif path == "/api/draft/pick":
                self._send_json(200, draft_pick_action(self.server, body))
            elif path == "/api/draft/view":
                self._send_json(200, draft_view_action(self.server, body))
            elif path == "/api/draft/reset":
                self._send_json(200, draft_reset_action(self.server))
            elif path == "/api/draft/save":
                self._send_json(200, draft_save_action(self.server, body))
            elif path == "/api/draft/load":
                self._send_json(200, draft_load_action(self.server, body))
            elif path == "/api/weekly/run":
                self._send_json(200, weekly_run_action(self.server, body))
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
        print(
            "  draft room: unavailable — no draft board loaded. Download the 5 "
            "FantasyPros CSVs into draft/ (README, step 2), then restart the "
            "server. Or set draft.board_csv / pass --board.",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
