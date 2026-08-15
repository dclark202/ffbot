#!/usr/bin/env python3
"""Bootstrap a fresh clone against a real Sleeper league in one command —
the automated replacement for hand-transcribing `league.yml` off Sleeper's
League Settings page and pasting `league_id`/`roster_id` from
`scripts/whoami.py`'s printout.

    python scripts/init_league.py --username <your-sleeper-username>
    python scripts/init_league.py --username <you> --season 2026 --force

No credentials needed — same public, unauthenticated `SleeperClient` calls
`scripts/whoami.py` already makes. This script does everything that one
does, plus:

- writes `config.local.yml` (never `config.yml` — see CLAUDE.md: that file
  is hand-narrated and never machine-rewritten; this is the exact overlay
  `scripts/gui.py`'s settings page already writes to)
- writes `league.yml` from Sleeper's real `scoring_settings`
  (`ffbot.sleeper.scoring_import`), instead of a manual transcription
- copies `roster.example.yml` -> `roster.yml` if you don't have one yet

`league.yml`/`roster.yml` are only written if missing, unless `--force`.
Every field this script cannot translate from Sleeper's scoring is printed,
never silently dropped — see `ffbot.sleeper.scoring_import`'s own docstring.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot.config import Config, _deep_merge  # noqa: E402
from ffbot.sleeper.cache import SleeperFetchError  # noqa: E402
from ffbot.sleeper.client import SleeperClient  # noqa: E402
from ffbot.sleeper.scoring_import import (  # noqa: E402
    league_dict_from_sleeper_scoring,
    roster_positions_from_sleeper,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--username", required=True, help="your Sleeper username")
    p.add_argument("--season", type=int, default=None, help="season year (default: current per Sleeper's /state/nfl)")
    p.add_argument("--league-id", default=None, help="pick a specific league if --username is in more than one")
    p.add_argument("--force", action="store_true", help="overwrite an existing league.yml/roster.yml")
    p.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    p.add_argument("--config-dir", default=str(_REPO_ROOT), help=argparse.SUPPRESS)  # test seam
    return p.parse_args(argv)


def _select_league(leagues: list[dict], league_id: str | None) -> dict | None:
    if league_id:
        return next((lg for lg in leagues if lg.get("league_id") == league_id), None)
    if len(leagues) == 1:
        return leagues[0]
    return None


def _write_yaml(path: Path, data: dict, header: str, dry_run: bool) -> None:
    text = header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if dry_run:
        print(f"--- would write {path} ---\n{text}")
        return
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")


def _write_config_local(config_dir: Path, sleeper_block: dict, dry_run: bool) -> None:
    """Deep-merge `{"sleeper": sleeper_block}` onto config.local.yml — the
    exact overlay pattern `scripts/gui.py`'s settings POST handler uses, so
    a config.local.yml already carrying other GUI-set overrides is
    preserved, not clobbered."""
    overlay_path = config_dir / "config.local.yml"
    existing: dict = {}
    if overlay_path.exists():
        existing = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, {"sleeper": sleeper_block})
    if dry_run:
        print(f"--- would write {overlay_path} ---\n{yaml.safe_dump(merged, sort_keys=False)}")
        return
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    print(f"wrote {overlay_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_dir = Path(args.config_dir)
    client = SleeperClient()

    try:
        state = client.nfl_state()
    except SleeperFetchError as e:
        print(f"Could not reach Sleeper: {e}", file=sys.stderr)
        return 1
    season = args.season if args.season is not None else int(state["season"])

    user = client.user(args.username)
    if user is None:
        print(f"No Sleeper user found for username {args.username!r}.", file=sys.stderr)
        return 1
    user_id = user["user_id"]

    try:
        leagues = client.user_leagues(user_id, season)
    except SleeperFetchError as e:
        print(f"Could not list leagues: {e}", file=sys.stderr)
        return 1
    if not leagues:
        print(f"No {season} leagues found for {args.username!r}. Pass --season for a different year.", file=sys.stderr)
        return 1

    league = _select_league(leagues, args.league_id)
    if league is None:
        print(f"Found {len(leagues)} leagues for {season} — pass --league-id to pick one:", file=sys.stderr)
        for lg in leagues:
            print(f"    {lg.get('league_id')}  {lg.get('name', '?')}", file=sys.stderr)
        return 1

    league_id = league["league_id"]
    print(f"League : {league.get('name', '?')}  ({league_id})")

    try:
        rosters = client.rosters(league_id)
    except SleeperFetchError as e:
        print(f"Could not fetch rosters: {e}", file=sys.stderr)
        rosters = []
    mine = next((r for r in rosters if r.get("owner_id") == user_id), None)
    roster_id = mine["roster_id"] if mine else None
    if roster_id is None:
        print("Could not find your roster in this league — roster_id left unset "
              "(it will be resolved by username at run time, one extra lookup per run).")

    sleeper_block = {"league_id": league_id, "username": args.username}
    if roster_id is not None:
        sleeper_block["roster_id"] = roster_id
    _write_config_local(config_dir, sleeper_block, args.dry_run)

    settings = league.get("settings") or {}
    scoring = league.get("scoring_settings") or {}
    league_dict, unmapped = league_dict_from_sleeper_scoring(
        scoring, name=league.get("name", ""), source=f"Sleeper league {league_id}, imported by scripts/init_league.py"
    )
    league_dict["games_per_season"] = 17
    league_dict["regular_season_weeks"] = int(settings.get("playoff_week_start", 15)) - 1
    league_dict["playoff_teams"] = int(settings.get("playoff_teams", 0))
    # ffbot only models rolling-priority waivers (no FAAB support) — nothing
    # to write into league.yml for this, but warn if the real Sleeper league
    # looks like a FAAB league (a nonzero `waiver_budget`; `settings.
    # waiver_type` itself is an undocumented integer code, not worth
    # depending on) so the mismatch is visible up front rather than
    # discovered the first time a claim's priority cost looks wrong.
    if int(settings.get("waiver_budget", 0) or 0) > 0:
        print(
            "\nNOTE: this Sleeper league appears to run FAAB waivers "
            f"(waiver_budget={settings.get('waiver_budget')}) — ffbot only "
            "models rolling-priority waivers, so waiver claim-cost sizing "
            "here won't reflect how your league actually spends budget."
        )

    if unmapped:
        print(f"\n{len(unmapped)} Sleeper scoring key(s) have no league.yml equivalent yet — "
              "hand-edit league.yml to add them if your league actually uses them:")
        for key in unmapped:
            print(f"    {key}: {scoring[key]}")

    league_path = config_dir / "league.yml"
    if league_path.exists() and not args.force and not args.dry_run:
        print(f"\n{league_path} already exists — leaving it alone (pass --force to overwrite).")
    else:
        header = (
            "# GENERATED by scripts/init_league.py from Sleeper's live scoring_settings.\n"
            "# Verify it with `python scripts/scoring_check.py` before trusting it for a\n"
            "# real draft or week — some values (fg_distance_mix, points_allowed_stdev)\n"
            "# are carried-forward estimates this repo has always shipped, not read from\n"
            "# Sleeper, since Sleeper has no equivalent field. See league.example.yml for\n"
            "# what every field means.\n"
        )
        _write_yaml(league_path, league_dict, header, args.dry_run)

    roster_positions_list = league.get("roster_positions") or []
    reserve_slots = int(settings.get("reserve_slots", 0) or 0)
    roster_positions = roster_positions_from_sleeper(roster_positions_list, reserve_slots)
    print(f"\nroster_positions (paste into config.yml if it differs from the default there):\n    {roster_positions}")

    roster_yml_path = config_dir / "roster.yml"
    example_path = config_dir / "roster.example.yml"
    if roster_yml_path.exists() and not args.force:
        print(f"{roster_yml_path} already exists — leaving it alone (pass --force to overwrite).")
    elif example_path.exists():
        if args.dry_run:
            print(f"--- would copy {example_path} -> {roster_yml_path} ---")
        else:
            shutil.copy(example_path, roster_yml_path)
            print(f"wrote {roster_yml_path} (from roster.example.yml — optional under roster_source: sleeper, "
                  "used only as a per-player flag overlay there)")

    # --sync (both scripts/draft.py and scripts/gui.py default it on now)
    # needs two things: sleeper.league_id, already written above, and
    # draft/sleeper_ids.json — the board-key -> Sleeper player id map that
    # scripts/draft_export.py --reconcile produces. Build that map here too
    # so sync is live the moment this script finishes, not just after a
    # second command the user has to remember to run.
    #
    # On a first-ever bootstrap there is no board yet (`draft.board_csv`'s
    # FantasyPros exports are a manual download, step 2 below) — that's the
    # common case, not an error, so a missing/unloadable board just skips
    # this step silently and the reminder below still explains how to
    # finish it later, once a board exists.
    sync_ready = False
    if not args.dry_run:
        try:
            from ffbot.board import load_board_from_config
            # SleeperClient/SleeperFetchError reuse this module's own
            # top-level imports (not a fresh import under a different
            # name) so a test that monkeypatches `init_league.SleeperClient`
            # -- see tests/test_init_league.py's `_patch_client` -- covers
            # this call too, the same as every other one in this script.
            from scripts.draft_export import (
                reconcile_with_sleeper,
                sleeper_id_map,
                sleeper_players_to_rows,
                write_reconciliation_report,
            )

            cfg_with_board = Config.load(str(config_dir / "config.yml"))
            board = load_board_from_config(cfg_with_board, None)
        except (ValueError, ImportError):
            board = None  # no board configured yet -- expected on a fresh clone
        if board is not None:
            try:
                sleeper_players = sleeper_players_to_rows(client.players())
            except SleeperFetchError as exc:
                print(f"\nCould not fetch Sleeper's players dump for --sync setup ({exc}) — "
                      "run `scripts/draft_export.py --reconcile` yourself once Sleeper is reachable.")
            else:
                results, summary = reconcile_with_sleeper(board, sleeper_players, cfg_with_board)
                out_dir = config_dir / "draft"
                out_dir.mkdir(parents=True, exist_ok=True)
                write_reconciliation_report(results, summary, out_dir / "reconciliation.txt")
                (out_dir / "sleeper_ids.json").write_text(
                    json.dumps(sleeper_id_map(board, results), indent=2), encoding="utf-8"
                )
                print(f"\nWrote {out_dir / 'sleeper_ids.json'} ({dict(summary)}) — --sync will be live.")
                sync_ready = True

    print(
        "\nNext steps:\n"
        "  1. python scripts/scoring_check.py         # verify league.yml against a real board\n"
        "  2. python scripts/gui.py                    # http://127.0.0.1:8321/ -- works now\n"
        "  3. (recommended) Download the 5 FantasyPros CSVs into draft/ -- see the README's\n"
        "     step 2 -- to unlock the draft room and waiver-add valuation\n"
        + ("" if sync_ready else
           "\n--sync needs draft/sleeper_ids.json: once you have a board (step 3 above), "
           "re-run this script or run `python scripts/draft_export.py --reconcile` directly.\n")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
