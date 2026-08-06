#!/usr/bin/env python3
"""Prove the Yahoo connection works, end to end.

Run this straight after authorizing. It exercises the whole read path — token
refresh, league discovery, your team key, roster, and FAAB balance — so any
break is isolated here rather than at 12:45 on a Sunday.

    python scripts/whoami.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yahoo_fantasy_api as yfa  # noqa: E402

from ffbot.auth import AuthError, YahooSession, env_file_persister  # noqa: E402


def flatten_positions(positions: dict) -> dict[str, int]:
    """Yahoo nests the count: {'QB': {'count': 1, 'position_type': 'O'}}.

    The optimizer wants {'QB': 1}, so this is the adapter between them.
    """
    return {slot: int(meta.get("count", 0)) for slot, meta in positions.items()}


def main() -> int:
    try:
        sc = YahooSession.from_env(on_refresh=env_file_persister(".env"))
    except AuthError as e:
        print(f"Auth failed: {e}", file=sys.stderr)
        print("\nRun `python scripts/authorize.py` first.", file=sys.stderr)
        return 1

    print("Token OK.\n")

    gm = yfa.Game(sc, "nfl")
    try:
        league_ids = gm.league_ids()
    except Exception as e:
        print(f"Could not list leagues: {e}", file=sys.stderr)
        print(
            "\nIf this is a 401/403, your Yahoo app probably lacks the Fantasy "
            "Sports scope — see SETUP.md.",
            file=sys.stderr,
        )
        return 1

    if not league_ids:
        print(
            "No leagues found for this Yahoo account.\n"
            "Most likely you authorized with a different Yahoo login than the "
            "one that owns your team. Re-run scripts/authorize.py and sign in "
            "as the team owner.",
            file=sys.stderr,
        )
        return 1

    for league_id in league_ids:
        lg = gm.to_league(league_id)
        print("=" * 60)
        try:
            settings = lg.settings()
            print(f"League : {settings.get('name', '?')}  ({league_id})")
        except Exception:
            print(f"League : {league_id}")

        try:
            print(f"Week   : {lg.current_week()}")
        except Exception:
            pass

        try:
            print(f"Slots  : {flatten_positions(lg.positions())}")
        except Exception as e:
            print(f"Slots  : unavailable ({e})")

        try:
            team_key = lg.team_key()
        except Exception as e:
            print(f"Your team: could not resolve ({e})")
            continue

        print(f"Team   : {team_key}")
        print("\nPut these in config.yml:")
        print(f"    league_id: \"{league_id}\"")
        print(f"    team_key: \"{team_key}\"")

        try:
            info = lg.teams().get(team_key, {})
            if "faab_balance" in info:
                print(f"\nFAAB balance: {info['faab_balance']}")
        except Exception:
            pass

        try:
            roster = lg.to_team(team_key).roster()
        except Exception as e:
            print(f"\nRoster unavailable: {e}")
            continue

        print(f"\nRoster ({len(roster)}):")
        for p in roster:
            status = f"  [{p['status']}]" if p.get("status") else ""
            print(
                f"  {p.get('selected_position', '?'):<7} "
                f"{p.get('name', '?'):<26} "
                f"{'/'.join(p.get('eligible_positions', []))}{status}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
