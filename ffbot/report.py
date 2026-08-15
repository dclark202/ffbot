"""The seam between "gather everything the weekly brief needs" and how it's
presented — `scripts/week_report.py`'s text renderers and the GUI's JSON
serializers (`ffbot/webapi.py`) both build on `load_everything` here.

Split out of `scripts/week_report.py` (which originally raised bare
`SystemExit` and returned an unlabeled 7-tuple) so the GUI can call the exact
same loading logic and get a catchable exception and named fields instead.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import projections
from . import roster_source as rs
from . import week
from .board import Board, _board_key, apply_league_scoring, load_board_from_config, rescale_board_points
from .config import Config
from .league_rosters import LeagueRosters, fetch_league_rosters, load_league_rosters
from .models import Player
from .names import normalize_name
from .projections.cache import ProjectionFetchError


class ReportError(ValueError):
    """Everything needed to build the weekly report could not be loaded."""


@dataclass
class LoadedReport:
    cfg: Config
    weekly: week.WeeklyIntel
    board: Board | None
    players: list[Player]
    unmatched: list  # rs.UnmatchedRosterName
    stadiums: dict[str, week.StadiumInfo]
    league_rosters: LeagueRosters

    # Live-projection wiring (see ffbot/projections/) -- "board" (the
    # default) and "csv" (the pre-existing --proj hatch) leave these at
    # their inert defaults, bit-identical to before this feature existed.
    projection_source: str = "board"
    projection_alerts: list[str] = field(default_factory=list)
    # `{board key: real this-week points}` for the WHOLE candidate pool, not
    # just the roster -- one live-provider fetch already covers every
    # player, so this is free to build alongside the roster's own numbers.
    # Feed straight into `week.waiver_candidates(weekly_points=...)`.
    weekly_points: dict[str, float] = field(default_factory=dict)

    # Live-roster wiring (see ffbot/sleeper_roster.py) -- "file" (the
    # default) leaves this at its inert default, bit-identical to before
    # this feature existed. A failed "sleeper" fetch falls back to "file"
    # for that run, same never-crash contract as projection_alerts above.
    roster_source: str = "file"
    roster_source_alerts: list[str] = field(default_factory=list)

    # Your live rolling waiver priority (see ffbot/sleeper_roster.py's
    # `waiver_position`), fetched alongside the roster under
    # `roster_source: sleeper` -- `None` (the default, and the fallback on
    # a fetch failure, same roster_source_alerts entry covers both) means
    # "no live value this run." Callers should still let an explicit
    # `--priority`/GUI value win over this -- see scripts/week_report.py
    # and ffbot/webapi.py's resolution order.
    waiver_priority: int | None = None

    # A REST-OF-SEASON-rescaled copy of `board` (via
    # `ffbot.board.rescale_board_points` + `ffbot.projections.ros_rows`),
    # built only when `projection_source: sleeper` and a board is
    # configured. `None` (the default, and the fallback on a fetch failure)
    # means "no live ROS numbers this run" -- callers computing `ros_gain`/
    # `hold_margin`/`drop_cost` (`week.waiver_candidates`'s `pool` argument)
    # should pass `ros_board or board`, never `ros_board` unconditionally.
    # Deliberately a SEPARATE field from `board`, not a replacement: `board`
    # still has to stay a true full-season total for
    # `roster_source.season_board_rows`'s fixed-divisor contract --
    # substituting a partial-season ROS total there would silently
    # understate every fallback-priced candidate's weekly rate.
    ros_board: Board | None = None

    # Surfaced whenever `board` above is `None` because the configured
    # board CSVs don't exist yet (the common fresh-clone/no-CSVs-downloaded
    # state — see `ffbot.board.load_board`'s missing-file handling). This is
    # a LOCAL-file gap, not a live-fetch failure, so it gets its own list
    # rather than reusing projection_alerts/roster_source_alerts, which are
    # both about live Sleeper seams degrading. Empty means either a board
    # loaded fine or `draft.board_csv` was never configured at all.
    board_alerts: list[str] = field(default_factory=list)

    # Auto-fetched game-conditions wiring (see ffbot/live/conditions.py) --
    # "off"/"off" (the default) leaves this at its inert empty default,
    # bit-identical to before this feature existed. A source that's off or
    # that fails degrades to "no auto-fetched conditions this run," never a
    # crash; the reason lands here, same never-silent contract as
    # projection_alerts/roster_source_alerts above.
    game_conditions_alerts: list[str] = field(default_factory=list)

    # Live-standings wiring (see ffbot/sleeper_standings.py) -- "file" (the
    # default) leaves cfg.league's teams:/my_team/my_opponent/week exactly
    # as league.yml wrote them. A failed "sleeper" fetch leaves league.yml's
    # own standings untouched for this run, same never-crash contract as
    # every other live seam.
    standings_alerts: list[str] = field(default_factory=list)

    # "sleeper" when `players[...].selected_position` reflects the REAL
    # current lineup in the Sleeper app (via `sleeper_roster.
    # starters_slot_map`) rather than `weekly/lineup_state.yml`'s remembered
    # baseline -- callers building a lineup baseline (`webapi.
    # weekly_report_json`) must skip the lineup-state file entirely in that
    # case, since live starters already ARE the baseline and moves should
    # read as "change these in the app now," not drift against a stale
    # local file. "file" (the default, and the fallback on a fetch failure)
    # means the lineup-state file is still the right baseline, same as
    # before this feature existed.
    slots_source: str = "file"

    # Live-league-rosters wiring (see ffbot/league_rosters.py's
    # `fetch_league_rosters`) -- "file" (the default) leaves `league_rosters`
    # exactly what `league_rosters_path` held, unchanged. A failed "sleeper"
    # fetch falls back to that file too, same never-crash contract as every
    # other live seam; the reason lands here.
    league_rosters_source: str = "file"
    league_rosters_alerts: list[str] = field(default_factory=list)

    # Live-opponent-starters wiring (see ffbot/sleeper_standings.py's
    # `fetch_opponent_starters`) -- feeds `week.opponent_overlap` /
    # `SeasonConfig.opponent_correlation_weight`. Empty (the default) is an
    # exact no-op, same contract as every other optional live input here;
    # the fetch itself is skipped entirely whenever the weight that gates it
    # is 0.0, so this stays empty for every run that hasn't opted in, not
    # just ones where the fetch happened to fail.
    opponent_starters: list[week.OpponentStarter] = field(default_factory=list)
    opponent_alerts: list[str] = field(default_factory=list)


def default_weekly_path(week_num: int) -> Path:
    return Path("weekly") / f"week-{week_num:02d}.yml"


def _merge_kalshi_scores(weekly: week.WeeklyIntel, board: Board, scores: dict[str, float]) -> week.WeeklyIntel:
    """A copy of `weekly` with `scores` (`{board_key: 0..1}`, from
    `ffbot.markets.kalshi_nfl.weekly_signal`) merged into
    `players[...].kalshi` (rescaled to 0-100, `WeeklyPlayerIntel`'s own
    contract). Field-level, not whole-entry, precedence -- unlike
    `ffbot.live.conditions.merge_conditions`'s per-game GameInfo (always
    written as one atomic research pass by `/gameday`), a `players:` entry
    is very often hand-typed for an UNRELATED reason (a status override, a
    note) with no opinion on `kalshi` at all; only overwriting `kalshi`
    specifically when it's still unset is what keeps that pre-existing
    entry's other fields untouched while still honoring an explicit
    hand-typed `kalshi:` value if one is ever written.
    """
    if not scores:
        return weekly
    merged = dict(weekly.players)
    for key, score in scores.items():
        bp = board.by_key.get(key)
        if bp is None:
            continue
        name_key = normalize_name(bp.name)
        existing = merged.get(name_key)
        if existing is None:
            merged[name_key] = week.WeeklyPlayerIntel(name=bp.name, kalshi=score * 100.0)
        elif existing.kalshi is None:
            merged[name_key] = dataclasses.replace(existing, kalshi=score * 100.0)
    return dataclasses.replace(weekly, players=merged)


def load_everything(
    config_path: str = "config.yml",
    roster_path: str = "roster.yml",
    week_num: int = 1,
    proj_csv_paths: Sequence[str] | None = None,
    weekly_path: str | None = None,
    weeks_in_season: int = 17,
    league_rosters_path: str = "league_rosters.yml",
    season: int | None = None,
    source_override: str | None = None,
    kalshi_log_dir: str | None = None,
    refresh: bool = False,
) -> LoadedReport:
    """Load config, weekly intel, the draft board (if configured), and the
    roster, matched and ready for `week.build_week_brief`/`waiver_candidates`.

    Raises `ReportError` (never `SystemExit`) when the roster can't be
    loaded at all, or when there's neither a fresh weekly projection nor a
    board to fall back on — both cases where there is genuinely nothing to
    report on.

    `source_override` (`"board"`/`"sleeper"`/`"csv"`) overrides
    `cfg.projection_source.source` for this one call — the CLI's `--source`
    flag; the GUI leaves this `None` and always follows config, exactly like
    every other GUI-relevant setting. `season` overrides
    `ffbot.projections.current_nfl_season()`'s calendar-based guess — needed
    only when `source` resolves to `"sleeper"`, which is the only live
    provider that needs a season year at all.

    A live-provider fetch failure NEVER raises out of this function and
    never silently degrades either: it falls back to the season board (the
    `"board"`-source behavior) for this run, and the reason lands in the
    returned `LoadedReport.projection_alerts`, which every caller must
    surface to the user (see `scripts/week_report.py`/`ffbot/webapi.py`).

    `refresh=True` (the GUI's "Refresh" button; also passed by
    `scripts/autorun.py`'s fired pre-kickoff/pre-waiver runs, which need the
    latest information right before the moment it matters) bypasses every
    Sleeper league-state cache's normal TTL for this one call — league
    identity, rosters, users, the players dump, ownership, and this week's
    live projections/rest-of-season totals and auto-fetched weather/odds
    ALL refetch regardless of how fresh their cache already is. One shared
    `SleeperClient(force_refresh=True)` is built for the whole call (see
    `SleeperClient.__init__`'s own docstring) so this still costs one HTTP
    request per distinct endpoint, not one per call site. Note the
    rest-of-season projection fetch is per-remaining-week, so `refresh` on
    an early-season week can mean well over a dozen requests — accepted, in
    service of "genuinely latest" ahead of kickoff, not something to
    optimize away here.

    `kalshi_log_dir` (default: `None`, meaning `ffbot.markets.kalshi_log
    .DEFAULT_LOG_DIR`) is where a successful weekly Kalshi player-prop fetch
    gets appended for future grading (B7 — see that module's docstring and
    docs/dev/SPICE.md). Only reachable, same as the fetch itself, when
    `cfg.season.kalshi_weight != 0.0`; a logging failure is swallowed the
    same way a fetch failure is, never raised.
    """
    cfg = Config.load(config_path)

    # One shared client for every Sleeper-touching branch below, so
    # `refresh=True` forces each distinct endpoint fresh exactly once (see
    # `SleeperClient.__init__`) instead of once per call site, and so the
    # roster/league-rosters branches can share one ~5MB players-dump
    # download. Built once, up front, purely from config -- none of these
    # three sources need anything resolved later in this function to know
    # whether they're active.
    sleeper_client = None
    if (
        (cfg.standings_source.source == "sleeper" and cfg.league is not None)
        or cfg.roster_source.source == "sleeper"
        or cfg.league_rosters_source.source == "sleeper"
    ):
        from .sleeper.cache import DEFAULT_CACHE_DIR as SLEEPER_DEFAULT_CACHE_DIR
        from .sleeper.client import SleeperClient

        sleeper_client = SleeperClient(
            cache_dir=cfg.sleeper.cache_dir or SLEEPER_DEFAULT_CACHE_DIR, force_refresh=refresh,
        )

    standings_alerts: list[str] = []
    # Captured here (not just inside the try below) so the opponent-starters
    # seam further down can reuse the SAME resolved roster id without a
    # second `resolve_roster_id` round-trip -- set as soon as it's resolved,
    # even if `fetch_standings` itself goes on to fail, since resolving
    # "which roster is mine" and fetching this week's opponent are
    # independent Sleeper calls.
    my_roster_id_for_opponent: int | None = None
    if cfg.standings_source.source == "sleeper" and cfg.league is not None:
        from . import sleeper_roster
        from .sleeper.cache import SleeperFetchError
        from .sleeper_standings import fetch_standings, merge_standings

        try:
            client = sleeper_client
            standings_roster_id = cfg.sleeper.roster_id
            if standings_roster_id is None and cfg.sleeper.username:
                standings_roster_id = sleeper_roster.resolve_roster_id(
                    client, cfg.sleeper.league_id, cfg.sleeper.username,
                    roster_ttl_minutes=cfg.standings_source.cache_ttl_minutes,
                )
            my_roster_id_for_opponent = standings_roster_id
            teams, my_team_name, my_opponent_name = fetch_standings(
                client, cfg.sleeper.league_id, week_num, my_roster_id=standings_roster_id,
            )
            cfg.league = merge_standings(cfg.league, teams, my_team_name, my_opponent_name, week_num)
        except (SleeperFetchError, sleeper_roster.RosterSourceError) as exc:
            standings_alerts.append(
                f"Live standings (sleeper) unavailable this run ({exc}) — "
                "league.yml's own teams:/my_team/my_opponent stay as written."
            )

    weekly_p = Path(weekly_path) if weekly_path else default_weekly_path(week_num)
    weekly = week.load_weekly_intel(weekly_p)

    game_conditions_alerts: list[str] = []
    if cfg.game_conditions.weather_source != "off" or cfg.game_conditions.odds_source != "off":
        from .live import conditions as live_conditions

        resolved_season_for_conditions = season if season is not None else projections.current_nfl_season()
        # `refresh=True` bypasses this cache's TTL too -- a pre-kickoff
        # check firing right before a game wants the freshest possible
        # weather/odds read, not whatever was already cached this run.
        conditions_cfg = (
            dataclasses.replace(cfg.game_conditions, cache_ttl_minutes=0.0) if refresh else cfg.game_conditions
        )
        auto_games, game_conditions_alerts = live_conditions.fetch_conditions(
            resolved_season_for_conditions, week_num, conditions_cfg,
        )
        weekly = live_conditions.merge_conditions(weekly, auto_games)

    board = None
    board_alerts: list[str] = []
    try:
        board = load_board_from_config(cfg)
    except ValueError as exc:
        # No board configured, or the configured CSVs don't exist yet (the
        # common fresh-clone state) -- season-board fallback and waivers
        # just won't be available this run; start/sit still runs fine on
        # live projections. Surfaced, never silent -- see board_alerts.
        board_alerts.append(
            f"No draft board loaded ({exc}) — waiver-add valuation and the "
            "season-board fallback are off this run; start/sit still runs "
            "on live projections."
        )

    fallback_rows = []
    if board is not None:
        # A FIXED season length, not a shrinking "weeks remaining" -- see
        # `season_board_rows`'s own docstring for why, and why this must
        # match whatever `waiver_candidates` callers pass for the same
        # fallback-pricing convention (webapi.py, week_report.py).
        fallback_rows = rs.season_board_rows(board, weeks_in_season)

    # Weekly Kalshi per-player signal -- SPICE LEVEL 4 ONLY (see
    # SeasonConfig.SPICE_PRESETS). Skipped entirely, no network touched at
    # all, when the weight is 0.0 -- the same "don't even ask" guard
    # scripts/draft.py's _fetch_kalshi_draft_signal uses on the draft side.
    if cfg.season.kalshi_weight != 0.0 and board is not None:
        from .live import schedule as live_schedule
        from .live.schedule import ScheduleError
        from .markets import kalshi_log, kalshi_nfl

        resolved_season_for_kalshi = season if season is not None else projections.current_nfl_season()
        try:
            this_week_games = live_schedule.this_week_games(resolved_season_for_kalshi, week_num)
            kalshi_scores = kalshi_nfl.weekly_signal(this_week_games, board)
        except ScheduleError as exc:
            game_conditions_alerts.append(f"Kalshi weekly signal unavailable this run (schedule fetch failed: {exc}).")
            kalshi_scores = {}
        except Exception as exc:  # noqa: BLE001 -- a market-data hiccup must never crash the weekly report
            game_conditions_alerts.append(f"Kalshi weekly signal unavailable this run ({exc}).")
            kalshi_scores = {}
        weekly = _merge_kalshi_scores(weekly, board, kalshi_scores)

        # Forward-logging (B7) -- append this week's fetched signal for
        # future grading, no matter the outcome above; a no-op when there
        # was nothing to log (empty scores) or when disk I/O fails, same
        # never-crash contract as the fetch itself. See
        # ffbot.markets.kalshi_log's own docstring.
        game_odds = {
            team: {"team_total": g.team_total, "opp_total": g.opp_total}
            for team, g in weekly.games.items()
            if g.team_total is not None or g.opp_total is not None
        }
        kalshi_log.log_weekly_snapshot(
            resolved_season_for_kalshi, week_num, kalshi_scores, game_odds,
            log_dir=kalshi_log_dir if kalshi_log_dir is not None else kalshi_log.DEFAULT_LOG_DIR,
        )

    resolved_source = source_override or cfg.projection_source.source
    projection_alerts: list[str] = []
    weekly_points: dict[str, float] = {}
    provider_rows: list[dict] = []
    ros_board: Board | None = None

    if resolved_source == "sleeper":
        resolved_season = season if season is not None else projections.current_nfl_season()
        # `source_override` (the CLI's --source) may disagree with
        # `cfg.projection_source.source` -- resolve_provider must see the
        # OVERRIDDEN source, not the raw config value, or an override to
        # "sleeper" over a config default of "board" silently resolves to
        # no provider at all. `refresh=True` also forces this week's (and,
        # below, every rest-of-season week's) fetch past its normal TTL --
        # see this function's own docstring for the request-count tradeoff.
        effective_source_cfg = dataclasses.replace(
            cfg.projection_source, source=resolved_source,
            **({"cache_ttl_minutes": 0.0} if refresh else {}),
        )
        provider = projections.resolve_provider(effective_source_cfg)
        assert provider is not None  # "sleeper" always resolves to a real provider
        try:
            provider_rows = projections.weekly_rows(resolved_season, week_num, provider)
        except ProjectionFetchError as exc:
            projection_alerts.append(
                f"Live projections (sleeper) unavailable this run ({exc}) — "
                "falling back to the season board."
            )
            provider_rows = []
        else:
            # A second, independent league-scoring pass (load_roster below
            # does its own for the roster's rows) -- deliberately not shared,
            # since this one covers the WHOLE candidate pool, not just the
            # roster, and apply_league_scoring is cheap per row.
            scored_rows = [dict(r) for r in provider_rows]
            apply_league_scoring(scored_rows, cfg.league)
            weekly_points = {_board_key(r["name"], r["position"]): r["points"] for r in scored_rows}

            # A REAL rest-of-season total (weeks_num..weeks_in_season, each
            # scored and summed -- see ros_rows' own docstring), overlaid
            # onto a copy of the season board so ros_gain/hold_margin/
            # drop_cost read live numbers too, not just this week's half of
            # ros_blend. Reuses the SAME provider/season already resolved
            # above; a separate try/except because this is strictly more
            # network calls (one per remaining week) than the weekly fetch,
            # so it can fail independently of it.
            if board is not None:
                try:
                    ros_overlay_rows = projections.ros_rows(
                        resolved_season, week_num, weeks_in_season, provider, cfg.league,
                    )
                except ProjectionFetchError as exc:
                    projection_alerts.append(
                        f"Live rest-of-season projections (sleeper) unavailable this run "
                        f"({exc}) — ros_gain/hold_margin/drop_cost stay on the frozen "
                        "season board."
                    )
                else:
                    ros_board = rescale_board_points(
                        board, cfg.roster_positions, cfg.draft.num_teams, cfg, ros_overlay_rows,
                    )

    resolved_roster_source = cfg.roster_source.source
    roster_source_alerts: list[str] = []
    roster_entries_override = None
    sleeper_roster_players = None
    waiver_priority: int | None = None
    slots_source = "file"
    # Shared with the league-rosters branch below, so a live run downloads
    # the ~5MB players dump at most once, whichever branch reaches it
    # first.
    players_dump: dict | None = None

    if resolved_roster_source == "sleeper":
        from . import sleeper_roster
        from .sleeper.cache import SleeperFetchError

        resolved_season_for_roster = season if season is not None else projections.current_nfl_season()
        try:
            client = sleeper_client
            roster_ttl = cfg.roster_source.cache_ttl_minutes
            roster_id = cfg.sleeper.roster_id
            if roster_id is None:
                if not cfg.sleeper.username:
                    raise sleeper_roster.RosterSourceError(
                        "sleeper.roster_id is unset and sleeper.username is empty "
                        "-- cannot resolve which roster is yours"
                    )
                roster_id = sleeper_roster.resolve_roster_id(
                    client, cfg.sleeper.league_id, cfg.sleeper.username, roster_ttl_minutes=roster_ttl,
                )
            players_dump = client.players()
            try:
                ownership = client.ownership(resolved_season_for_roster, week_num)
            except SleeperFetchError:
                ownership = {}  # ownership is a nice-to-have on top of identity -- degrade quietly

            # The live lineup baseline: which slot each of your players
            # currently occupies in the Sleeper app. Only `client.league()`
            # (this league's ordered roster_positions, needed to zip against
            # `starters`) gets its own try/except -- a failure fetching THAT
            # specific piece shouldn't sink an otherwise-successful roster
            # fetch, only degrade the lineup baseline back to
            # weekly/lineup_state.yml. `resolve_starters_slot_map`'s own
            # `client.rosters()` call is deliberately NOT separately caught:
            # if it fails, `fetch_my_roster`'s own `rosters()` call below is
            # about to fail for the exact same reason, so letting it
            # propagate to the outer except produces ONE alert for the one
            # underlying failure, not two for it.
            slot_map: dict[str, str] = {}
            try:
                league_info = client.league(cfg.sleeper.league_id)
            except SleeperFetchError as exc:
                roster_source_alerts.append(
                    f"Live lineup slots (sleeper) unavailable this run ({exc}) — "
                    "lineup baseline falls back to weekly/lineup_state.yml."
                )
                league_info = None

            if league_info is not None:
                slot_map, slot_warnings = sleeper_roster.resolve_starters_slot_map(
                    client, cfg.sleeper.league_id, roster_id,
                    league_info.get("roster_positions") or [], roster_ttl_minutes=roster_ttl,
                )
                roster_source_alerts.extend(slot_warnings)
                slots_source = "sleeper"

            sleeper_roster_players = sleeper_roster.fetch_my_roster(
                client, cfg.sleeper.league_id, roster_id, players_dump, ownership,
                roster_ttl_minutes=roster_ttl, slot_map=slot_map,
            )
            roster_entries_override = sleeper_roster.merge_flags(sleeper_roster_players, roster_path)
            # Best-effort on top of an already-successful roster fetch --
            # reuses client.rosters()'s own TTL cache (same league_id/ttl
            # `fetch_my_roster` just called with), so this costs no extra
            # HTTP request. A league that has never processed a waiver
            # simply has no `waiver_position` yet -- that degrades to None
            # here exactly like a genuine fetch failure would, no separate
            # alert needed for something that isn't an error.
            waiver_priority = sleeper_roster.waiver_position(
                client, cfg.sleeper.league_id, roster_id, roster_ttl_minutes=roster_ttl,
            )
        except (SleeperFetchError, sleeper_roster.RosterSourceError) as exc:
            roster_source_alerts.append(
                f"Live roster (sleeper) unavailable this run ({exc}) — "
                f"falling back to {roster_path}."
            )
            sleeper_roster_players = None
            roster_entries_override = None
            slots_source = "file"

    csv_paths = list(proj_csv_paths or [])
    try:
        players, unmatched = rs.load_roster(
            csv_paths, roster_path, fallback_rows=fallback_rows, league=cfg.league,
            provider_rows=provider_rows, entries=roster_entries_override,
        )
    except rs.RosterError as exc:
        raise ReportError(str(exc)) from exc

    if sleeper_roster_players is not None:
        from . import sleeper_roster
        # roster_positions is always passed -- a player whose slot came
        # back unknown (slots_source == "file") simply has sp.slot == "",
        # which apply_sleeper_identity already treats as "leave
        # selected_position untouched," so this is a no-op in that case.
        players = sleeper_roster.apply_sleeper_identity(players, sleeper_roster_players, roster_positions=cfg.roster_positions)

    if not csv_paths and not provider_rows and not fallback_rows:
        raise ReportError(
            "No weekly projections and no draft board to fall back on — pass "
            "a weekly projection CSV, or set draft.board_csv in config.yml."
        )

    league_rosters_source = "file"
    league_rosters_alerts: list[str] = []
    league_rosters: LeagueRosters | None = None
    if cfg.league_rosters_source.source == "sleeper":
        from .sleeper.cache import SleeperFetchError

        try:
            client = sleeper_client
            lr_players_dump = players_dump if players_dump is not None else client.players()
            league_rosters = fetch_league_rosters(
                client, cfg.sleeper.league_id, lr_players_dump,
                week=week_num, ttl_minutes=cfg.league_rosters_source.cache_ttl_minutes,
            )
            league_rosters_source = "sleeper"
        except SleeperFetchError as exc:
            league_rosters_alerts.append(
                f"Live league rosters (sleeper) unavailable this run ({exc}) — "
                f"falling back to {league_rosters_path}."
            )
            league_rosters = None

    if league_rosters is None:
        league_rosters = load_league_rosters(league_rosters_path)

    # Live opponent starters -- the "don't even ask" pattern every optional
    # live seam in this repo uses: the fetch is skipped entirely whenever
    # `opponent_correlation_weight` is 0.0 (the default), not merely
    # discarded after the fact. Needs a resolved roster id (from the
    # standings branch above) and a shared client; reuses `players_dump`
    # from the roster/league-rosters branches when either already fetched
    # it, so this costs at most one extra HTTP request (the matchups call
    # itself, likely already warm from the standings branch's own TTL
    # cache) rather than a second ~5MB players-dump download.
    opponent_starters: list[week.OpponentStarter] = []
    opponent_alerts: list[str] = []
    if (
        cfg.season.opponent_correlation_weight != 0.0
        and cfg.standings_source.source == "sleeper"
        and sleeper_client is not None
        and my_roster_id_for_opponent is not None
    ):
        from .sleeper.cache import SleeperFetchError
        from .sleeper_standings import fetch_opponent_starters, starters_identity

        try:
            client = sleeper_client
            _opp_roster_id, opp_starter_ids = fetch_opponent_starters(
                client, cfg.sleeper.league_id, week_num, my_roster_id_for_opponent,
            )
            if opp_starter_ids:
                opp_players_dump = players_dump if players_dump is not None else client.players()
                opponent_starters = starters_identity(opp_starter_ids, opp_players_dump)
        except SleeperFetchError as exc:
            opponent_alerts.append(
                f"Live opponent starters (sleeper) unavailable this run ({exc}) — "
                "the opponent-correlation discount is off for this run."
            )

    stadiums = week.load_stadiums()

    return LoadedReport(
        cfg=cfg,
        weekly=weekly,
        board=board,
        board_alerts=board_alerts,
        players=players,
        unmatched=unmatched,
        stadiums=stadiums,
        league_rosters=league_rosters,
        projection_source=resolved_source,
        projection_alerts=projection_alerts,
        weekly_points=weekly_points,
        roster_source=resolved_roster_source,
        roster_source_alerts=roster_source_alerts,
        waiver_priority=waiver_priority,
        ros_board=ros_board,
        game_conditions_alerts=game_conditions_alerts,
        standings_alerts=standings_alerts,
        slots_source=slots_source,
        league_rosters_source=league_rosters_source,
        league_rosters_alerts=league_rosters_alerts,
        opponent_starters=opponent_starters,
        opponent_alerts=opponent_alerts,
    )
