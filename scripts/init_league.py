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
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot.config import _deep_merge  # noqa: E402
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
    # Sleeper's `waiver_budget` is nonzero for a FAAB league (its own
    # `settings.waiver_type` is an undocumented integer code, not worth
    # depending on) — a real budget is the reliable signal.
    league_dict["waiver_type"] = "faab" if int(settings.get("waiver_budget", 0) or 0) > 0 else "rolling"

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

    print(
        "\nNext steps:\n"
        "  1. python scripts/scoring_check.py         # verify league.yml against a real board\n"
        "  2. Download the 5 FantasyPros CSVs into draft/ — see docs/DRAFT.md\n"
        "  3. python scripts/gui.py                    # http://127.0.0.1:8321/\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
