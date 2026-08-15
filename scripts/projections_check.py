#!/usr/bin/env python3
"""Coverage check for a live weekly-projection source, before trusting it
for a real report (see docs/REFERENCE.md's config.yml reference,
projection_source).

    python scripts/projections_check.py --season 2026 --week 1
    python scripts/projections_check.py --season 2026 --week 6 --refresh

Two independent checks, same spirit as `scripts/history_check.py`:

1. NAME-MATCH COVERAGE — every player on the frozen draft board (and, if
   `roster.yml` exists, every rostered player specifically) matched against
   the fetched source's rows via `names.match_board_to_platform` — the STRICT,
   human-reviewable cascade, never the permissive live-TUI `search_scored`
   matcher (same rule `ffbot.history.names.match_actuals` documents: a wrong
   silent match here is worse than a visible miss). Defenses match on team
   abbreviation via `names.defense_key`, which `match_board_to_platform`
   already applies internally. A miss doesn't mean the source lacks that
   player — it usually means a name-spelling mismatch (see
   `ffbot.history.names.TEAM_RELOCATIONS`'s own `"JAC"` entry, found this
   same way, for the shape of bug this catches).

2. POINTS SANITY — for players that DID match, the fetched source's
   league-scored weekly number vs. the frozen board's own
   `points / weeks_in_season` rescaled estimate. These are never expected to
   agree closely (one is a real weekly projection, the other a flat season
   average) — but a large SYSTEMATIC bias in one direction across the whole
   pool points at a stat-mapping or league-scoring bug, not a source
   disagreement.

Currently only the "sleeper" source is checkable this way; "board" and
"csv" have no separate fetch to compare against themselves.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import apply_league_scoring, load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.names import match_board_to_platform, normalize_name  # noqa: E402
from ffbot.projections import sleeper as sleeper_module  # noqa: E402
from ffbot.projections.cache import DEFAULT_CACHE_DIR, ProjectionFetchError  # noqa: E402
from ffbot.roster_source import load_roster_names  # noqa: E402

_LOW_COVERAGE_WARN_PCT = 95.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml")
    p.add_argument("--roster", default="roster.yml")
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--week", type=int, required=True)
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--weeks-in-season", type=int, default=17, help="for the board's own rescaling, matching week_report.py's default")
    p.add_argument("--refresh", action="store_true", help="bypass the TTL cache and force a fresh fetch")
    p.add_argument("--show-misses", type=int, default=15, help="how many unmatched names to print per section")
    return p.parse_args(argv)


def _provider_players(rows: list[dict]) -> list[dict]:
    """Fetched rows -> the `{player_id, name, position, team}` shape
    `names.match_board_to_platform` expects. `player_id` is synthetic (a plain
    index) — the fetched rows carry no numeric id of their own — same
    convention `ffbot.history.names.match_actuals` uses for the identical
    reason.
    """
    return [
        {"player_id": i, "name": r["name"], "position": r["position"], "team": r["team"]}
        for i, r in enumerate(rows, start=1)
    ]


def _report_coverage(label: str, targets: list[dict], provider_players: list[dict], show_misses: int) -> float:
    matches = match_board_to_platform(targets, provider_players)
    matched = [m for m in matches if m.matched_id is not None]
    pct = 100.0 * len(matched) / len(matches) if matches else 0.0
    print(f"\n{label}: {len(matched)}/{len(matches)} matched ({pct:.1f}%)")

    conf_counts = Counter(m.confidence for m in matches)
    non_exact = sum(n for conf, n in conf_counts.items() if conf not in ("exact", "none"))
    if non_exact:
        breakdown = ", ".join(f"{conf}={conf_counts[conf]}" for conf in ("position", "initial", "fuzzy") if conf_counts.get(conf))
        print(f"  ({non_exact} matched only approximately — {breakdown}; worth a human look)")

    unmatched = [m for m in matches if m.matched_id is None]
    if unmatched:
        print(f"  {len(unmatched)} unmatched (showing up to {show_misses}):")
        for m in unmatched[:show_misses]:
            hint = f" (closest: {m.alternatives[0]!r})" if m.alternatives else ""
            print(f"    {m.query!r}{hint}")

    if pct < _LOW_COVERAGE_WARN_PCT:
        print(f"  <-- below {_LOW_COVERAGE_WARN_PCT:.0f}% coverage; investigate before trusting this source for {label.lower()}")
    return pct


def _report_points_sanity(board_players, rows: list[dict], league, weeks_in_season: int) -> None:
    scored_rows = [dict(r) for r in rows]
    apply_league_scoring(scored_rows, league)
    by_key = {(normalize_name(r["name"]), r["position"]): r["points"] for r in scored_rows}

    diffs: list[float] = []
    for bp in board_players:
        live = by_key.get((normalize_name(bp.name), bp.position))
        if live is None:
            continue
        board_weekly = bp.points / max(1, weeks_in_season)
        diffs.append(live - board_weekly)

    if not diffs:
        print("\npoints sanity: no matched players to compare — skipping")
        return

    mean_diff = statistics.fmean(diffs)
    median_diff = statistics.median(diffs)
    print(f"\npoints sanity ({len(diffs)} matched players, live minus board/{weeks_in_season}):")
    print(f"  mean diff={mean_diff:+.2f}  median diff={median_diff:+.2f}")
    print("  (large diffs are expected -- a real weekly number vs. a flat season")
    print("   average; a big, consistently-signed mean is what would flag a bug)")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)

    try:
        board = load_board_from_config(cfg)
    except ValueError as exc:
        print(f"error: no draft board configured ({exc})", file=sys.stderr)
        return 1

    ttl_minutes = 0.0 if args.refresh else None
    try:
        rows = sleeper_module.fetch_weekly_rows(
            args.season, args.week, cache_dir=args.cache_dir, ttl_minutes=ttl_minutes,
        )
    except ProjectionFetchError as exc:
        print(f"error: sleeper fetch failed: {exc}", file=sys.stderr)
        return 1

    print(f"sleeper: {len(rows)} projected players, season {args.season} week {args.week}")
    provider_players = _provider_players(rows)

    board_rows = [{"name": bp.name, "position": bp.position, "team": bp.team} for bp in board.players]
    board_pct = _report_coverage("board pool", board_rows, provider_players, args.show_misses)

    roster_pct = None
    try:
        roster_names = load_roster_names(args.roster)
    except Exception:
        roster_names = []
    if roster_names:
        # Position is unknown from a bare roster name -- match on name+team
        # only by leaving position blank; match_board_to_platform degrades its
        # cascade gracefully (normalize_position("") stays "", which still
        # participates correctly in the exact/fuzzy name tiers).
        roster_by_key = {normalize_name(bp.name): bp for bp in board.players}
        roster_rows = []
        for name in roster_names:
            bp = roster_by_key.get(normalize_name(name))
            roster_rows.append({"name": name, "position": bp.position if bp else "", "team": bp.team if bp else ""})
        roster_pct = _report_coverage("your roster", roster_rows, provider_players, args.show_misses)

    _report_points_sanity(board.players, rows, cfg.league, args.weeks_in_season)

    worst = min(p for p in (board_pct, roster_pct) if p is not None)
    if worst < _LOW_COVERAGE_WARN_PCT:
        print("\nFAILED — see flagged coverage above.")
        return 1
    print("\nOK — coverage checks out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
