#!/usr/bin/env python3
"""Grade the DRAFT-side spice ladder against real NFL history (B5/B7) --
simulate N synthetic snake drafts per season, agent edge-weights vs. a
control, and score the resulting roster by REALIZED season points under a
weekly-OPTIMAL ("oracle") lineup. Reports a paired block-bootstrap delta.

    python scripts/backtest_draft.py --seasons 2021-2023 --seeds 30 --agent-spice-level 4
    python scripts/backtest_draft.py --seasons 2024 --seeds 50 \\
        --agent-spice-level 3 --control-spice-level 1
    python scripts/backtest_draft.py --seasons 2021-2023 --seeds 30 \\
        --agent-spice-level 1 --control-spice-level 1 \\
        --isolate bye_collision_weight=0.30 --out data/backtest/b7_draft_bye030.json
    python scripts/backtest_draft.py --seasons 2021-2023 --seeds 30 \\
        --agent-spice-level 1 --control-spice-level 1 --agent-policy adp

Why not scripts/backtest_season.py? That grader mixes THREE sources of
noise into one win-rate number: draft quality, weekly lineup-setting, and
schedule luck -- B6 found 12 full replays gave a points delta CI of
+/-35 pts, wide enough to make the comparison uninformative (see
docs/dev/BACKTEST.md). This script isolates draft quality alone: agent- and
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

`--isolate KEY=VALUE` (repeatable) is the flag to reach for when sweeping
one dial: it sets the agent to VALUE and the control to that field's
`DraftConfig` dataclass default, in one argument.

`--agent-override`/`--control-override KEY=VALUE` (repeatable) overlay
individual `DraftConfig` fields on top of the resolved spice-level preset --
the B7 isolation-sweep seam this script didn't have during B5, letting a
single dial be swept vs. a shared level-1 control without hand-editing
config.yml per run.

**The trap `--isolate` exists to close.** Because both sides start from
`--config`'s own draft block (see below), a dial that is already live in
config.yml is inherited by the CONTROL as well -- so `--agent-override X=v`
alone changes nothing, and the run reports "every paired draft picked the
identical roster," which reads like a finding about the dial rather than
the operator error it is. This really happened, to the first
`scarcity_weight` sweep (see docs/dev/BACKTEST.md's B8 section). Two guards
now make it impossible to hit silently: every run prints the resolved
agent-vs-control field diff, and a run whose two sides resolve identically
(or whose `--agent-override` key matches the control's value) exits 1 with
the fix spelled out.

Each side's `DraftConfig` is built by taking `--config`'s OWN draft block
(so `position_targets`/`position_caps`/`depth_decay`/etc. all survive
exactly as config.yml set them) and overlaying the spice-level preset's
fields, then any `--*-override`s, on top -- fixing a real B5-era bug where
`DraftConfig.from_spice_level(level)` replaced the WHOLE config, silently
discarding config.yml's `position_targets` and making `balance_weight`
sweeps a no-op (`draft.recommend` only applies that bonus when
`position_targets` is non-empty, and the discarded config always had it
empty). This means B7 draft-cell results are not directly comparable to
B5's -- see docs/dev/BACKTEST.md's B7 section.

`--agent-policy`/`--control-policy {recommend,adp}` (default: recommend)
choose whether that side's own draft slot picks via `draft.recommend()` or
the same noisy-ADP process every OPPONENT already uses
(`ffbot.backtest.draft_sim._adp_order`, `adp_noise=0` for a fully
deterministic market order) -- the "just follow the market" counterfactual,
surfaced here rather than only inside `draft_sim.simulate_draft`'s own
argument.

Only ECR_CLEAN_SEASONS (2021-2024) have a cached preseason board
(`ffbot.history.board.historical_board`) and FFC ADP -- see
docs/dev/BACKTEST.md's open question on that board's shallower player
coverage relative to the in-season ECR-derived one. 2025 additionally
qualifies (a real preseason ECR scrape and FFC ADP are both cached) and,
unlike the weekly ECR path, has never been looked at by any draft-ladder
tuning run -- see docs/dev/BACKTEST.md's B7 held-out ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import MISSING, fields as dc_fields, replace as dc_replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.backtest.draft_sim import agent_roster, simulate_draft  # noqa: E402
from ffbot.backtest.metrics import block_bootstrap_mean_ci  # noqa: E402
from ffbot.config import Config, ConfigError, DRAFT_SPICE_PRESETS, DraftConfig, LeagueScoring  # noqa: E402
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
    p.add_argument(
        "--agent-override", action="append", default=[], metavar="KEY=VALUE",
        help="overlay one DraftConfig field on top of the agent's resolved preset; repeatable",
    )
    p.add_argument(
        "--control-override", action="append", default=[], metavar="KEY=VALUE",
        help="overlay one DraftConfig field on top of the control's resolved preset; repeatable",
    )
    p.add_argument(
        "--isolate", action="append", default=[], metavar="KEY=VALUE",
        help="sweep ONE dial cleanly: sets the agent to VALUE and the control to that field's "
             "DraftConfig default, in one flag. Prefer this over --agent-override for an "
             "isolation sweep -- a dial already set in config.yml lands on BOTH sides, so an "
             "--agent-override alone silently measures nothing. Repeatable.",
    )
    p.add_argument(
        "--agent-policy", choices=["recommend", "adp"], default="recommend",
        help="how the agent's own slot drafts: draft.recommend() or the same noisy-ADP process "
             "every opponent uses (default: %(default)s)",
    )
    p.add_argument(
        "--control-policy", choices=["recommend", "adp"], default="recommend",
        help="how the control's own slot drafts (default: %(default)s)",
    )
    p.add_argument(
        "--out", default=None, metavar="PATH",
        help="write the full result (both columns, both metrics, the full CI, and the run's "
             "config) as JSON -- see scripts/backtest_tune.py's --out for the sibling schema",
    )
    return p.parse_args(argv)


def _parse_overrides(specs: list[str]) -> dict[str, Any]:
    """`["bye_collision_weight=0.30", "risk_ramp_start=1"]` ->
    `{"bye_collision_weight": 0.30, "risk_ramp_start": 1}` -- values that
    parse cleanly as `int` stay `int` (needed for `risk_ramp_start`/
    `risk_ramp_full`, the ladder's two non-float fields), numeric ones parse
    as `float`, and anything else stays a plain `str` (`rank_calibration`'s
    curve path, `board_points_source`) rather than raising -- a non-numeric
    DraftConfig field is still a legitimate thing to sweep, and an
    unparseable KEY=VALUE is caught by `_build_draft_config`'s own
    unknown-key check with a better message than a bare float() failure."""
    out: dict[str, Any] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"override {spec!r} must be KEY=VALUE")
        key, raw_value = spec.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        value: Any
        try:
            value = int(raw_value)
        except ValueError:
            try:
                value = float(raw_value)
            except ValueError:
                value = raw_value
        out[key] = value
    return out


def _field_defaults() -> dict[str, Any]:
    """`DraftConfig`'s own dataclass defaults, for `--isolate`'s control
    side. `default_factory` fields (the dict/list ones -- `position_caps`,
    `aliases`, ...) are excluded: they are not sweepable dials, and a
    caller aiming one at `--isolate` should get the clear KeyError-style
    message below rather than a mutable default shared between two configs.
    """
    out: dict[str, Any] = {}
    for f in dc_fields(DraftConfig):
        if f.default is not MISSING:
            out[f.name] = f.default
    return out


def _config_diff(agent: DraftConfig, control: DraftConfig) -> dict[str, tuple[Any, Any]]:
    """Every `DraftConfig` field where the two sides actually differ.

    This is what the run is really comparing -- printed on every run (see
    `main`) because the spice level and override flags on the command line
    do NOT determine it on their own: both sides start from `--config`'s own
    draft block, so a dial already live in config.yml lands on the control
    too and an `--agent-override` for it changes nothing at all.
    """
    out: dict[str, tuple[Any, Any]] = {}
    for f in dc_fields(DraftConfig):
        a, c = getattr(agent, f.name), getattr(control, f.name)
        if a != c:
            out[f.name] = (a, c)
    return out


def _build_draft_config(base_draft: DraftConfig, level: int, overrides: dict[str, Any]) -> DraftConfig:
    """`base_draft` (config.yml's OWN draft block -- `position_targets`/
    `position_caps`/`depth_decay`/etc. all preserved) with the `level`
    preset's fields overlaid, then `overrides` on top of THAT. See the
    module docstring for the bug this fixes relative to B5's bare
    `DraftConfig.from_spice_level(level)`.
    """
    DraftConfig.from_spice_level(level)  # validation only -- raises with config.py's own message/range
    preset_fields = dict(DRAFT_SPICE_PRESETS[level])
    merged = dc_replace(base_draft, spice_level=level, **preset_fields)
    if overrides:
        try:
            merged = dc_replace(merged, **overrides)
        except TypeError as exc:
            valid = ", ".join(f.name for f in dc_fields(DraftConfig))
            raise ConfigError(f"--agent-override/--control-override: {exc}. Valid keys: {valid}") from exc
    return merged


def _board_signature(draft: DraftConfig) -> str:
    """Cache key deciding whether two sides may SHARE one built board.

    Deliberately the whole `DraftConfig`, not a curated list of the fields
    known to affect board construction. That curated list existed here for
    exactly one commit and was already wrong: it predated
    `rank_calibration`, so the first sweep of that dial had both sides
    reading one shared board and reported a clean, entirely fictional
    `+0.00` across every cell -- the same dead-dial failure the isolation
    guards above exist to catch, arriving through the one door they don't
    watch. A list that must be updated by hand every time a board-affecting
    field is added will be wrong again, and its failure mode is a
    plausible-looking null result rather than an error.

    The cost of being conservative is one extra board build per season
    whenever the two sides differ at all (~a second), against draft
    simulations that dominate the runtime. Correct by construction beats
    fast and occasionally fictional.
    """
    return repr(draft)


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
    seeds: range, cache_dir: str, agent_uses_recommend: bool, control_uses_recommend: bool,
) -> list[tuple[int, float, float, bool]]:
    """`[(seed, agent_points, control_points, rosters_differ), ...]` for one
    season -- the board and every week's grading data are built ONCE and
    reused across every seed, since both are seed-independent.

    "Once" means once PER DISTINCT BOARD SIGNATURE, not once per season: a
    dial in `_BOARD_DERIVING_FIELDS` changes the board itself, so the two
    sides need their own. When neither side touches those fields (every
    sweep before B8) both signatures match and exactly one board is built,
    identical to this function's original behaviour.
    """
    boards: dict[str, object] = {}

    def _board_for(cfg: Config):
        sig = _board_signature(cfg.draft)
        if sig not in boards:
            boards[sig] = historical_board(season, cfg, num_teams=num_teams, cache_dir=cache_dir)
        return boards[sig]

    agent_board = _board_for(agent_cfg)
    control_board = _board_for(control_cfg)
    scoring = base_cfg.league or LeagueScoring.fantasypros_default()
    actuals_by_week = {w: week_actuals(season, w, scoring, cache_dir=cache_dir) for w in weeks}
    snapshot_by_week = {w: as_of(season, w, cache_dir=cache_dir) for w in weeks}
    out = []
    for seed in seeds:
        agent_result = simulate_draft(
            agent_board, agent_cfg, num_teams, rounds, agent_slot, seed=seed,
            agent_uses_recommend=agent_uses_recommend, adp_noise=adp_noise, order=order,
        )
        control_result = simulate_draft(
            control_board, control_cfg, num_teams, rounds, agent_slot, seed=seed,
            agent_uses_recommend=control_uses_recommend, adp_noise=adp_noise, order=order,
        )
        agent_keys = agent_roster(agent_result, agent_slot)
        control_keys = agent_roster(control_result, agent_slot)

        # Graded on the agent's board either way: both boards carry the same
        # players and the same points (only replacement-derived vor/tier/rank
        # differ), and `_score_drafted_roster` reads only name/position/team,
        # so this keeps the grading policy provably identical for both sides.
        agent_points = _score_drafted_roster(agent_keys, agent_board, base_cfg, weeks, actuals_by_week, snapshot_by_week)
        control_points = _score_drafted_roster(control_keys, agent_board, base_cfg, weeks, actuals_by_week, snapshot_by_week)
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

    try:
        agent_overrides = _parse_overrides(args.agent_override)
        control_overrides = _parse_overrides(args.control_override)
        isolated = _parse_overrides(args.isolate)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # --isolate KEY=V is exactly "agent gets V, control gets the dataclass
    # default" -- expanded here so everything downstream (the diff, the
    # guards, the --out record) sees one uniform pair of override dicts.
    if isolated:
        defaults = _field_defaults()
        for key, value in isolated.items():
            if key in agent_overrides or key in control_overrides:
                print(
                    f"error: --isolate {key}=... conflicts with an explicit "
                    f"--agent-override/--control-override for the same key; use one or the other",
                    file=sys.stderr,
                )
                return 1
            if key not in defaults:
                valid = ", ".join(sorted(defaults))
                print(
                    f"error: --isolate {key}=...: no scalar DraftConfig default to use as the "
                    f"control baseline. Isolatable fields: {valid}",
                    file=sys.stderr,
                )
                return 1
            agent_overrides[key] = value
            control_overrides[key] = defaults[key]

    base_cfg = Config.load(args.config)
    try:
        agent_draft = _build_draft_config(base_cfg.draft, args.agent_spice_level, agent_overrides)
        control_draft = _build_draft_config(base_cfg.draft, args.control_spice_level, control_overrides)
    except (ValueError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    agent_cfg = Config.load(args.config)
    agent_cfg.draft = agent_draft
    control_cfg = Config.load(args.config)
    control_cfg.draft = control_draft
    # Both drafts must run the same roster CAPACITY/rounds regardless of the
    # config's own draft.rounds -- same reasoning scripts/backtest_season.py
    # documents (`cfg.draft.rounds = max(rounds, 15)`).
    rounds = max(args.rounds, 15)

    agent_uses_recommend = args.agent_policy == "recommend"
    control_uses_recommend = args.control_policy == "recommend"

    print(
        f"Simulating {len(seasons)} season(s) x {args.seeds} draft(s), "
        f"agent spice_level={args.agent_spice_level} (policy={args.agent_policy}"
        f"{', overrides=' + str(agent_overrides) if agent_overrides else ''}) vs "
        f"control spice_level={args.control_spice_level} (policy={args.control_policy}"
        f"{', overrides=' + str(control_overrides) if control_overrides else ''}), "
        f"num_teams={args.num_teams}, rounds={rounds}, agent_slot={args.agent_slot}"
    )

    # What the run is ACTUALLY comparing, always printed. The command line
    # alone does not tell you: both sides inherit `--config`'s draft block,
    # so a dial that is live in config.yml lands on the control too.
    diff = _config_diff(agent_draft, control_draft)
    same_policy = agent_uses_recommend == control_uses_recommend
    if diff:
        print("resolved agent-vs-control DraftConfig differences:")
        for name, (a_val, c_val) in sorted(diff.items()):
            print(f"  {name}: agent={a_val!r}  control={c_val!r}")
    elif same_policy:
        # Nothing differs and both sides draft the same way -- the run
        # cannot produce a signal, and its "every paired draft picked the
        # identical roster" output would read like a finding about the dial
        # rather than the operator error it is. Refuse instead.
        hint = ""
        if agent_overrides:
            keys = ", ".join(sorted(agent_overrides))
            hint = (
                f"\n  --agent-override set [{keys}], but the control resolved to the same "
                f"value(s) -- they are already set that way in {args.config}, so both sides "
                f"inherited them.\n  Use --isolate KEY=VALUE, or pass an explicit "
                f"--control-override KEY=<baseline>."
            )
        print(
            "error: agent and control resolve to an IDENTICAL DraftConfig and the same draft "
            f"policy, so this run cannot measure anything.{hint}",
            file=sys.stderr,
        )
        return 1
    else:
        print("resolved agent-vs-control DraftConfig differences: none (policies differ)")

    # Narrower version of the same trap: other fields differ (so the check
    # above passed), but the dial actually being swept does not.
    silent = sorted(k for k in agent_overrides if k not in diff)
    if silent and same_policy:
        detail = ", ".join(f"{k}={getattr(agent_draft, k)!r}" for k in silent)
        print(
            f"error: --agent-override had no effect for [{detail}] -- the control resolved to "
            f"the same value, so this sweep does not isolate that dial. It is set in "
            f"{args.config}, which both sides inherit. Use --isolate, or pass an explicit "
            f"--control-override for it.",
            file=sys.stderr,
        )
        return 1

    rows: list[tuple[int, int, float, float, bool]] = []  # (season, seed, agent, control, differ)
    for season in seasons:
        seeds = range(args.seed_start, args.seed_start + args.seeds)
        try:
            season_rows = _run_season(
                season, weeks, base_cfg, agent_cfg, control_cfg,
                args.num_teams, rounds, args.agent_slot, args.adp_noise, args.order,
                seeds, args.cache_dir, agent_uses_recommend, control_uses_recommend,
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
    all_stats = {"mean": mean, "lo": lo, "hi": hi, "n": n}

    disc_deltas = [d for d, (_s, _seed, _a, _c, differ) in zip(deltas, rows) if differ]
    disc_blocks = [b for b, (_s, _seed, _a, _c, differ) in zip(blocks, rows) if differ]
    discordant_stats: dict[str, float] | None = None
    if disc_deltas:
        d_mean, d_lo, d_hi = block_bootstrap_mean_ci(disc_deltas, disc_blocks, seed=args.seed_start)
        print(
            f"agent vs control, DIFFERENT ROSTER ONLY ({len(disc_deltas)}/{n} drafts where the two "
            f"policies drafted a different set of players): mean delta = {d_mean:+.2f} season pts, "
            f"95% CI [{d_lo:+.2f}, {d_hi:+.2f}]"
        )
        discordant_stats = {"mean": d_mean, "lo": d_lo, "hi": d_hi, "n": len(disc_deltas)}
    else:
        print("agent vs control: every paired draft picked the identical roster in this sample.")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "seasons": seasons,
                    "weeks": weeks,
                    "seeds": args.seeds,
                    "seed_start": args.seed_start,
                    "num_teams": args.num_teams,
                    "rounds": rounds,
                    "agent_slot": args.agent_slot,
                    "adp_noise": args.adp_noise,
                    "order": args.order,
                    "agent_spice_level": args.agent_spice_level,
                    "control_spice_level": args.control_spice_level,
                    "agent_policy": args.agent_policy,
                    "control_policy": args.control_policy,
                    "agent_overrides": agent_overrides,
                    "control_overrides": control_overrides,
                    "isolated": isolated,
                    # The resolved field-level diff -- what the run actually
                    # compared, independent of how the flags spelled it.
                    "config_diff": {k: list(v) for k, v in sorted(diff.items())},
                    "n_drafts": n,
                    "all": all_stats,
                    "discordant": discordant_stats,
                    # Raw per-draft rows, so results from SEPARATE runs can
                    # be pooled afterwards. Needed whenever a dial forces
                    # one run per season -- a leave-one-season-out
                    # calibration curve, say -- since each run then
                    # bootstraps over a single season block and reports a
                    # degenerate CI that cannot be combined from summary
                    # statistics alone.
                    "rows": [
                        {"season": s, "seed": seed, "agent": a, "control": c, "differ": d}
                        for s, seed, a, c, d in rows
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
