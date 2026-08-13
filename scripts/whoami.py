#!/usr/bin/env python3
"""Discover your Sleeper league — league_id, your roster_id, the roster
slot layout, and the league's real scoring rules.

No credentials needed at all: Sleeper's read API is public and unauthenticated,
so this is a one-shot lookup, not an authorization flow (there is no Sleeper
equivalent of `scripts/authorize.py` — that whole step is gone).

    python scripts/whoami.py --username <your-sleeper-username>
    python scripts/whoami.py --username <your-sleeper-username> --season 2026

Paste the printed `league_id` into `config.yml`'s `sleeper:` block (and
`roster_id`, if you want to skip the one extra lookup `roster_id: null`
costs each run). `roster_positions` and the scoring settings are printed for
reference — this script never writes `config.yml`/`league.yml` itself (see
CLAUDE.md: `config.yml` is hand-narrated and never machine-rewritten).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.sleeper.client import SleeperClient  # noqa: E402
from ffbot.sleeper.cache import SleeperFetchError  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--username", required=True, help="your Sleeper username")
    p.add_argument("--season", type=int, default=None, help="season year (default: current season per Sleeper's own /state/nfl)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = SleeperClient()

    try:
        state = client.nfl_state()
    except SleeperFetchError as e:
        print(f"Could not reach Sleeper: {e}", file=sys.stderr)
        return 1
    season = args.season if args.season is not None else int(state["season"])
    print(f"Sleeper season: {season} (week {state.get('week')}, {state.get('season_type')})\n")

    user = client.user(args.username)
    if user is None:
        print(f"No Sleeper user found for username {args.username!r}.", file=sys.stderr)
        return 1
    user_id = user["user_id"]
    print(f"User   : {user.get('display_name', args.username)}  (user_id {user_id})")

    try:
        leagues = client.user_leagues(user_id, season)
    except SleeperFetchError as e:
        print(f"Could not list leagues: {e}", file=sys.stderr)
        return 1

    if not leagues:
        print(
            f"\nNo {season} leagues found for this user. If your draft hasn't "
            "happened yet, Sleeper may not list the league until it's created "
            "for the season — try again closer to draft day, or pass --season "
            "to check a different year.",
            file=sys.stderr,
        )
        return 1

    for league in leagues:
        league_id = league["league_id"]
        print("=" * 60)
        print(f"League : {league.get('name', '?')}  ({league_id})")
        print(f"Status : {league.get('status', '?')}")

        roster_positions = league.get("roster_positions") or []
        print(f"Slots  : {roster_positions}")

        settings = league.get("settings") or {}
        if "waiver_budget" in settings:
            print(f"FAAB budget: {settings['waiver_budget']}")
        print(f"Waiver type: {settings.get('waiver_type', '?')}")

        try:
            rosters = client.rosters(league_id)
        except SleeperFetchError as e:
            print(f"\nRosters unavailable: {e}")
            continue

        mine = next((r for r in rosters if r.get("owner_id") == user_id), None)
        if mine is None:
            print("\nCould not find your roster in this league (co-owned team? "
                  "check league_users below manually).")
        else:
            print(f"\nYour roster_id: {mine['roster_id']}")
            print(f"Players on roster: {len(mine.get('players') or [])}")

        print("\nPut these in config.yml's sleeper: block:")
        print(f"    league_id: \"{league_id}\"")
        print(f"    username: \"{args.username}\"")
        if mine is not None:
            print(f"    roster_id: {mine['roster_id']}")

        scoring = league.get("scoring_settings") or {}
        if scoring:
            print(f"\nScoring settings ({len(scoring)} keys) — see league.yml for "
                  "how this repo represents league scoring; this is Sleeper's raw "
                  "form, not yet translated:")
            for key in sorted(scoring):
                val = scoring[key]
                if val:
                    print(f"    {key}: {val}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
