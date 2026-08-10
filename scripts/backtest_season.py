#!/usr/bin/env python3
"""Full-season replay: draft, then week-by-week lineup + waiver management
for one manager under the AGENT policy vs. the CONTROL policy, over a real
head-to-head schedule against 11 static opponents (see docs/BACKTEST.md,
milestone B4).

    python scripts/backtest_season.py --seasons 2023 --seeds 3
    python scripts/backtest_season.py --seasons 2021-2024 --seeds 5 --agent-slot 7

For each `(season, seed)`: builds the historical draft board
(`ffbot.history.board.historical_board`), drafts `--num-teams` managers (the
agent slot via `draft.recommend()`, everyone else by noisy ADP —
`ffbot.backtest.draft_sim`), then replays the full regular season TWICE from
that identical starting roster — once under the AGENT policy (spice-adjusted
lineups and waiver adds), once under CONTROL (frozen projections) — against
the IDENTICAL static opponents and schedule (`ffbot.backtest.schedule`).
Reports:

  1. Season-long points delta (agent total - control total), pooled across
     every `(season, seed)` with a block-bootstrap CI (blocked by season).
  2. Win-rate delta for the one manager under test, same bootstrap.

This is meaningfully slower than `scripts/backtest_lineup.py` — a full
season replay does real work every week (`waiver_candidates`, actuals,
projections) for every manager, not a cheap per-decision comparison. Expect
roughly 1-2 minutes per `(season, seed)` pair; size `--seeds` accordingly.
Requires the historical data already cached — see `scripts/history_fetch.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.draft_sim import simulate_draft  # noqa: E402
from ffbot.backtest.metrics import block_bootstrap_mean_ci, paired_win_rate_deltas  # noqa: E402
from ffbot.backtest.schedule import round_robin_schedule, score_schedule  # noqa: E402
from ffbot.backtest.season import simulate_season, static_opponent_points  # noqa: E402
from ffbot.config import Config, SeasonConfig  # noqa: E402
from ffbot.history.board import historical_board  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.openmeteo import open_meteo_game_weather  # noqa: E402
from ffbot.history.signals import combine_providers, historical_form, usage_form  # noqa: E402

SIGNAL_PROVIDERS = {"historical_form": historical_form, "usage_form": usage_form}
GAME_PROVIDERS = {"openmeteo": open_meteo_game_weather}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2023" or "2021-2024"')
    p.add_argument("--weeks", default="1-15", help="(default: %(default)s)")
    p.add_argument("--seeds", type=int, default=3, help="independent drafts per season (default: %(default)s)")
    p.add_argument("--seed-start", type=int, default=11, help="(default: %(default)s)")
    p.add_argument("--num-teams", type=int, default=12, help="(default: %(default)s)")
    p.add_argument("--agent-slot", type=int, default=4, help="1-indexed draft slot under test (default: %(default)s)")
    p.add_argument("--source", choices=["naive", "ecr"], default="ecr", help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--spice-level", type=int, default=None, help="override config.yml's season.spice_level (1-5)")
    p.add_argument(
        "--signals", default=None, metavar="NAME[,NAME...]",
        help=f"comma-separated signal provider(s) to merge in (choices: {sorted(SIGNAL_PROVIDERS)})",
    )
    p.add_argument(
        "--game-weather", choices=sorted(GAME_PROVIDERS), default=None,
        help="game-level weather enrichment (see ffbot/history/openmeteo.py)",
    )
    return p.parse_args(argv)


def _build_signal_provider(names: list[str]):
    if not names:
        return None
    if len(names) == 1:
        return SIGNAL_PROVIDERS[names[0]]
    return combine_providers(*(SIGNAL_PROVIDERS[n] for n in names))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    weeks = parse_seasons(args.weeks)
    if not seasons or not weeks:
        print("error: --seasons/--weeks produced no values", file=sys.stderr)
        return 1

    cfg = Config.load(args.config)
    cfg.draft.num_teams = args.num_teams
    cfg.draft.rounds = max(cfg.draft.rounds, 15)
    if args.spice_level is not None:
        cfg.season = SeasonConfig.from_spice_level(args.spice_level)

    signal_names = [s.strip() for s in args.signals.split(",") if s.strip()] if args.signals else []
    unknown = [n for n in signal_names if n not in SIGNAL_PROVIDERS]
    if unknown:
        print(f"error: unknown --signals {unknown} (choices: {sorted(SIGNAL_PROVIDERS)})", file=sys.stderr)
        return 1
    signal_provider = _build_signal_provider(signal_names)
    game_provider = GAME_PROVIDERS[args.game_weather] if args.game_weather else None

    points_deltas: list[float] = []
    points_blocks: list[tuple[int, int]] = []
    win_rate_deltas: list[float] = []
    win_rate_blocks: list[tuple[int, int]] = []

    for season in seasons:
        print(f"season {season}: building historical board...", file=sys.stderr)
        try:
            board = historical_board(season, cfg, num_teams=args.num_teams, cache_dir=args.cache_dir)
        except ValueError as exc:
            print(f"  skipping {season}: {exc}", file=sys.stderr)
            continue

        schedule = round_robin_schedule(args.num_teams, len(weeks))

        for i in range(args.seeds):
            seed = args.seed_start + i
            print(f"season {season} seed {seed}: drafting + replaying...", file=sys.stderr)
            draft = simulate_draft(
                board, cfg, num_teams=args.num_teams, rounds=cfg.draft.rounds,
                agent_slot=args.agent_slot, seed=seed, agent_uses_recommend=True,
            )

            opp_points: dict[int, dict[int, float]] = {}
            for slot in range(1, args.num_teams + 1):
                if slot == args.agent_slot:
                    continue
                opp_points[slot] = static_opponent_points(
                    season, cfg, board, draft, slot, args.source, weeks, cache_dir=args.cache_dir,
                )

            agent = simulate_season(
                season, cfg, board, draft, args.agent_slot, "agent", args.source, weeks,
                cache_dir=args.cache_dir, schedule=schedule,
                signal_provider=signal_provider, game_provider=game_provider,
            )
            control = simulate_season(
                season, cfg, board, draft, args.agent_slot, "control", args.source, weeks,
                cache_dir=args.cache_dir, schedule=schedule,
                signal_provider=signal_provider, game_provider=game_provider,
            )

            def _team_week_points(agent_points: dict[int, float]) -> dict[tuple[int, int], float]:
                d = {(slot, w): pts for slot, wk_pts in opp_points.items() for w, pts in wk_pts.items()}
                d.update({(args.agent_slot, w): pts for w, pts in agent_points.items()})
                return d

            recs_agent = score_schedule(schedule, _team_week_points(agent.points_by_week))
            recs_control = score_schedule(schedule, _team_week_points(control.points_by_week))

            points_deltas.append(sum(agent.points_by_week.values()) - sum(control.points_by_week.values()))
            points_blocks.append((season, 0))

            wr_a = {t: r.win_rate for t, r in recs_agent.items()}
            wr_c = {t: r.win_rate for t, r in recs_control.items()}
            delta = paired_win_rate_deltas(wr_a, wr_c).get(args.agent_slot)
            if delta is not None:
                win_rate_deltas.append(delta)
                win_rate_blocks.append((season, 0))

    n = len(points_deltas)
    print(f"\n{n} (season, seed) full-season replays completed.\n")
    if n == 0:
        print("nothing to report", file=sys.stderr)
        return 1

    pm, plo, phi = block_bootstrap_mean_ci(points_deltas, points_blocks, seed=args.seed_start)
    print(f"Season points delta (agent - control): mean={pm:+.1f} pts, 95% CI [{plo:+.1f}, {phi:+.1f}]")

    if win_rate_deltas:
        wm, wlo, whi = block_bootstrap_mean_ci(win_rate_deltas, win_rate_blocks, seed=args.seed_start)
        print(
            f"Win-rate delta (agent - control), the one manager under test: "
            f"mean={wm:+.3f}, 95% CI [{wlo:+.3f}, {whi:+.3f}]"
        )
    else:
        print("Win-rate delta: no season completed a full schedule for the agent slot.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
