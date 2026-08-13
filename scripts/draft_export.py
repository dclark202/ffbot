#!/usr/bin/env python3
"""Pre-draft safety net: export the board so Sleeper can draft for you.

There is no API for setting Sleeper's pre-draft rankings either — it's a
manual step in their web/app UI, same as Yahoo was. But it happens before
the clock starts, so it costs nothing on draft day, and it means Sleeper's
autopick follows *your* board if you're ever disconnected or away when your
pick comes up.

    python scripts/draft_export.py --board my_rankings.csv

Writes, by default, to draft/:
    board.csv          Rank,Player,Team,Pos,Bye,Proj,VOR,Tier,ADP
    board.txt           plain numbered list -- paste this into Sleeper's
                         pre-draft rankings, in order
    board_by_pos.txt    the same, split per position
    cheatsheet.txt       printable one-pager, grouped by position and tier

Optionally reconciles board names against Sleeper's real player ids, fetched
live (Sleeper's players dump needs no auth):

    python scripts/draft_export.py --board my_rankings.csv --reconcile

Or against a previously-saved dump (offline/reproducible runs, e.g. tests):

    python scripts/draft_export.py --board my_rankings.csv --sleeper-players-file dump.json

`--reconcile` is the only place this script touches the network — the rest
of it never imports `ffbot.sleeper` at all.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import Board, BoardPlayer, export_rankings, load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.names import MatchResult, match_board_to_platform  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml (default: config.yml)")
    p.add_argument("--board", action="append", default=None, help="FantasyPros CSV path (repeatable); overrides config.yml's draft.board_csv")
    p.add_argument("--out", default="draft", help="output directory (default: draft/)")
    p.add_argument("--reconcile", action="store_true", help="fetch Sleeper's players dump live and reconcile board names against it")
    p.add_argument("--sleeper-players-file", default=None, help="use a previously-saved Sleeper players JSON dump instead of a live fetch (implies --reconcile)")
    return p.parse_args(argv)


# --- Exports -------------------------------------------------------------


def _write_board_csv(ranked: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Rank", "Player", "Team", "Pos", "Bye", "Proj", "VOR", "Tier", "ADP"])
        for row in ranked:
            writer.writerow(
                [
                    row["rank"],
                    row["name"],
                    row["team"],
                    row["position"],
                    row["bye"] if row["bye"] is not None else "",
                    f"{row['points']:.1f}",
                    f"{row['vor']:.1f}",
                    row["tier"],
                    f"{row['adp']:.1f}" if row["adp"] is not None else "",
                ]
            )


def _write_board_txt(ranked: list[dict], path: Path) -> None:
    lines = []
    for i, row in enumerate(ranked, start=1):
        bye = row["bye"] if row["bye"] is not None else "-"
        lines.append(f"{row['rank']:>4}. {row['name']:<24} {row['position']:<4} {row['team']:<4} bye {bye:<3} tier {row['tier']}")
        if i % 10 == 0:
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_board_by_pos(board: Board, path: Path) -> None:
    by_pos: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in board.players:
        by_pos[bp.position].append(bp)

    lines = []
    for pos in sorted(by_pos):
        lines.append(f"=== {pos} ===")
        ordered = sorted(by_pos[pos], key=lambda b: -b.vor)
        for i, bp in enumerate(ordered, start=1):
            bye = bp.bye_week if bp.bye_week is not None else "-"
            lines.append(f"{i:>3}. {bp.name:<24} tier {bp.tier}  proj {bp.points:.1f}  vor {bp.vor:.1f}  bye {bye}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_cheatsheet(board: Board, path: Path) -> None:
    by_pos: dict[str, list[BoardPlayer]] = defaultdict(list)
    for bp in board.players:
        by_pos[bp.position].append(bp)

    lines = ["DRAFT CHEATSHEET", "=" * 60, ""]
    for pos in sorted(by_pos):
        lines.append(pos)
        by_tier: dict[int, list[str]] = defaultdict(list)
        for bp in sorted(by_pos[pos], key=lambda b: -b.vor):
            by_tier[bp.tier].append(bp.name)
        for tier in sorted(by_tier):
            lines.append(f"  T{tier}: {', '.join(by_tier[tier])}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_exports(board: Board, cfg: Config, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ranked = export_rankings(board, cfg)

    paths = {
        "board_csv": out_dir / "board.csv",
        "board_txt": out_dir / "board.txt",
        "board_by_pos": out_dir / "board_by_pos.txt",
        "cheatsheet": out_dir / "cheatsheet.txt",
    }
    _write_board_csv(ranked, paths["board_csv"])
    _write_board_txt(ranked, paths["board_txt"])
    _write_board_by_pos(board, paths["board_by_pos"])
    _write_cheatsheet(board, paths["cheatsheet"])
    return paths


# --- Name reconciliation --------------------------------------------------


def sleeper_players_to_rows(players: dict) -> list[dict]:
    """Sleeper's players dump (`{player_id: {...}}`, from
    `ffbot.sleeper.client.SleeperClient.players`) -> the
    `[{"player_id", "name", "position", "team"}, ...]` shape
    `names.match_board_to_platform` expects. Every entry is kept (not just
    fantasy-relevant positions) — a non-match is harmless, a silently
    dropped entry is not.
    """
    rows = []
    for player_id, p in players.items():
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue
        rows.append({
            "player_id": player_id,
            "name": name,
            "position": p.get("position") or "",
            "team": p.get("team") or "",
        })
    return rows


def reconcile_with_sleeper(
    board: Board, sleeper_players: list[dict], cfg: Config
) -> tuple[list[MatchResult], Counter]:
    """Match every board player against a Sleeper player list. Pre-draft only."""
    board_rows = [{"name": bp.name, "position": bp.position, "team": bp.team} for bp in board.players]
    results = match_board_to_platform(
        board_rows,
        sleeper_players,
        aliases=cfg.draft.aliases,
        fuzzy_threshold=cfg.draft.fuzzy_threshold,
        fuzzy_margin=cfg.draft.fuzzy_margin,
    )
    return results, Counter(r.confidence for r in results)


def write_reconciliation_report(results: list[MatchResult], summary: Counter, path: Path) -> None:
    total = sum(summary.values())
    lines = [
        f"NAME RECONCILIATION — {total} board players",
        f"  exact:    {summary.get('exact', 0)}",
        f"  position: {summary.get('position', 0)}",
        f"  initial:  {summary.get('initial', 0)}",
        f"  fuzzy:    {summary.get('fuzzy', 0)}  (verify these by hand)",
        f"  none:     {summary.get('none', 0)}  (unmatched)",
        "",
    ]

    fuzzy = [r for r in results if r.confidence == "fuzzy"]
    if fuzzy:
        lines.append("FUZZY MATCHES — verify each of these:")
        for r in fuzzy:
            lines.append(f"  {r.query!r} -> {r.matched_name!r}")
        lines.append("")

    unmatched = [r for r in results if r.confidence == "none"]
    if unmatched:
        lines.append(
            "UNMATCHED — if any of these are really the same player under a "
            "different name (nicknames, etc.), add them to draft.aliases in "
            "config.yml. Paste-ready stub:"
        )
        lines.append("aliases:")
        for r in unmatched:
            hint = f"  # candidates: {', '.join(r.alternatives)}" if r.alternatives else ""
            lines.append(f'  "{r.query}": ""{hint}')
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sleeper_id_map(board: Board, results: list[MatchResult]) -> dict[str, str]:
    """{board_key: sleeper_player_id} for every non-"none" match, in board order."""
    id_map: dict[str, str] = {}
    for bp, result in zip(board.players, results):
        if result.matched_id is not None:
            id_map[bp.key] = result.matched_id
    return id_map


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)

    try:
        board = load_board_from_config(cfg, args.board)
    except ValueError as exc:
        print(
            f"{exc}. Pass --board path/to/rankings.csv, or set draft.board_csv "
            "in config.yml.",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out)
    paths = write_exports(board, cfg, out_dir)
    print(f"Wrote {len(board.players)} players to {out_dir}/:")
    for p in paths.values():
        print(f"  {p}")

    if args.reconcile or args.sleeper_players_file:
        if args.sleeper_players_file:
            players = json.loads(Path(args.sleeper_players_file).read_text(encoding="utf-8"))
        else:
            # The only place this script touches the network -- imported
            # lazily so a bare `--board` run never needs ffbot.sleeper at all.
            from ffbot.sleeper.cache import SleeperFetchError
            from ffbot.sleeper.client import SleeperClient

            try:
                players = SleeperClient().players()
            except SleeperFetchError as exc:
                print(f"Could not fetch Sleeper's players dump: {exc}", file=sys.stderr)
                return 1

        sleeper_players = sleeper_players_to_rows(players)
        results, summary = reconcile_with_sleeper(board, sleeper_players, cfg)
        report_path = out_dir / "reconciliation.txt"
        write_reconciliation_report(results, summary, report_path)
        ids_path = out_dir / "sleeper_ids.json"
        ids_path.write_text(json.dumps(sleeper_id_map(board, results), indent=2), encoding="utf-8")
        print(f"\nName reconciliation: {dict(summary)}")
        print(f"  report: {report_path}")
        print(f"  sleeper ids: {ids_path}")

    print(
        "\nNext: open Sleeper's pre-draft rankings and enter board.txt in "
        "order (or board_by_pos.txt one position at a time). That's what "
        "Sleeper's autopick follows if you're ever disconnected or away when "
        "your pick comes up."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
