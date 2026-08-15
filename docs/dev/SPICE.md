# The spice ladder audit (B7)

The evidence behind `spice_level` — what backtest evidence exists for each
dial in `season.spice_level` (weekly start/sit + waivers) and
`draft.spice_level` (the live draft assistant), and the run results the B7
audit produced. The user-facing description of what each level actually
*does* lives in [docs/REFERENCE.md](../REFERENCE.md#spice-levels) — this
page is the "why," for the curious; that page is the "what." See
[docs/dev/METHODOLOGY.md](METHODOLOGY.md) for how the pipeline uses these
signals and [docs/dev/BACKTEST.md](BACKTEST.md) for the backtesting
environment and statistics protocol this audit followed.

**The scale changed from 1–5 to 1–4** in this pass, and the semantics
changed with it — this is not just a range clamp. If you have an old
`spice_level` in `config.yml`/`config.local.yml`: old 1 → new 1, old 2 has no
clean equivalent (see docs/REFERENCE.md), old 3 or 4 → new 3, old 5 → new 4.
A literal `5` now raises `ValueError` with this same migration note.

## Feature × level matrix — weekly (`SeasonConfig.SPICE_PRESETS`)

| Dial | 1 | 2 | 3 | 4 | Evidence class |
|---|---|---|---|---|---|
| `weather_weight` | 0 | 0 | 0.25 | 0.38 | **Validated** (B5, part of the level-3 bundle that cleared train+test) |
| `vegas_weight` | 0 | 0 | 0.20 | 0.32 | **Validated** (same bundle) |
| `usage_weight` | 0 | 0 | 0.15 | 0.20 | **Validated** (same bundle) |
| `momentum_weight` | 0 | 0 | 0.15 | 0.20 | **Validated** (same bundle) |
| `divergence_weight` | 0 | 0 | 0.05 | 0.10 | **Validated** (same bundle) |
| `volatility_weight` | 0 | 0 | 0.05 | 0.45 | Level 3: validated (bundle). Level 4: **re-tuned in B7** — largest value whose train CI still included zero; (0.60,0.60) confirmed negative |
| `upside_lean_weight` | 0 | 0 | 0.05 | 0.45 | same as `volatility_weight` |
| `matchup_variance_weight` | 0 | 0 | 0 | 0.60 | **Judgment** — structurally unmeasurable by the lineup-only replayer (no `lean` is ever passed); only `backtest_season.py` can exercise it, at a noise floor too wide to size it |
| `kalshi_weight` | 0 | 0 | 0 | 0.15 | **Untested** — Kalshi's NFL player-prop markets launched Sept 2025, zero overlap with the 2021-2024 backtest window |
| `venue_disruption_weight` | 0 | 0 | 0 | 0.10 | **Inconclusive** — no train/test season has ever isolated it, positive or negative |
| `streaming_weight` | 0.0 | 0.65 | 0.80 | 0.95 | **Unmeasured** — no backtest tool calls `rank_streamers` at all; judgment-set ramp, L1=0 is the naive "raw season floor" reading |
| `waiver_value_mode` | `"points"` | `"marginal"` | `"marginal"` | `"marginal"` | **Structural** — B7's new naive-vs-VOR waiver switch, not a weight |
| `blocking_hold_bonus` | 0 | 1.5 | 1.5 | 1.5 | **Judgment** — no synthetic-roster backtest has ever set `blocking: true`; flat from level 2 up |
| `denial_weight` | 0 | 0.15 | 0.15 | 0.15 | **Judgment** — needs `league_rosters.yml`/`league.yml` regardless of weight; flat from level 2 up |
| `denial_opponent_boost` | 0 | 0.15 | 0.15 | 0.15 | **Judgment**, flat from level 2 up |
| `denial_seed_window` | 0 | 2 | 2 | 2 | **Judgment**, flat from level 2 up |
| `denial_priority_floor` | 0 | 3 | 3 | 3 | **Judgment**, flat from level 2 up |
| `priority_value` | 0 | 0.3 | 0.3 | 0.3 | **Judgment**, flat from level 2 up |

**Retired, zero at every level:** `game_script_weight` (B5/B6, confirmed harm — see its own docstring in `ffbot/config.py`).

## Feature × level matrix — draft (`DraftConfig.DRAFT_SPICE_PRESETS`)

| Dial | 1 | 2 | 3 | 4 | Evidence class |
|---|---|---|---|---|---|
| `scoring_arbitrage_weight` | 0 | 0.05 | 0.10 | 0.10 | Measured **exact no-op** at 0.10 (B5, reconfirmed B7) — kept on theory (a league whose scoring diverges further from standard PPR would see this matter more) |
| `team_concentration_weight` | 0 | 0.06 | 0.06 | 0.06 | **Neutral-inconclusive** (B7 isolation sweep: exact no-op at 0.06, +3.48 pts CI [-34.91,+37.81] at 0.12 — 90 paired drafts) |
| `same_team_position_weight` | 0 | 0.10 | 0.10 | 0.10 | **Neutral-inconclusive** (B7: exact no-op at both 0.10 and 0.20) |
| `bye_collision_weight` | 0 | 0.15 | 0.15 | 0.15 | **Neutral-inconclusive** (B7: exact no-op at both 0.15 and 0.30) |
| `block_weight` | 0 | 0.20 | 0.20 | 0.20 | **Neutral-inconclusive** (B7: exact no-op at both 0.20 and 0.40 — `demand_ahead` needs rival-roster fidelity this synthetic sim may not fully carry) |
| `balance_weight` | 0 | 0.30 | 0.30 | 0.30 | **Neutral-inconclusive, leaning positive** (B7: exact no-op at 0.30, +19.32 pts CI [-3.80,+46.97] at 0.60 — CI nearly excludes zero) |
| `stack_bonus` | 0 | 0 | 0.15 | 0.30 | **Neutral-inconclusive, leaning positive** (B7: +8.46 pts CI [-5.97,+33.12] at 0.15, +8.17 CI [-10.04,+36.51] at 0.30). Enters at level 3, not 2 — pro-stacking is a variance play by the user's own semantics, not a structural tactic |
| `upside_weight` | 0 | 0 | 0.30 | 0.65 | **Judgment** — structurally unmeasurable; `historical_board` never populates `BoardPlayer.upside` (no `intel.yml` equivalent in the historical replayer) |
| `risk_weight` | 0 | 0 | 0.40 | 0.20 | **Judgment**, unmeasurable (`BoardPlayer.availability_risk` never populated). Non-monotonic by design — higher spice tolerates MORE risk |
| `volatility_weight` | 0 | 0 | 0.20 | 0.50 | **Judgment**, unmeasurable (`BoardPlayer.adp_spread` never populated — single-source ADP in the historical board) |
| `kalshi_weight` | 0 | 0 | 0 | 0.15 | **Untested** — same zero-overlap reasoning as the weekly side |
| `risk_ramp_start`/`risk_ramp_full` | 2/5 | 2/5 | 2/5 | 1/3 | **Structural** — earlier/faster ramp into risk-seeking at level 4 |

**Retired, zero at every level:** `arbitrage_weight` (B5, confirmed harm: -27.0 season pts, 95% CI [-36.4,-22.2]).

**Stays hand-set at every level, never part of the ladder:** `depth_weight`/`depth_decay` (bench-depth valuation — `week.drop_cost` reads `depth_weight` directly, so laddering it would let draft spice silently change *weekly* drop-cost behavior).

## Structural deltas that aren't weights

- **Level 1's weekly waivers are genuinely naive** (`waiver_value_mode = "points"`): candidates rank by this week's raw projected points (live points where covered, else the board's season total prorated over weeks remaining, zeroed on a bye), with no replacement subtraction, no `hold_margin`, no `ros_blend`, no "must beat zero" filter. The paired drop is whoever on the roster has the lowest projected points, not the worst `hold_margin`. Levels 2–4 use the pre-existing VOR-aware machinery unchanged.
- **`policy.can_drop`'s safety guardrails apply at every level, identically.** Undroppable flags, the never-drop list, draft-round/ownership protection, and rolling-priority caps are facts about the transaction, not strategy — they were never part of the spice ladder and B7 didn't change that.
- **The exact optimizer runs at every level.** "Blind highest-projected-points" still means the *provably optimal* legal lineup for those raw numbers — `lineup.optimize()`'s exactness was never something spice could turn off.
- **Injury status handling is unconditional.** `questionable_multiplier`/`doubtful_is_out` live in `ProjectionConfig`, outside the ladder entirely — a hard-OUT player never starts regardless of spice level.
- **Draft level 1 is VOR-chalk, not literal blind-ADP-following.** B7 measured the difference directly (see below): VOR-chalk beats following the market's own ADP order by a wide, significant margin. Blind-ADP is what Sleeper's own autopick already does; there was no reason to make the assistant degenerate to that at its most conservative setting.

## Run results

All raw JSON output lives under `data/backtest/b7_*.json`.

### Harness fixes (prerequisite to everything below)

- `scripts/backtest_draft.py` was silently discarding `config.yml`'s own `position_targets`/`position_caps`/`depth_decay` per cell (a real B5-era bug — `DraftConfig.from_spice_level(level)` replaced the WHOLE config instead of overlaying the preset onto it). Fixed before any draft sweep ran; B7's draft numbers are not directly comparable to B5's for that reason.
- `scripts/backtest_tune.py` gained `NO_PROVIDER_FIELDS`/`LINEUP_INERT_FIELDS` refusals so a dead sweep (e.g. `kalshi_weight`, or any waiver/denial-only dial the lineup-only replayer can't reach) errors instead of silently reporting a flat zero.
- `ffbot.history.projections.ecr_projections` gained a season-level coverage guard. The naive fix attempt (a flat per-week staleness threshold) broke every clean season's completely normal week-1/2 cold start (a 250+ day gap between kickoff and the freshest available scrape is NORMAL every single year, not a sign the archive died) — see that function's own comment for the false-positive data that ruled out the simple approach in favor of a season-level "did the archive publish anything at all near this season's kickoff" check.

### Weekly ladder

**Anchor reproduction** (train-only, 2021-2023, all 4 signals, 400 rosters/week) confirmed the harness still reproduces B5's shape before any change: level 3 train delta +0.369 (vs. B5's +0.392), level 5 (old scale) -0.807 (vs. -0.848). Small drift attributed to code changes since B5 (the Kalshi/live-conditions commit), not a harness regression.

**Level 4's variance pair was the one dial genuinely re-tuned.** A 3×3 grid sweep (train 2021-2023, 400 rosters/week, all 4 signals) of `volatility_weight`/`upside_lean_weight`:

| (vol, upside) | train delta | 95% CI |
|---|---|---|
| (0.30, 0.30) | +0.033 | [-0.45, +0.50] |
| (0.45, 0.45) | **-0.311** | **[-0.84, +0.21]** ← selected |
| (0.60, 0.60) | -0.794 | [-1.39, -0.19] ← excludes zero, confirmed negative |

Pre-registered rule: largest matched pair whose train CI still includes zero. (0.45, 0.45) wins; (0.60, 0.60) — the old level-5 point — is confirmed harmful and excluded.

**Held-out spend on 2024** (reused a third time — already looked at by both B6 and B5; caveat carried forward), presets exactly as shipped:

| Level | train (all) | test/2024 (all) |
|---|---|---|
| 2 | +0.000 [0,0] | +0.000 [0,0] — exact no-op on this replayer (structural dials unreachable, see matrix above) |
| 3 | +0.369 [+0.08,+0.66] | +0.360 [-0.02,+0.75] |
| 4 | -0.311 [-0.84,+0.21] | -0.420 [-1.56,+0.57] |

Level 3 stays the standout — a positive point estimate on both columns, train CI clearing zero. Level 4 stays statistically neutral, as designed.

**Fresh 2025 robustness check** (`--source naive`, since 2025 isn't in the ECR-clean window — a genuinely untouched season for the weekly ladder, never used in B5, B6, or any B7 tuning):

| Level | delta | 95% CI |
|---|---|---|
| 3 | **+0.78** | **[+0.41, +1.18]** — excludes zero |
| 4 | **+0.88** | **[+0.16, +1.66]** — excludes zero |

Both significant and positive — stronger than the ECR-based numbers, on data with zero chance of having been mined by any prior tuning pass. Caveat: `naive` projections are a lower-fidelity engine than ECR, so this isn't a like-for-like replication, but it's independent evidence in the same direction. (A first pass at this run omitted `--signals`, which structurally zeroes every trend-based dial — that run read as a near-null +0.04/+0.01 pts, confirming the significant result above comes specifically from the trend signals firing, not from weather/Vegas alone; re-run with signals for the numbers reported here.)

**Consensus-vs-control gap** (what the exact optimizer alone buys over "just pick the top-projected players," 2021-2023, 400 rosters/week): control (optimizer, no spice) captures 86.3% of oracle points; consensus (greedy top-points fill, no optimizer) captures 85.4%. About 0.9 percentage points of lineup efficiency comes from the optimizer itself, before any spice signal fires — this is the real content behind the level-1→level-2 jump's "VOR-aware" half.

### Draft ladder

**Per-dial isolation sweeps** (level-1-vs-level-1 plus one override, train 2021-2023, 30 seeds/season = 90 paired drafts): every one of the five newly-laddered structural terms measured either an exact no-op or a wide, zero-crossing CI — see the feature matrix above for each dial's specific numbers. None confirmed harmful; `balance_weight` and `stack_bonus` both showed a directionally positive lean that didn't reach significance.

**Blind-ADP-vs-VOR-chalk** (agent follows the noisy-ADP market with `--agent-policy adp`; control is `recommend()` with every edge weight zeroed — both level 1): **+123.15 season pts, 95% CI [+16.10, +309.86]** — excludes zero, the single strongest signal in this entire audit. This is why level 1 is VOR-chalk and not literal ADP-following.

**Held-out spend on virgin 2024+2025** (never touched by any B5 or B7 train sweep — 50 seeds × 2 seasons = 100 paired drafts each, agent vs. a level-1 control):

| Level | delta | 95% CI |
|---|---|---|
| 2 | +1.15 | [+0.00, +2.31] |
| 3 | -35.00 | [-83.19, +13.18] |
| 4 | -32.14 | [-79.56, +15.27] |

**Read this honestly.** Levels 3 and 4's ASSEMBLED bundles show a negative point estimate on this holdout, though neither CI excludes zero. This is a genuinely different signal than the clean per-dial isolation sweeps above, which found nothing confirmed harmful in any single term. Two things are true at once: (1) per the pre-registered discipline, this number is reported once and not chased — no re-tuning happened in response to it; (2) the CI width here (~±80-90 pts on 100 paired drafts) is comparable to `backtest_season.py`'s own previously-documented noise floor (B6 flagged ±35 season pts as "too wide to be informative" at higher sample counts), so this result is likely underpowered rather than a real reversal — but it is not proof of that either. Treat the draft ladder's assembled shape at levels 3-4 as **not re-validated by this holdout**, an open question for a future session with a larger seed count, not a confirmed problem.

### Season-path not-harmful checks

`scripts/backtest_season.py` (2021-2024, 3 seeds/season, all 4 signals) is the only tool that exercises real waivers (including the new naive level-1 mode), momentum-on-waivers, and `matchup_variance_weight` — at the cost of mixing lineup-setting and schedule luck into one noisy number over a real full-season replay (draft is shared, drafted once, identically, for both `agent` and `control` — only the in-season lineup/waiver policy differs between them). Used here only as a directional "did this break anything" check on the WEEKLY ladder, never for value selection.

| Level | season pts delta | 95% CI | win-rate delta | 95% CI |
|---|---|---|---|---|
| 1 | +0.0 | [+0.0, +0.0] | +0.000 | [+0.000, +0.000] |
| 2 | +0.0 | [+0.0, +0.0] | +0.000 | [+0.000, +0.000] |
| 4 | +10.6 | [-19.8, +38.3] | +0.022 | [-0.011, +0.067] |

Level 1 reads as an exact tie by construction: `control` is always the frozen-projection baseline in this simulator, and level 1's every spice weight is 0 with `waiver_value_mode` shared across both policies (this simulator varies only `cfg.season`, not a separate agent/control config pair for it) — a real, correct confirmation that level 1 collapses fully to "just the baseline," not a bug. Level 2 is also an exact tie across these 12 replays — consistent with the draft-side isolation sweeps above, where every one of level 2's structural dials individually measured either an exact no-op or a near-zero effect; 12 replays is too small a sample to expect a subtle, conditional tactic (denial only fires when a rival is genuinely threatening a specific player; blocking only applies to a `blocking: true`-flagged roster spot) to trigger at all. Level 4 leans positive on both metrics — season points and win rate — though neither CI clears zero. Taken together with the weekly ladder's own significant 2024/2025 results above, nothing here contradicts the shipped ladder; level 4 in particular shows no sign of being harmful over a real full season.

## Data-freshness appendix

- **The ECR (FantasyPros consensus rank) archive is frozen at 2025-08-08.** `ECR_CLEAN_SEASONS` stays `(2021, 2022, 2023, 2024)`. `ecr_projections` now refuses (rather than silently degrading) a season with no in-season scrape coverage at all — see the harness-fixes note above.
- **Held-out ledger, weekly:** 2024 has been looked at three times now (B6, B5, B7) — there is no unspent ECR season left. 2025 (via `--source naive`) is the one genuinely fresh season, used as a robustness check, not a formal held-out spend (different projection engine).
- **Held-out ledger, draft:** 2021-2023 were B7's train seasons; 2024 and 2025 were both virgin before this session (`historical_board` only needs a preseason ECR scrape + FFC ADP, both of which exist for 2025 despite the ECR archive being otherwise frozen) and were spent together, once.
- **Kalshi forward-logging is now live.** `ffbot.markets.kalshi_log.log_weekly_snapshot` appends the weekly player-prop signal (plus the game-odds context already fetched for the same run) to `data/kalshi_log/<season>.jsonl`, piggybacked on the existing level-4-only fetch — no new network call for anyone not running level 4. A future season's audit can finally grade this signal against `ffbot.history.actuals.week_actuals` instead of shipping on judgment indefinitely.
