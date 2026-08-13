#!/usr/bin/env python3
"""A throwaway, self-contained 2025 season for rehearsing the in-season
weekly manager's GUI before it matters for real, in a real September.

    python scripts/demo_season.py build --season 2025    # materialize demo/2025/
    python scripts/demo_season.py goto 2025-10-12          # move the clock
    python scripts/demo_season.py serve --port 8322        # browse it

Must be run from the repo root (same assumption every other script here
makes — `ffbot.config.Config.load`'s `league_file` resolution, in
particular, is CWD-relative). `build`/`goto` never touch the real
`roster.yml`/`weekly/`/`league.yml` — everything lives under `demo/<season>/`,
a complete, isolated ffbot working directory. `serve` just `chdir`s into it
and hands off to `scripts/gui.py`'s own `main()` — no new GUI flags, no risk
to the real files.

The "clock" is one date, resolved into BOTH a week number and a day-of-week
state (`wed`/`fri`/`sun`) off the already-cached `games.csv`. nflverse's
`injuries_{season}.csv` carries one row per (player, week) — the FINAL
weekly report, not a day-by-day time series — so the three states are
reconstructed from two columns on that single row (`practice_status` for
`wed`, `report_status` for `fri`) rather than replayed from three real
snapshots. `sun` additionally asks whether a Questionable player actually
appeared in the box score, which is real hindsight (hidden from the other
two states) and only ever used when `goto --allow-hindsight` asks for it.

This module must stay importable/runnable with no network at module level —
same offline invariant as scripts/draft.py, scripts/week_report.py,
scripts/gui.py. Every network call here goes through
`ffbot.history.fetch`'s cache-first `fetch_rows`/`fetch_json`, exactly like
every other historical-replay script.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot import roster_editor  # noqa: E402
from ffbot import weekly_editor  # noqa: E402
from ffbot.backtest.draft_sim import DraftResult, agent_roster, simulate_draft  # noqa: E402
from ffbot.backtest.schedule import round_robin_schedule, score_schedule  # noqa: E402
from ffbot.backtest.season import static_opponent_points  # noqa: E402
from ffbot.board import Board, load_board_from_config  # noqa: E402
from ffbot.config import Config, LeagueScoring, _deep_merge  # noqa: E402
from ffbot.history.actuals import week_actuals  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_rows  # noqa: E402
from ffbot.history.index import as_of, injury_report  # noqa: E402
from ffbot.history.names import actuals_key, canonical_team  # noqa: E402
from ffbot.history.signals import combine_providers, historical_form, scoring_form, usage_divergence, usage_form  # noqa: E402
from ffbot.league_rosters import LeagueRosters  # noqa: E402
from ffbot.names import normalize_name  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_ROOT = REPO_ROOT / "demo"

_SIGNAL_PROVIDER = combine_providers(historical_form, usage_form, scoring_form, usage_divergence)
_MAX_WEEK = 17  # matches the GUI's own --weeks-in-season default
_DAY_STATES = ("wed", "fri", "sun")


# --- Demo directory layout -------------------------------------------------


def _demo_dir(season: int) -> Path:
    return DEMO_ROOT / str(season)


def _board_dir(demo_dir: Path) -> Path:
    return demo_dir / "board"


def _variants_dir(demo_dir: Path) -> Path:
    return demo_dir / "weekly" / "variants"


def _standings_dir(demo_dir: Path) -> Path:
    return demo_dir / "standings"


def _meta_path(demo_dir: Path) -> Path:
    return demo_dir / "demo_meta.yml"


# --- Config resolution -------------------------------------------------
#
# The demo script itself always runs with CWD == repo root (same assumption
# every other script here makes), so every path it hands to a library
# function is explicit/absolute -- never one of the plain relative strings
# written into the demo's own on-disk config.yml/league.yml, which are
# relative to demo_dir on purpose (correct once `serve` chdirs there).


def _load_base_cfg() -> Config:
    """The real repo's config.yml + config.local.yml, deep-merged -- the
    starting point for the demo's roster shape, spice level, and league
    scoring path."""
    return Config.load(str(REPO_ROOT / "config.yml"))


def _demo_cfg_for_script(demo_dir: Path) -> Config:
    """The demo's own config.yml/config.local.yml, with every path resolved
    explicitly against `demo_dir` -- safe for the build script's own use
    regardless of its CWD. Never write this cfg back to disk; it exists
    only for in-process calls (board loading, draft sim, signal providers)."""
    cfg = Config.load(str(demo_dir / "config.yml"))
    league_path = demo_dir / (cfg.league_file or "league.yml")
    league = LeagueScoring.load(str(league_path)) if league_path.exists() else None
    board_csv = [str(_board_dir(demo_dir) / "projections.csv"), str(_board_dir(demo_dir) / "adp.csv")]
    draft = dataclasses.replace(cfg.draft, board_csv=board_csv, intel_file=str(demo_dir / "draft" / "intel.yml"))
    return dataclasses.replace(cfg, league=league, draft=draft)


def _load_demo_board(demo_dir: Path, cfg: Config) -> Board:
    return load_board_from_config(cfg, csv_paths=cfg.draft.board_csv)


# --- 1. Board: historical_board() -> FantasyPros-shaped CSVs ---------------


def _team_byes(season: int, cache_dir: Path, opener: UrlOpener) -> dict[str, int]:
    """`{canonical team abbrev: bye week}` for `season`'s regular season,
    derived from which week each team has no game in `games.csv` -- FFC's
    own `bye` field only covers the ~200 players it ranks, which silently
    leaves most of the board with no bye at all (and `lineup._must_bench`
    can never fire for a null bye)."""
    rows = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    weeks_played: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        try:
            if int(row.get("season", -1)) != season or row.get("game_type") != "REG":
                continue
            wk = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        for side in ("home_team", "away_team"):
            team = canonical_team(row.get(side))
            if team:
                weeks_played[team].add(wk)

    if not weeks_played:
        return {}
    max_week = max(w for weeks in weeks_played.values() for w in weeks)
    return {
        team: next(w for w in range(1, max_week + 1) if w not in weeks)
        for team, weeks in weeks_played.items()
        if len(weeks) < max_week
    }


_PROJECTIONS_HEADER = ["Player Name", "Team", "POS", "Bye", "FPTS"]
_ADP_HEADER = ["Player Name", "Team", "POS", "Bye", "AVG", "Std Dev"]


def _write_board_csvs(board: Board, byes: dict[str, int], board_dir: Path) -> dict:
    """The pure part of `build_board_csvs`: write `board.players` back out
    as two plain FantasyPros-shaped CSVs -- decoupled from the network-
    touching `historical_board()`/`_team_byes()` calls so it's directly
    testable (and reusable) against a hand-built `Board`."""
    board_dir.mkdir(parents=True, exist_ok=True)

    with_bye = 0
    with (board_dir / "projections.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_PROJECTIONS_HEADER)
        for bp in board.players:
            bye = byes.get(canonical_team(bp.team), bp.bye_week)
            if bye is not None:
                with_bye += 1
            w.writerow([bp.name, bp.team, bp.position, bye if bye is not None else "", round(bp.points, 2)])

    with (board_dir / "adp.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_ADP_HEADER)
        for bp in board.players:
            bye = byes.get(canonical_team(bp.team), bp.bye_week)
            w.writerow([
                bp.name, bp.team, bp.position, bye if bye is not None else "",
                bp.adp if bp.adp is not None else "",
                bp.adp_stdev if bp.adp_stdev is not None else "",
            ])

    return {"players": len(board.players), "with_bye": with_bye}


def build_board_csvs(season: int, base_cfg: Config, demo_dir: Path, cache_dir: Path, opener: UrlOpener) -> dict:
    """`ffbot.history.board.historical_board(season, ...)` written back out
    as two plain FantasyPros-shaped CSVs, so the demo exercises the exact
    same `ffbot.board.read_fantasypros`/`load_board` pipeline a real board
    does, not a bypass. Returns a small coverage dict for `build`'s printout.
    """
    from ffbot.history.board import historical_board  # local: avoids importing at module level

    board = historical_board(season, base_cfg, num_teams=base_cfg.draft.num_teams, cache_dir=cache_dir, opener=opener)
    byes = _team_byes(season, cache_dir, opener)
    return _write_board_csvs(board, byes, _board_dir(demo_dir))


# --- 2. Roster + rivals: draft_sim.simulate_draft() -------------------------


def build_rosters(demo_dir: Path, cfg: Config, board: Board, agent_slot: int, seed: int) -> DraftResult:
    """Simulate a real `num_teams`-team snake draft over the demo board
    (the agent via `draft.recommend()`, everyone else by noisy ADP), then
    write `roster.yml` (the agent's own team) and `league_rosters.yml` (the
    other 11) -- this is what gives the corrected free-agent pool and the
    denial-holds panel real content instead of staying exact no-ops."""
    result = simulate_draft(
        board, cfg, num_teams=cfg.draft.num_teams, rounds=cfg.draft.rounds,
        agent_slot=agent_slot, seed=seed, order=cfg.draft.order,
    )

    my_keys = agent_roster(result, agent_slot)
    my_names = [board.by_key[k].name for k in my_keys if k in board.by_key]
    roster_editor.write_roster_entries(str(demo_dir / "roster.yml"), [{"name": n} for n in my_names])

    teams: dict[str, list[str]] = {}
    for i, roster_keys in enumerate(result.rosters, start=1):
        if i == agent_slot:
            continue
        teams[f"team_{i}"] = [board.by_key[k].name for k in roster_keys if k in board.by_key]
    league_rosters = LeagueRosters(week=None, generated="demo_season.py build", source="draft_sim", teams=teams)
    (demo_dir / "league_rosters.yml").write_text(
        yaml.safe_dump(
            {"week": league_rosters.week, "generated": league_rosters.generated,
             "source": league_rosters.source, "teams": league_rosters.teams, "unmatched": []},
            sort_keys=False, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return result


# --- 3. Weekly intel variants (wed / fri / sun) -----------------------------


def _kickoff_et(row: dict) -> str:
    gameday = (row.get("gameday") or "").strip()
    gametime = (row.get("gametime") or "").strip()
    if not gameday:
        return ""
    return f"{gameday}T{gametime}" if gametime else gameday


def _with_kickoffs(games: dict, game_rows: tuple[dict, ...]):
    """A copy of `snap.games` with `kickoff_et` filled in from `game_rows`'
    `gameday`+`gametime` -- `as_of()` never sets this (see
    `ffbot.history.index`'s module docstring), but it's sitting right there
    on the same ground-truth channel `ffbot.history.openmeteo` already reads
    for weather. Not a result (a schedule time isn't an outcome), just a
    field `as_of()`'s adapter happens not to carry over yet."""
    out = dict(games)
    for row in game_rows:
        kickoff = _kickoff_et(row)
        if not kickoff:
            continue
        home = canonical_team(row.get("home_team"))
        away = canonical_team(row.get("away_team"))
        if home in out:
            out[home] = dataclasses.replace(out[home], kickoff_et=kickoff)
        if away in out:
            out[away] = dataclasses.replace(out[away], kickoff_et=kickoff)
    return out


def _practice_note(row) -> str:
    status = row.practice_status.lower()
    if "did not participate" in status:
        verb = "did not practice"
    elif "limited" in status:
        verb = "was limited in practice"
    elif "full" in status:
        verb = "practiced fully"
    else:
        return ""
    injury = f" ({row.injury.lower()})" if row.injury else ""
    return f"{verb} this week{injury}"


def _report_note(row) -> str:
    if not row.status:
        return ""
    label = {"Q": "questionable", "D": "doubtful", "O": "out"}.get(row.status, row.status.lower())
    injury = f" — {row.injury.lower()}" if row.injury else ""
    return f"listed {label}{injury}"


def _weather_note(game) -> str:
    if game is None:
        return ""
    bits = []
    if game.wind_mph is not None and game.wind_mph >= 15:
        bits.append(f"{game.wind_mph:.0f} mph wind")
    if game.team_total is not None and game.opp_total is not None:
        bits.append(f"implied total {game.team_total:.1f}")
    return "; ".join(bits)


def _hindsight_note(row, actuals: dict[str, float], position: str) -> str:
    """Whether this Questionable player's box-score line is missing --
    genuine hindsight (whether the Q flag resolved to a DNP), never used
    unless the caller explicitly asked for the `sun` state with
    `--allow-hindsight`. `actuals` comes from `week_actuals`, a
    results-bearing source -- this is the one place in the demo generator
    that deliberately crosses that line, and only for this."""
    if row.status != "Q":
        return ""
    key = actuals_key(row.name, position)
    if key not in actuals:
        return "(hindsight) did not appear in the box score — likely inactive"
    return "(hindsight) played despite the Questionable tag"


def build_weekly_variants(
    demo_dir: Path,
    season: int,
    cfg: Config,
    board: Board,
    rostered_names: set[str],
    cache_dir: Path,
    opener: UrlOpener,
    weeks: range = range(1, _MAX_WEEK + 1),
) -> dict:
    """One `weekly/variants/week-NN-{wed,fri,sun}.yml` per (week, day
    state), with player entries restricted to `rostered_names` (the AGENT's
    own roster -- see `cmd_build`'s call site).

    Deliberately NOT the whole league: `week.unmatched_player_warnings`
    flags every weekly-intel player entry that matches nobody on the
    CALLER's own roster, by design (a real researcher curates their own
    team's news into this file, and an entry for someone else's player is
    exactly the typo/mismatch that check exists to catch) -- filling this
    file with all 168 rostered players across the league would flood that
    section with ~150 false positives every single week. Free-agent/
    streaming candidates don't need an entry here either: `rank_streamers`/
    `waiver_candidates`' own "reason" text is derived from `weekly.games`
    (matchups, always written for every team), never from `weekly.players`.
    Returns coverage counters for `build`'s printout: how many of the
    agent's own players resolved to a game (weather/Vegas) this generation
    covers.
    """
    variants_dir = _variants_dir(demo_dir)
    variants_dir.mkdir(parents=True, exist_ok=True)

    name_by_norm = {normalize_name(bp.name): bp.name for bp in board.players}
    team_by_norm = {normalize_name(bp.name): canonical_team(bp.team) for bp in board.players}
    pos_by_norm = {normalize_name(bp.name): bp.position for bp in board.players}

    resolved_games = 0
    total_rostered_game_checks = 0
    no_team_checks = 0  # "FA"/blank team in the source data -- not a mismatch, just unresolvable
    _SIGNAL_KEYS = ("volatility", "upside", "usage_trend", "momentum", "divergence")

    for wk in weeks:
        snap = as_of(season, wk, cache_dir=cache_dir, opener=opener)
        snap = snap.with_signals(_SIGNAL_PROVIDER(season, wk, cfg, cache_dir=cache_dir, opener=opener))
        games_with_kickoff = _with_kickoffs(snap.games, snap.game_rows)
        intel = dataclasses.replace(snap, games=games_with_kickoff).weekly_intel()
        base_editor = weekly_editor.editor_json_from_intel(intel)
        base_editor["week"] = wk
        # `with_signals` keys a status-having entry by its real display name
        # but a signal-only entry by the bare normalized name (see
        # `WeekSnapshot.with_signals`) -- normalize both so a lookup by
        # normalized name always finds either kind.
        base_by_norm = {normalize_name(k): v for k, v in base_editor["players"].items()}

        reports = injury_report(season, wk, cache_dir=cache_dir, opener=opener)
        actuals = week_actuals(season, wk, cfg.league or LeagueScoring.fantasypros_default(), cache_dir=cache_dir, opener=opener)

        # Coverage counted once per week, not per day-state, so re-running
        # the same week for wed/fri/sun doesn't triple-count it.
        for norm_name in rostered_names:
            team = team_by_norm.get(norm_name)
            if team is None:
                continue
            if not team or team == "FA":
                no_team_checks += 1
                continue
            total_rostered_game_checks += 1
            if team in games_with_kickoff:
                resolved_games += 1

        for day_state in _DAY_STATES:
            editor = {**base_editor, "players": {}}
            if day_state == "wed":
                editor["source_notes"] = f"demo: week {wk}, Wednesday — practice report only, no injury designation or weather/Vegas yet."
                editor["matchups"] = [
                    {**m, "wind_mph": None, "precip_pct": None, "home_team_total": None, "away_team_total": None}
                    for m in base_editor["matchups"]
                ]
            elif day_state == "fri":
                editor["source_notes"] = f"demo: week {wk}, Friday — final injury report, weather/Vegas as of kickoff."
            else:
                editor["source_notes"] = (
                    f"demo: week {wk}, Sunday — Friday's report plus HINDSIGHT on whether a Questionable "
                    "player actually played. Only ever used behind `goto --allow-hindsight`."
                )

            for norm_name in rostered_names:
                signals = base_by_norm.get(norm_name, {})
                entry: dict = {k: signals[k] for k in _SIGNAL_KEYS if signals.get(k) is not None}

                row = reports.get(norm_name)
                if row is not None:
                    team = team_by_norm.get(norm_name)
                    game = games_with_kickoff.get(team) if team else None
                    if day_state == "wed":
                        note = _practice_note(row)
                        if note:
                            entry["note"] = note
                    else:
                        if row.status:
                            entry["status"] = row.status
                        notes = [n for n in (_report_note(row), _weather_note(game)) if n]
                        if day_state == "sun":
                            hindsight = _hindsight_note(row, actuals, pos_by_norm.get(norm_name, ""))
                            if hindsight:
                                notes.append(hindsight)
                        if notes:
                            entry["note"] = "; ".join(notes)

                if entry:
                    display_name = name_by_norm.get(norm_name, row.name if row is not None else norm_name)
                    editor["players"][display_name] = entry

            weekly_editor.write_weekly_intel(variants_dir / f"week-{wk:02d}-{day_state}.yml", editor)

    return {"resolved_games": resolved_games, "rostered_game_checks": total_rostered_game_checks, "no_team_checks": no_team_checks}


def _write_blank_week(path: Path, week_num: int) -> None:
    weekly_editor.write_weekly_intel(path, {"week": week_num, "generated": "", "source_notes": "", "players": {}, "matchups": []})


# --- 4. Standings ------------------------------------------------------------


def build_standings(
    demo_dir: Path, season: int, cfg: Config, board: Board, draft: DraftResult,
    agent_slot: int, cache_dir: Path, opener: UrlOpener, weeks: range = range(1, _MAX_WEEK + 1),
) -> None:
    """Realized weekly points for all `num_teams` simulated rosters (via
    `ffbot.backtest.season.static_opponent_points` -- the exact "start the
    best healthy player" baseline the season simulator already uses for its
    static opponents), paired by a round-robin schedule into cumulative
    win/loss standings, written one `standings/week-NN.yml` per week.
    `goto` stamps the right one into `league.yml`'s `teams:` block. Without
    this, `denial.py` and `week.matchup_lean` are exact no-ops."""
    num_teams = cfg.draft.num_teams
    points_by_team_week: dict[tuple[int, int], float] = {}
    for slot in range(1, num_teams + 1):
        pts = static_opponent_points(season, cfg, board, draft, slot, source="naive", weeks=list(weeks), cache_dir=cache_dir, opener=opener)
        for wk, p in pts.items():
            points_by_team_week[(slot, wk)] = p

    standings_dir = _standings_dir(demo_dir)
    standings_dir.mkdir(parents=True, exist_ok=True)
    schedule = round_robin_schedule(num_teams, max(weeks))

    for wk in weeks:
        schedule_upto = schedule[:wk]
        records = score_schedule(schedule_upto, points_by_team_week)

        totals = {slot: sum(points_by_team_week.get((slot, w), 0.0) for w in range(1, wk + 1)) for slot in range(1, num_teams + 1)}
        ranked = sorted(range(1, num_teams + 1), key=lambda slot: (-records.get(slot).win_rate if slot in records else 0.0, -totals[slot]))

        teams = []
        for seed, slot in enumerate(ranked, start=1):
            rec = records.get(slot)
            wl = f"{rec.wins}-{rec.losses}" + (f"-{rec.ties}" if rec.ties else "") if rec else "0-0"
            teams.append({
                "name": f"team_{slot}",
                "record": wl,
                "seed": seed,
                "waiver_priority": num_teams - seed + 1,  # worse record picks first
                "eliminated": False,  # not modeled -- see the demo README's fidelity limits
            })

        my_opponent = ""
        if schedule_upto:
            for a, b in schedule_upto[-1]:
                if a == agent_slot:
                    my_opponent = f"team_{b}"
                elif b == agent_slot:
                    my_opponent = f"team_{a}"

        (standings_dir / f"week-{wk:02d}.yml").write_text(
            yaml.safe_dump({"teams": teams, "my_opponent": my_opponent}, sort_keys=False), encoding="utf-8",
        )


# --- Config/league/meta scaffolding -----------------------------------------


def _write_demo_config(demo_dir: Path) -> None:
    import shutil

    shutil.copyfile(REPO_ROOT / "config.yml", demo_dir / "config.yml")

    real_local = REPO_ROOT / "config.local.yml"
    local_raw = yaml.safe_load(real_local.read_text(encoding="utf-8")) if real_local.exists() else {}
    local_raw = local_raw or {}

    overrides = {
        "draft": {
            "board_csv": ["board/projections.csv", "board/adp.csv"],
            # Doesn't exist under demo_dir -- missing-file no-op, same
            # contract as the real repo's own draft.intel_file.
            "intel_file": "draft/intel.yml",
            "board_points_source": "csv",
            # See kalshi_weight's note below -- forced off here too, since
            # draft.spice_level: 5 would otherwise turn it on the same way.
            "kalshi_weight": 0.0,
        },
        # Forced regardless of what the real repo's config(.local).yml says:
        # every "sleeper" source below fetches the CURRENT NFL season/league,
        # which would be actively wrong here -- this demo replays a specific
        # PAST season against SIMULATED opponents, and a live fetch would
        # either 404 (no real league_id), mismatch it against the wrong
        # year's roster, or (worse) silently look plausible while being
        # wrong. This demo's whole purpose is credential-free offline
        # rehearsal, so every live-data switch the real repo might have
        # turned on gets forced back to its file-based default here, not
        # just the one (projection_source) that predates the others.
        "projection_source": {"source": "board"},
        "roster_source": {"source": "file"},
        "standings_source": {"source": "file"},
        "game_conditions": {"weather_source": "off", "odds_source": "off"},
        # season.kalshi_weight is gated to spice_level 5 (see SPICE_PRESETS)
        # and, unlike weather/odds above, is NOT covered by game_conditions
        # being off -- ffbot/report.py's weekly Kalshi player-signal wiring
        # fetches independently of that switch. A future tuning session
        # bumping the real repo's spice_level to 5 to test the Kalshi
        # wiring must not also make every demo run reach out to the real
        # api.sleeper.app/Kalshi for the CURRENT actual week while replaying
        # a past season -- see the "sleeper" note above for the same
        # underlying risk.
        "season": {"kalshi_weight": 0.0},
    }
    merged = _deep_merge(local_raw, overrides)
    (demo_dir / "config.local.yml").write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")


def _write_league_base(demo_dir: Path) -> None:
    """`league.yml`'s scoring rules, minus the mutable standings block --
    `goto` re-merges this with one `standings/week-NN.yml` every time it
    runs. Unlike the real repo's `league.yml` (hand-narrated, never
    rewritten by code -- see CLAUDE.md), this copy is wholly generated for
    a generated environment and is safe to round-trip through
    `yaml.safe_dump`, same reasoning as `league_rosters.yml`."""
    raw = yaml.safe_load((REPO_ROOT / "league.yml").read_text(encoding="utf-8")) or {}
    for key in ("teams", "week", "my_team", "my_opponent"):
        raw.pop(key, None)
    text = "# GENERATED by scripts/demo_season.py -- do not hand-edit; re-run `goto` to update.\n" + yaml.safe_dump(raw, sort_keys=False)
    (demo_dir / "league.base.yml").write_text(text, encoding="utf-8")


def _write_meta(demo_dir: Path, season: int, agent_slot: int, num_teams: int, seed: int) -> None:
    (_meta_path(demo_dir)).write_text(
        yaml.safe_dump({"season": season, "agent_slot": agent_slot, "num_teams": num_teams, "seed": seed, "max_week": _MAX_WEEK}, sort_keys=False),
        encoding="utf-8",
    )


def _load_meta(demo_dir: Path) -> dict:
    p = _meta_path(demo_dir)
    if not p.exists():
        raise SystemExit(f"error: {p} not found -- run `build` first")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


_README_TEMPLATE = """\
# Demo season {season}

Generated by `scripts/demo_season.py build --season {season}`. Throwaway,
self-contained -- safe to delete and rebuild any time.

## What to click

Run `python scripts/demo_season.py serve --port 8322` from the repo root,
then open http://127.0.0.1:8322/weekly. The week box already matches the
clock's current week (set via `goto`); change it freely within the clock.
Run the brief with `--stream K DEF` and waivers on to see every panel.

## Moving the clock

    python scripts/demo_season.py goto 2025-10-12   # Sunday -> week 6, game-day state
    python scripts/demo_season.py goto 2025-10-08   # Wednesday -> week 6, practice-only state

Weeks at or before the clock carry real intel; weeks after it are blank --
you genuinely can't know week 15's weather in week 6.

## Known fidelity limits

- Projections are a frozen preseason board rescaled per week (`season_board_rows`),
  so a week-14 projection still looks like a preseason one -- this demo
  always uses `projection_source: board` regardless of what your real
  `config.local.yml` says, since a live source (`sleeper`) fetches the
  CURRENT NFL season, not a past demo season like this one. For a real
  season, set `projection_source.source: sleeper` in config.yml (or
  `--source sleeper` on `week_report.py`) to get real weekly projections
  and league-scored numbers instead of this rescaled fallback -- see
  docs/INSEASON.md.
- `wind`/`temp` are recorded actuals from `games.csv`, not the Thursday
  forecast a real manager saw; Vegas numbers are closing lines, not
  Sunday-morning ones. Both are slightly sharper than reality, in the
  demo's favor.
- The `wed`/`fri`/`sun` day states are reconstructed from
  `practice_status`/`report_status` on nflverse's single weekly injury row,
  not replayed from three real snapshots.
- `sun` uses genuine hindsight (whether a Questionable player's box-score
  line exists) and is opt-in via `goto --allow-hindsight` -- never the
  default, since a real Sunday morning doesn't know who's inactive until
  ~90 minutes before kickoff.
- The date -> day-state mapping is a plain weekday rule (Mon-Wed -> wed,
  Thu-Sat -> fri, Sun -> sun); it doesn't account for Monday-night games.
- Standings come from a synthetic round-robin over the 12 simulated,
  static-lineup rosters (no waivers of their own), not a real league; no
  team is ever marked eliminated.
- League scoring recomputation (`league.yml`'s rules vs. FantasyPros
  consensus) doesn't apply to the demo board -- the generated CSVs carry
  season totals only, no per-stat columns, so every player stays on
  consensus points. Real weekly deltas from actual results are unaffected.
"""


def _write_readme(demo_dir: Path, season: int) -> None:
    (demo_dir / "README.md").write_text(_README_TEMPLATE.format(season=season), encoding="utf-8")


# --- CLI ---------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    import shutil

    demo_dir = _demo_dir(args.season)
    demo_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)

    print(f"building demo season {args.season} in {demo_dir}")
    print("first-run downloads (cache-first, one-time): stats_player_week/stats_team_week/injuries/ffc_adp "
          f"for {args.season} if not already cached under {cache_dir} (~9.5 MB total)")

    base_cfg = _load_base_cfg()
    opener = _default_opener

    (demo_dir / "data").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "data" / "stadiums.yml", demo_dir / "data" / "stadiums.yml")
    _write_demo_config(demo_dir)
    _write_league_base(demo_dir)

    board_stats = build_board_csvs(args.season, base_cfg, demo_dir, cache_dir, opener)
    print(f"board: {board_stats['players']} players, {board_stats['with_bye']} with a resolved bye week")

    cfg = _demo_cfg_for_script(demo_dir)
    board = _load_demo_board(demo_dir, cfg)

    agent_slot = args.agent_slot if args.agent_slot is not None else cfg.draft.my_slot
    draft = build_rosters(demo_dir, cfg, board, agent_slot, args.seed)
    my_names = {normalize_name(n) for n in [board.by_key[k].name for k in agent_roster(draft, agent_slot) if k in board.by_key]}
    print(f"draft: agent is team_{agent_slot} ({len(my_names)} players), {cfg.draft.num_teams - 1} rival rosters")

    # Only the agent's OWN roster -- see build_weekly_variants' docstring
    # for why rival players must never appear in weekly.players.
    weekly_stats = build_weekly_variants(demo_dir, args.season, cfg, board, my_names, cache_dir, opener)
    if weekly_stats["rostered_game_checks"]:
        pct = 100.0 * weekly_stats["resolved_games"] / weekly_stats["rostered_game_checks"]
        expected_floor = 100.0 * (1 - 1.0 / _MAX_WEEK)  # ~1 bye week per player per season
        print(f"weekly intel: {weekly_stats['resolved_games']}/{weekly_stats['rostered_game_checks']} "
              f"rostered-player-weeks resolved to a game ({pct:.0f}%; expect ~{expected_floor:.0f}% from "
              "ordinary bye weeks alone) -- a number well below that usually means a team-abbreviation "
              "mismatch between the board and games.csv, not just byes")
        if weekly_stats["no_team_checks"]:
            print(f"  ({weekly_stats['no_team_checks']} additional player-weeks skipped: no NFL team listed "
                  "in the source data -- undrafted rookies/free agents, not a mismatch)")

    build_standings(demo_dir, args.season, cfg, board, draft, agent_slot, cache_dir, opener)
    print("standings: written for weeks 1-%d" % _MAX_WEEK)

    _write_meta(demo_dir, args.season, agent_slot, cfg.draft.num_teams, args.seed)
    _write_readme(demo_dir, args.season)

    # Leave the clock at week 1 (wed) so `build` alone produces something
    # `report.load_everything` can load immediately, before anyone runs `goto`.
    _apply_clock(demo_dir, week=1, day_state="wed", allow_hindsight=False)
    print(f"done. clock defaults to week 1 (wed) -- run `goto <date>` to move it, then `serve`.")
    return 0


def _week_bounds(season: int, cache_dir: Path, opener: UrlOpener) -> dict[int, tuple[dt.date, dt.date]]:
    rows = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    bounds: dict[int, list[dt.date]] = defaultdict(list)
    for row in rows:
        try:
            if int(row.get("season", -1)) != season or row.get("game_type") != "REG":
                continue
            wk = int(row.get("week"))
            gameday = dt.date.fromisoformat((row.get("gameday") or "").strip())
        except (TypeError, ValueError):
            continue
        bounds[wk].append(gameday)
    return {wk: (min(days), max(days)) for wk, days in bounds.items()}


def resolve_clock(date: dt.date, week_bounds: dict[int, tuple[dt.date, dt.date]]) -> tuple[int, str]:
    """`date` -> `(week, day_state)`. Week: the first week whose games end
    on/after `date` (clamps to the last known week once the season is
    over). Day state: a plain weekday rule -- Mon/Tue/Wed -> "wed" (before
    the official report), Thu/Fri/Sat -> "fri" (the report is in), Sun ->
    "sun" (gameday) -- see the demo README for why this doesn't special-case
    Monday-night games."""
    if not week_bounds:
        raise ValueError("no week bounds available -- games.csv not cached for this season")
    week = max(week_bounds)
    for wk in sorted(week_bounds):
        _start, end = week_bounds[wk]
        if date <= end:
            week = wk
            break
    weekday = date.weekday()  # Mon=0 .. Sun=6
    if weekday <= 2:
        day_state = "wed"
    elif weekday <= 5:
        day_state = "fri"
    else:
        day_state = "sun"
    return week, day_state


def _apply_clock(demo_dir: Path, week: int, day_state: str, allow_hindsight: bool) -> None:
    meta = _load_meta(demo_dir) if _meta_path(demo_dir).exists() else {}
    agent_slot = meta.get("agent_slot", 1)

    weekly_dir = demo_dir / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    variants_dir = _variants_dir(demo_dir)

    for wk in range(1, _MAX_WEEK + 1):
        dest = weekly_dir / f"week-{wk:02d}.yml"
        if wk > week:
            _write_blank_week(dest, wk)
            continue
        if wk == week:
            state = day_state if (day_state != "sun" or allow_hindsight) else "fri"
        else:
            # A fully past week: fully resolved either way; only actually
            # differs from "fri" when hindsight is asked for.
            state = "sun" if allow_hindsight else "fri"
        src = variants_dir / f"week-{wk:02d}-{state}.yml"
        if src.exists():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            _write_blank_week(dest, wk)

    base_path = demo_dir / "league.base.yml"
    standings_path = _standings_dir(demo_dir) / f"week-{week:02d}.yml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    base = base or {}
    standings = yaml.safe_load(standings_path.read_text(encoding="utf-8")) if standings_path.exists() else {}
    standings = standings or {}
    base["week"] = week
    base["my_team"] = f"team_{agent_slot}"
    base["my_opponent"] = standings.get("my_opponent", "")
    base["teams"] = standings.get("teams", [])
    text = "# GENERATED by scripts/demo_season.py -- do not hand-edit; re-run `goto` to update.\n" + yaml.safe_dump(base, sort_keys=False)
    (demo_dir / "league.yml").write_text(text, encoding="utf-8")


def cmd_goto(args: argparse.Namespace) -> int:
    demo_dir = _demo_dir(args.season)
    meta = _load_meta(demo_dir)
    cache_dir = Path(args.cache_dir)

    try:
        date = dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"error: {args.date!r} is not a YYYY-MM-DD date", file=sys.stderr)
        return 1

    bounds = _week_bounds(meta["season"], cache_dir, _default_opener)
    week, day_state = resolve_clock(date, bounds)
    if day_state == "sun" and not args.allow_hindsight:
        print(f"note: {date} is a Sunday -- clamping to the Friday state (pass --allow-hindsight for gameday hindsight)")

    _apply_clock(demo_dir, week=week, day_state=day_state, allow_hindsight=args.allow_hindsight)
    print(f"clock set to {date} -> week {week}, {day_state if (day_state != 'sun' or args.allow_hindsight) else 'fri'} state")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    demo_dir = _demo_dir(args.season)
    if not _meta_path(demo_dir).exists():
        print(f"error: {demo_dir} has no demo_meta.yml -- run `build` first", file=sys.stderr)
        return 1

    os.chdir(demo_dir)
    from scripts import gui as gui_module

    return gui_module.main(["--port", str(args.port), "--host", args.host])


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="materialize demo/<season>/")
    b.add_argument("--season", type=int, default=2025)
    b.add_argument("--seed", type=int, default=11)
    b.add_argument("--agent-slot", type=int, default=None, help="default: config.yml's draft.my_slot")
    b.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    b.set_defaults(func=cmd_build)

    g = sub.add_parser("goto", help="move the clock to a date")
    g.add_argument("date", help="YYYY-MM-DD")
    g.add_argument("--season", type=int, default=2025)
    g.add_argument("--allow-hindsight", action="store_true", help="allow a Sunday date to resolve to the (hindsight) sun state")
    g.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    g.set_defaults(func=cmd_goto)

    s = sub.add_parser("serve", help="serve the GUI against the demo season")
    s.add_argument("--season", type=int, default=2025)
    s.add_argument("--port", type=int, default=8322)
    s.add_argument("--host", default="127.0.0.1")
    s.set_defaults(func=cmd_serve)

    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
