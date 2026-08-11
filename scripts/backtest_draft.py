#!/usr/bin/env python3
"""Grade the DRAFT-side spice ladder against real NFL history (B5) --
simulate N synthetic snake drafts per season, agent edge-weights vs. a
control, and score the resulting roster by REALIZED season points under a
weekly-OPTIMAL ("oracle") lineup. Reports a paired block-bootstrap delta.

    python scripts/backtest_draft.py --seasons 2021-2023 --seeds 30 --agent-spice-level 4
    python scripts/backtest_draft.py --seasons 2024 --seeds 50 \\
        --agent-spice-level 3 --control-spice-level 1

Why not scripts/backtest_season.py? That grader mixes THREE sources of
noise into one win-rate number: draft quality, weekly lineup-setting, and
schedule luck -- B6 found 12 full replays gave a points delta CI of
+/-35 pts, wide enough to make the comparison uninformative (see
docs/BACKTEST.md). This script isolates draft quality alone: agent- and
control-drafted rosters are scored under the IDENTICAL policy (the
objectively best legal lineup that exact roster could have started each
week, real pre-game status respected, final score known -- the same
"oracle" definition `ffbot.backtest.baselines` uses) -- the only thing
that differs between a paired draft is which players got drafted, since
`ffbot.backtest.draft_sim.simulate_draft` reuses the identical seed (and
therefore the identical noisy-ADP opponent order) for both. `--seeds`
draws are nearly free once a season's `week_actuals`/`as_of` calls are
made (`optimize()` is ~66us against a 15-player roster), so raising it
costs little relative to adding more grid cells.

Only ECR_CLEAN_SEASONS (2021-2024) have a cached preseason board
(`ffbot.history.board.historical_board`) and FFC ADP -- see
docs/BACKTEST.md's open question on that board's shallower player
coverage relative to the in-season ECR-derived one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.draft_sim import agent_roster, simulate_draft  # noqa: E402
from ffbot.backtest.metrics import block_bootstrap_mean_ci  # noqa: E402
from ffbot.config import Config, DraftConfig, LeagueScoring  # noqa: E402
from ffbot.history.actuals import week_actuals  # noqa: E402
from ffbot.history.board import historical_board  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.index import as_of  # noqa: E402
from ffbot.history.projections import players_asof  # noqa: E402
from ffbot.lineup import optimize  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2021-2023" or "2024"')
    p.add_argument("--weeks", default="1-15", help="scoring window, post-draft (default: %(default)s)")
    p.add_argument("--seeds", type=int, default=30, help="paired drafts per season (default: %(default)s)")
    p.add_argument("--seed-start", type=int, default=11, help="(default: %(default)s)")
    p.add_argument("--num-teams", type=int, default=12, help="(default: %(default)s)")
    p.add_argument("--rounds", type=int, default=15, help="(default: %(default)s)")
    p.add_argument("--agent-slot", type=int, default=4, help="(default: %(default)s)")
    p.add_argument("--adp-noise", type=float, default=1.0, help="opponent ADP jitter (default: %(default)s)")
    p.add_argument("--order", choices=["snake", "linear"], default="snake", help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument(
        "--agent-spice-level", type=int, required=True,
        help="DraftConfig.spice_level for the agent's own picks",
    )
    p.add_argument(
        "--control-spice-level", type=int, default=1,
        help="DraftConfig.spice_level for the control's own picks (default: %(default)s -- Chalk/pure VOR)",
    )
    return p.parse_args(argv)


def _score_drafted_roster(
    keys: list[str], board, cfg: Config, weeks: list[int],
    actuals_by_week: dict[int, dict[str, float]], snapshot_by_week: dict[int, object],
) -> float:
    """Total realized points across `weeks` for the objectively best legal
    lineup this exact roster could have started each week -- real pre-game
    status respected, final score known. Identical "oracle" construction to
    `ffbot.backtest.baselines.build_baselines`'s own oracle: feed realized
    points into `players_asof` as if they were the projection, so
    `lineup.optimize()` picks the hindsight-best lineup, then sum what it
    actually started. Applying this SAME policy to both the agent- and
    control-drafted roster is what makes the paired comparison isolate
    draft quality rather than also grading lineup-setting skill.

    `actuals_by_week`/`snapshot_by_week` are pre-fetched ONCE per season by
    the caller and reused across every seed and both policies -- neither
    `week_actuals` nor `as_of` memoizes internally (unlike the ECR curve
    fit, which is process-lifetime cached), so re-fetching per seed would
    re-parse the same CSVs dozens of times for data that never changes
    across seeds.
    """
    total = 0.0
    for week in weeks:
        actuals = actuals_by_week[week]
        snapshot = snapshot_by_week[week]
        rows = []
        for key in keys:
            bp = board.by_key.get(key)
            if bp is None:
                continue
            rows.append({"key": key, "name": bp.name, "position": bp.position, "team": bp.team})
        players = players_asof(rows, actuals, snapshot)
        plan = optimize(players, cfg.roster_positions, week, cfg)
        total += sum(p.projected_points or 0.0 for _slot, p in plan.assignments)
    return total


def _run_season(
    season: int, weeks: list[int], base_cfg: Config, agent_cfg: Config, control_cfg: Config,
    num_teams: int, rounds: int, agent_slot: int, adp_noise: float, order: str,
    seeds: range, cache_dir: str,
) -> list[tuple[int, float, float, bool]]:
    """`[(seed, agent_points, control_points, rosters_differ), ...]` for one
    season -- the board and every week's grading data are built ONCE and
    reused across every seed, since both are seed-independent."""
    board = historical_board(season, base_cfg, num_teams=num_teams, cache_dir=cache_dir)
    scoring = base_cfg.league or LeagueScoring.fantasypros_default()
    actuals_by_week = {w: week_actuals(season, w, scoring, cache_dir=cache_dir) for w in weeks}
    snapshot_by_week = {w: as_of(season, w, cache_dir=cache_dir) for w in weeks}
    out = []
    for seed in seeds:
        agent_result = simulate_draft(
            board, agent_cfg, num_teams, rounds, agent_slot, seed=seed,
            agent_uses_recommend=True, adp_noise=adp_noise, order=order,
        )
        control_result = simulate_draft(
            board, control_cfg, num_teams, rounds, agent_slot, seed=seed,
            agent_uses_recommend=True, adp_noise=adp_noise, order=order,
        )
        agent_keys = agent_roster(agent_result, agent_slot)
        control_keys = agent_roster(control_result, agent_slot)

        agent_points = _score_drafted_roster(agent_keys, board, base_cfg, weeks, actuals_by_week, snapshot_by_week)
        control_points = _score_drafted_roster(control_keys, board, base_cfg, weeks, actuals_by_week, snapshot_by_week)
        differ = set(agent_keys) != set(control_keys)
        out.append((seed, agent_points, control_points, differ))
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    weeks = parse_seasons(args.weeks)
    if not seasons or not weeks:
        print("error: --seasons/--weeks produced no values", file=sys.stderr)
        return 1

    base_cfg = Config.load(args.config)
    agent_cfg = Config.load(args.config)
    agent_cfg.draft = DraftConfig.from_spice_level(args.agent_spice_level)
    control_cfg = Config.load(args.config)
    control_cfg.draft = DraftConfig.from_spice_level(args.control_spice_level)
    # Both drafts must run the same roster CAPACITY/rounds regardless of the
    # config's own draft.rounds -- same reasoning scripts/backtest_season.py
    # documents (`cfg.draft.rounds = max(rounds, 15)`).
    rounds = max(args.rounds, 15)

    print(
        f"Simulating {len(seasons)} season(s) x {args.seeds} draft(s), "
        f"agent spice_level={args.agent_spice_level} vs control spice_level={args.control_spice_level}, "
        f"num_teams={args.num_teams}, rounds={rounds}, agent_slot={args.agent_slot}"
    )

    rows: list[tuple[int, int, float, float, bool]] = []  # (season, seed, agent, control, differ)
    for season in seasons:
        seeds = range(args.seed_start, args.seed_start + args.seeds)
        try:
            season_rows = _run_season(
                season, weeks, base_cfg, agent_cfg, control_cfg,
                args.num_teams, rounds, args.agent_slot, args.adp_noise, args.order,
                seeds, args.cache_dir,
            )
        except ValueError as exc:
            print(f"  season {season}: skipped -- {exc}", file=sys.stderr)
            continue
        for seed, agent_pts, control_pts, differ in season_rows:
            rows.append((season, seed, agent_pts, control_pts, differ))

    n = len(rows)
    print(f"{n} (season, seed) paired draft(s) simulated.\n")
    if n == 0:
        print("nothing to report", file=sys.stderr)
        return 1

    deltas = [agent_pts - control_pts for _s, _seed, agent_pts, control_pts, _d in rows]
    blocks = [(season, 0) for season, _seed, *_ in rows]  # block by SEASON, same convention as season.py
    mean, lo, hi = block_bootstrap_mean_ci(deltas, blocks, seed=args.seed_start)
    print(f"agent vs control, all drafts: mean delta = {mean:+.2f} season pts, 95% CI [{lo:+.2f}, {hi:+.2f}]")

    disc_deltas = [d for d, (_s, _seed, _a, _c, differ) in zip(deltas, rows) if differ]
    disc_blocks = [b for b, (_s, _seed, _a, _c, differ) in zip(blocks, rows) if differ]
    if disc_deltas:
        d_mean, d_lo, d_hi = block_bootstrap_mean_ci(disc_deltas, disc_blocks, seed=args.seed_start)
        print(
            f"agent vs control, DIFFERENT ROSTER ONLY ({len(disc_deltas)}/{n} drafts where the two "
            f"policies drafted a different set of players): mean delta = {d_mean:+.2f} season pts, "
            f"95% CI [{d_lo:+.2f}, {d_hi:+.2f}]"
        )
    else:
        print("agent vs control: every paired draft picked the identical roster in this sample.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
