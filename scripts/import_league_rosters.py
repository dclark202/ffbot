#!/usr/bin/env python3
"""Import all 12 teams' rosters, so the tool finally knows the other 11
teams exist — `week.waiver_candidates` has always built its free-agent
pool as *board minus your own roster*, which means it will happily
recommend adding a player who is already on someone else's roster. This
fixes that, and is also step one toward tactical denial (a rival's real
roster is what makes "is this player worth denying them" a computable
number instead of a guess — see `week.hold_margin` and, eventually,
`draft.need` run against a rival's roster).

    python scripts/import_league_rosters.py --paste rosters.txt

Input format — delimited text, one block per team:

    == Rival Team Name ==
    Player One
    Player Two

    == Another Team ==
    Player Three
    ...

A league page's raw copy-paste output varies by browser/zoom/columns shown
and isn't reliably parseable sight unseen — reshape it into the delimited
format above (by hand, or by asking Claude Code to clean it up from the
page) before running this script. `--paste` is the manual import route;
Sleeper's `rosters`/`users` endpoints (`ffbot.sleeper.client.SleeperClient`)
are a live, automatic route that writes the identical output file — see
`--live` below, which needs only `sleeper.league_id` configured, no pasting.

Every name is matched against the draft board via the same strict,
human-reviewed cascade `names.match_board_to_platform` uses for pre-draft ID
reconciliation — exact match, then position, then a fuzzy match that must
beat the runner-up by a real margin, NEVER a silent guess. Unmatched names
are written to `unmatched:` in the output file, never silently dropped —
a rival's star quietly vanishing from the exclusion set is the dangerous
failure mode here, the same reasoning `roster_source.py` and `intel.py`
already apply to your own roster.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ffbot.board import Board, load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.league_rosters import build_teams_from_sleeper  # noqa: E402
from ffbot.names import match_board_to_platform, normalize_name, search_scored  # noqa: E402

_TEAM_HEADER_RE = re.compile(r"^==\s*(.+?)\s*==$")


class RosterImportError(ValueError):
    """The paste dump could not be understood."""


def parse_team_blocks(text: str) -> dict[str, list[str]]:
    """Split the delimited paste format into `{team name: [display names]}`.

    Order is preserved and duplicate team headers merge (append), so a
    paste accidentally split across two copy operations still works.
    """
    teams: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _TEAM_HEADER_RE.match(line)
        if m:
            current = m.group(1).strip()
            teams.setdefault(current, [])
            continue
        if current is None:
            raise RosterImportError(
                f"line before any '== Team Name ==' header: {line!r} — "
                "see this script's module docstring for the expected format"
            )
        teams[current].append(line)
    if not teams:
        raise RosterImportError("no '== Team Name ==' headers found in the paste")
    return teams


class _BoardSearchable:
    """Adapts a BoardPlayer to names.search_scored's Searchable protocol."""

    __slots__ = ("key", "name", "player")

    def __init__(self, bp):
        self.name = bp.name
        self.key = normalize_name(bp.name)
        self.player = bp


def match_rosters(
    teams: dict[str, list[str]], board: Board, cfg: Config
) -> tuple[dict[str, list[str]], list[str]]:
    """Match every pasted name against the board.

    Returns `({team: [matched board display names]}, [unmatched entries])`.
    Reuses `names.match_board_to_platform`'s cascade by treating the pasted
    names as the "platform player" side of the reconciliation it was built
    for — see the module docstring for why that matcher, not the permissive
    TUI one, is the right tool for a bulk one-time import like this.
    """
    board_rows = [{"name": bp.name, "position": bp.position, "team": bp.team} for bp in board.players]

    pasted_players: list[dict] = []
    id_to_team: dict[int, str] = {}
    uid = 1
    for team, names in teams.items():
        for name in names:
            pasted_players.append({"player_id": uid, "name": name, "position": "", "team": ""})
            id_to_team[uid] = team
            uid += 1

    results = match_board_to_platform(
        board_rows, pasted_players,
        aliases=cfg.draft.aliases,
        fuzzy_threshold=cfg.draft.fuzzy_threshold,
        fuzzy_margin=cfg.draft.fuzzy_margin,
    )

    # match_board_to_platform returns one MatchResult per BOARD row (it
    # answers "which pasted player is this board player"), so invert it into
    # "which board player is this pasted id" for our purposes.
    matched_display_name: dict[int, str] = {}
    for board_row, result in zip(board_rows, results):
        if result.matched_id is not None and result.matched_id not in matched_display_name:
            matched_display_name[result.matched_id] = board_row["name"]

    searchable = [_BoardSearchable(bp) for bp in board.players]
    team_matches: dict[str, list[str]] = {t: [] for t in teams}
    unmatched: list[str] = []
    for pp in pasted_players:
        uid = pp["player_id"]
        team = id_to_team[uid]
        display = matched_display_name.get(uid)
        if display is not None:
            team_matches[team].append(display)
            continue
        hits = search_scored(pp["name"], searchable)
        hint = f" (did you mean {hits[0][1].name!r}?)" if hits else ""
        unmatched.append(f"{team}: {pp['name']!r}{hint}")

    return team_matches, unmatched


def write_league_rosters(
    path: str | Path, week_num: int | None, teams: dict[str, list[str]], unmatched: list[str], source: str
) -> None:
    payload = {
        "week": week_num,
        "generated": datetime.date.today().isoformat(),
        "source": source,
        "teams": teams,
        "unmatched": unmatched,
    }
    Path(path).write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paste", default=None, help="path to the delimited team-blocks text file")
    ap.add_argument("--live", action="store_true", help="fetch live from Sleeper (needs sleeper.league_id in config.yml) instead of --paste")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--out", default="league_rosters.yml")
    ap.add_argument("--week", type=int, default=None, help="current NFL week, for the output file's record only")
    args = ap.parse_args(argv)

    if bool(args.paste) == bool(args.live):
        print("error: pass exactly one of --paste <file> or --live", file=sys.stderr)
        return 1

    cfg = Config.load(args.config)

    if args.live:
        if not cfg.sleeper.league_id:
            print("error: --live needs sleeper.league_id set in config.yml", file=sys.stderr)
            return 1
        from ffbot.sleeper.cache import SleeperFetchError
        from ffbot.sleeper.client import SleeperClient

        client = SleeperClient()
        try:
            rosters = client.rosters(cfg.sleeper.league_id)
            league_users = client.league_users(cfg.sleeper.league_id)
            players = client.players()
        except SleeperFetchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        team_matches, unmatched = build_teams_from_sleeper(rosters, league_users, players)
        source = "api"
    else:
        try:
            board = load_board_from_config(cfg)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        text = Path(args.paste).read_text(encoding="utf-8")
        try:
            teams = parse_team_blocks(text)
        except RosterImportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        team_matches, unmatched = match_rosters(teams, board, cfg)
        source = "paste"

    write_league_rosters(args.out, args.week, team_matches, unmatched, source=source)

    total = sum(len(v) for v in team_matches.values())
    print(f"Wrote {args.out}: {len(team_matches)} teams, {total} matched players.")
    for team, names in team_matches.items():
        print(f"  {team}: {len(names)}")
    if unmatched:
        print(f"\n{len(unmatched)} unmatched — reported, not silently dropped:", file=sys.stderr)
        for u in unmatched:
            print(f"  {u}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
