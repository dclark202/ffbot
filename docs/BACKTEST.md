# Backtesting against NFL history

Every tunable in `config.yml` — `spice_level`, `upside_weight`, `balance_weight`,
the five `SPICE_PRESETS` rows — was set by judgment, not by evidence. The
optimizer in `ffbot/lineup.py` is provably exact, but *exactness is
conditional on the projections it's fed*, and everything `ffbot/week.py` and
`ffbot/edge.py` layer on top of those projections was, until now, never
checked against a single real season. This document is both the design for
closing that gap and the record of what's built: replay real NFL history
through the same pure functions the live paths use, and find out whether
spice/edge actually beat plain consensus, by how much, and where they don't.

**Status:** the weekly lineup path can be backtested today — B1 (historical
data), B2 (point-in-time projections), and B3 (the replayer + baselines) are
built (`ffbot/history/`, `ffbot/backtest/`, `scripts/backtest_lineup.py`).
The draft and waiver/streaming paths, and any actual weight tuning, are not —
see [Milestones](#milestones).

## The decision contract

Same rule this codebase already applies twice (draft intel, weekly intel),
applied a third time to the backtest itself:

> **A result only counts if the harness could not have seen the answer.**

A projection, a status, a weather reading — these are legitimate if they were
knowable before that week's kickoffs. A result, a season-end ranking, a
rank→points calibration fit on the season being scored — these are leakage,
and a backtest that admits any of them will report a number that looks great
and means nothing. See [Leakage register](#leakage-register) below for the
specific ways this bites.

## Why not `nfl_data_py`

The obvious starting point, `nfl_data_py`, is **deprecated** — superseded by
`nflreadpy`, which requires Polars. Neither is actually needed: nflverse
publishes every dataset as a plain CSV release asset on GitHub
(`nflverse/nflverse-data`), reachable over plain HTTPS. `ffbot/history/fetch.py`
uses stdlib `urllib`/`csv`/`gzip` only — **zero new dependencies**, consistent
with this repo's existing stdlib-only philosophy (see `requirements.txt`).

## Data sources

All verified live (HTTP 200/302) during scoping.

| Need | Source | Coverage | Notes |
|---|---|---|---|
| Weekly player box scores | nflverse `stats_player_week_{season}.csv` | 1999+ | Completions, distance-banded FG makes/misses, 2-pt conversions by type — richer than any FantasyPros export; see [Scoring gaps closed](#scoring-gaps-closed-by-real-box-scores) |
| Weekly team box scores (DEF) | nflverse `stats_team_week_{season}.csv` | 1999+ | Sacks/INTs/fumbles/TDs as a team unit, matching how Yahoo scores DEF |
| Schedules, weather, Vegas lines | nflverse `games.csv` (`schedules` release) | 1999+ | `spread_line`, `total_line`, `temp`, `wind`, `roof`, `gametime` — no separate weather API needed; the `lat`/`lon` in `data/stadiums.yml` stay unused for this purpose |
| Injury/practice reports | nflverse `injuries_{season}.csv` | 2009+ | Weekly `report_status` only — no IR/PUP/SUSP designations, a documented fidelity gap vs. live Yahoo |
| Weekly rosters | nflverse `roster_weekly_{season}.csv` | 2002+ | Position/team as of that week, not end-of-season |
| Historical ADP | Fantasy Football Calculator REST API | 2015+ | `fantasyfootballcalculator.com/api/v1/adp/{scoring}?teams=N&year=YYYY`, free, attribution requested |
| Historical rank-based rankings | DynastyProcess `db_fpecr.csv.gz` (FantasyPros ECR archive) | **2021-2024 clean; see below** | Ranks (`ecr`, `sd`, `best`, `worst`) — **no points**. This is the hard constraint on the whole plan; see below |

**The hard problem, confirmed and worse than assumed going in — twice over.**
First: historical *pre-week projections* — what a manager actually saw Sunday
morning — are not freely available as points, only as the free ECR rank
archive. Second, inspecting that archive directly during B2 turned up two
things the original scoping missed entirely:

1. **There are no weekly, matchup-aware rankings in the free archive at
   all** — only draft-season and **rest-of-season (ROS)** pages
   (`ros-ppr-{wr,rb,te}.php`, `ros-qb.php`, `ros-k.php`, `ros-dst.php`,
   `ecr_type == "rp"`). This turns out to sharpen the experiment rather than
   weaken it: ROS is matchup-agnostic by construction, which is exactly the
   right *frozen-projection control* — `week.adjusted_players`' entire job is
   layering a matchup-aware adjustment (weather, Vegas, volatility, upside)
   on top of a matchup-agnostic baseline. The question B3 actually answers is
   sharply well-posed: **does the matchup layer beat the market's
   matchup-agnostic rank?**
2. **The clean scrape window is 2021-2024, not "Dec 2019 onward."** Per-season
   distinct `ros-ppr-wr` scrape dates: 2020 has only 12, starting mid-October
   (partial season — no coverage for the first ~5 weeks); 2021-2024 each have
   15-19, spanning September through late December (clean); 2025's archive
   stops in early August, before week 1 even kicks off (**preseason-only,
   unusable for replay**). `ECR_CLEAN_SEASONS` in
   `ffbot/history/projections.py` is `(2021, 2022, 2023, 2024)` — **4 clean
   seasons, not 6.**

This makes the `naive` engine (any season back to whenever `injuries_*.csv`
coverage begins, 2009+) load-bearing rather than a nice-to-have — it's the
only engine that can be run at all outside the 4-season ECR window, and B3's
roster-sampling design (below) is what makes that engine's much larger
season count actually pay off statistically.

### Scoring gaps closed by real box scores

Several gaps `ffbot/scoring.py`'s `unmodeled_rules` flags as unmodelable from
any FantasyPros export turn out to be exactly modelable from nflverse's real
box scores — `ffbot/scoring.StatLine` grew the additive fields listed in its
"Historical-replay-only fields" comment block to carry them:

| Gap (per `unmodeled_rules`) | Closed by | New `StatLine` field(s) |
|---|---|---|
| Points allowed is a season total plugged into a distribution estimate | Real per-game points allowed | `points_allowed_game` — exact tier lookup, no `pa_distribution_estimated` flag |
| FG value estimated from a league-wide distance mix | Real per-kick distance bands | `fg_made_bands`/`fg_missed_bands` — no `fg_distance_estimated` flag |
| "Missed PATs — export has makes only, no attempts" | Real PAT attempts | `pat_missed` |
| "2-point conversions — no export column carries this" | Real 2-pt conversion counts, split by type | `pass_2pt`/`rush_2pt`/`rec_2pt` |
| — (partial; a proxy, not an exact match) | `passing_40` (nflfastR's 40+-yard-completion count) | `pass_completion_40plus` |

**Not closed even with real box scores:** 40+ yard *rushing*/*receiving* TD
bonuses (`BonusScoring.rush_td_40plus`/`rec_td_40plus`) need per-play yardage
on the scoring play specifically, which the weekly aggregate tables don't
carry. Left unmodeled in historical replay too, honestly, rather than
approximated with something misleading.

## Baselines

There is no single baseline, because the agent does three separable things.
Each needs its own — and the most important one for the weekly path is **not**
the oracle, since the oracle can't be beaten and says nothing about whether
*this system* is worth running.

### Weekly lineup — built (B3)

| Baseline | Built in `ffbot/backtest/baselines.py` as... | What it bounds |
|---|---|---|
| `oracle` | `lineup.optimize()` on realized points (`actuals.week_actuals`) | Ceiling. Unreachable; sets the scale for everything else. |
| **`control`** | `lineup.optimize()` on raw projections, no spice at all | **The control.** Oracle minus control is the entire addressable headroom spice could ever capture. |
| `agent` | `week.adjusted_players()` (real spice weights) then `lineup.optimize()` | The system under test. |
| `consensus` | Greedy: dedicated slots first, then flex, each claimed by the single best-projected healthy player remaining, **never reconsidered** | The "just follow the market, don't optimize" floor — distinguished from `control` specifically by NOT doing `lineup.optimize()`'s global swap-aware search. |
| `random_legal` | A uniform-ish random *slot-eligible* assignment — deliberately ignores availability/status, unlike the other four | True floor; calibrates whether the others differ from pure chance at all. |

All five are built from the exact same sampled roster within one
`(season, week)` unit (`build_baselines`), which is what makes `agent` vs.
`control` a true paired A/B rather than two independent samples. The only
honest claim shape is *"the agent captures X% of the Y-point gap between the
control and the oracle."* Spice weights are a config sweep, not a code
change: `SeasonConfig.from_spice_level` + per-key overrides via
`_season_from_dict` (`ffbot/config.py`), reachable from
`scripts/backtest_lineup.py --spice-level`.

**Roster sampling, not real drafted rosters (yet).** `ffbot/backtest/rosters.py`
draws seeded synthetic rosters from a fixed, documented position mix
(`_POSITION_MIX`), rather than running an actual draft. Four clean ECR
seasons x 15 weeks is ~60 manager-weeks if you replay one real team — nowhere
near enough to resolve the effect sizes the [statistics protocol](#statistics-protocol)
is built around. Sampling N rosters per `(season, week)` decouples
statistical power from how many historical seasons exist, and — unlike B4's
12 drafted managers — needs no draft simulator to exist first. **This is a
deliberate substitution the original plan didn't anticipate**; see the
[open questions](#open-questions) for what it costs.

### Draft — not yet built (B4/B5)

ADP-autopick roster vs. `draft.recommend()`'s roster, **both then managed by
the identical weekly policy** afterward so the draft is the only variable
under test. Swept over many seeds x all 12 draft slots.
`tests/test_edge.py`'s `_simulate()` (a 12-team snake draft where opponents
draft strictly by ADP) is directly reusable scaffolding — its docstring
explains why opponents must never be routed through `recommend()` themselves.

### Waivers / streaming — not yet built (B4)

Never-touch-the-roster (floor), greedy-highest-projection add (the naive
manager), oracle-waiver (best possible add each week, ceiling). Metric is
value added over the never-touch line, in real league-scored points.

## Statistics protocol

One team x 15 regular-season weeks x 4 clean ECR seasons is roughly **60
lineup decisions**, of which spice weights flip maybe a handful. A 1% edge is
unresolvable from that sample; a naive season-total comparison will produce a
confident number that is pure noise. Every milestone must follow this
protocol, pre-registered here before any sweep runs — B3 (`ffbot/backtest/`)
implements every line of it:

- **Evaluate at the decision level, not the season level.** Score every
  start/sit pair, not season point totals. — `metrics.Decision`, one per
  `(season, week, sampled roster)`.
- **Many independent samples per `(season, week)`**, not just one real team —
  B3 uses `rosters.sample_rosters` (N seeded synthetic rosters) rather than
  the originally-planned "12 simulated managers from a season replay", since
  a season-long draft/roster simulator doesn't exist yet (that's B4). See the
  [open questions](#open-questions) for the tradeoff this substitution makes.
- **Paired A/B.** Identical roster, identical week, one weight on vs. off.
  Shared variance cancels. — `metrics.paired_deltas`.
- **Only score discordant pairs** — decisions the weight actually flipped.
  McNemar-style, not a t-test on totals. — `metrics.lineups_differ` /
  `discordant_deltas`.
- **Bootstrap over (season, week) blocks**, not individual decisions — within
  one week, outcomes correlate (same games, same weather). —
  `metrics.block_bootstrap_mean_ci`.
- **Split by season.** Tune on early seasons, report on held-out late
  seasons, once. — **not yet enforced by any code**; today's harness reports
  whatever seasons are passed on the command line. Left as caller discipline
  until B5 actually needs a train/test split to tune against.
- **Report a confidence interval, never a bare point estimate.** —
  `scripts/backtest_lineup.py` prints the bootstrap CI and the discordant-pair
  count alongside every delta, so an underpowered result reads as
  underpowered rather than as a win.

## Leakage register

The section that makes a result trustworthy rather than merely impressive.
`ffbot/history/index.py`'s `as_of()` enforces the first three rows
*structurally* (it never even fetches a results-bearing source — see
`tests/test_history_index.py::TestAsOfLeakageGuarantee`); `naive_projections`/
`ecr_projections`/`ffbot/backtest/` enforce most of the rest the same way
(structurally, with a dedicated test), not merely by caller discipline.

| Risk | Where it bites | Mitigation | Enforced how |
|---|---|---|---|
| `games.csv`'s `temp`/`wind` are observed, not the Thursday forecast | `week.weather_multiplier` | Report weather's value as an upper bound; optionally degrade with forecast error (wind sigma ~3mph at 48h) and report both numbers | Open — not yet done |
| `games.csv` has no precipitation field at all | `week.weather_severity` | `GameInfo.precip_pct` is always `None` under historical replay — the weather signal is wind/temp/roof only, a documented fidelity reduction vs. a live `weekly/week-NN.yml` with a researched forecast | Structural (field literally can't be populated) |
| `spread_line`/`total_line` are closing lines | `week.vegas_multiplier` | Legitimate (still pre-kickoff), but sharper than what a Sunday-morning manager actually saw | Open — documented only |
| A player's own week-W result leaking into their week-W `naive` projection | `naive_projections` | `_game_log(..., before_week=week)` — weeks `>= week` are filtered before any average is computed | Tested: `TestNaiveProjectionsLeakage` in `tests/test_history_projections.py` |
| ECR `scrape_date` on or after kickoff | `ecr_projections` | `_latest_scrape_before` requires the scrape's calendar day strictly `<` the week's first game day — a same-day scrape is conservatively excluded, since `scrape_date` carries no time-of-day resolution | Tested: `TestLatestScrapeBefore` |
| rank -> points calibration fit on the season being tested | `ecr_projections` | `fit_seasons` defaults to every `ECR_CLEAN_SEASONS` entry *except* the target season, and the function **raises `ValueError`** if the caller passes a `fit_seasons` containing it — refused, not merely discouraged | Tested: `TestEcrProjectionsLeakage::test_refuses_fit_seasons_containing_the_target_season` |
| Game-day inactives instead of the Friday designation | `week.apply_status_overrides` | `injuries_{season}.csv`'s weekly practice-report designation, dated before kickoff — already what `index._build_player_status` uses | Structural (source data itself) |
| Injury report has no IR/PUP/SUSP designation | `week.apply_status_overrides` | Documented gap — under-detects those relative to live Yahoo; not fabricated from other signals | Open — documented only |
| End-of-season roster/position used for an early-season call | `player_pool` (roster-sampling universe) | `roster_weekly_{season}.csv`, never `players.csv` | Structural (source data itself) |
| Replacement level derived from full-season actuals | `naive_projections`' floor | Uses only the target season's *in-progress* per-game averages (via `board.derive_replacement`), never the completed season | Structural (only pre-`week` data is ever fetched) |
| Draft-board replacement level / tiers derived from full-season actuals (B4/B5, not yet built) | `board.derive_replacement`, `assign_tiers` | Build the board from preseason ADP + projections only | Open — not yet built |
| `ffbot/backtest/` code reading `WeekSnapshot.game_rows` directly instead of going through `actuals.week_actuals` | any baseline/metric | Declared as a package-level invariant in `ffbot/backtest/__init__.py`; `build_baselines`/`replay_week` only ever call `week_actuals` | Convention, code-reviewed — not (yet) statically enforced |
| Team relocations silently splitting one franchise's history in two | name/team matching | `ffbot.history.names.canonical_team`/`TEAM_RELOCATIONS` (OAK->LV, SD->LAC, STL->LAR, ...), applied before every team-keyed lookup | Structural |

## Architecture

```
nflverse (games, injuries,        DynastyProcess (ECR archive,
stats_player_week, stats_team_    playerids) ─────────────────┐
week, roster_weekly) ─────────────┐                            │
                                   v                            v
                          ffbot/history/fetch.py  (cache-first download, data/history/)
                                   │
      ┌───────────────┬───────────┼──────────────────┬──────────────────┐
      v                v          v                  v                  v
ffbot/history/  ffbot/history/  ffbot/history/  ffbot/history/  ffbot/history/
  index.py         actuals.py     names.py       projections.py  (all of the above)
as_of(season,   StatLine ->     canonical_team,   naive_projections,
week)           score_statline  actuals_key,      ecr_projections,
-> WeekSnapshot (ground truth,  match_actuals     player_pool,
-> WeeklyIntel/  post-hoc)                        players_asof
GameInfo                                                │
      │                                                 │
      v (unchanged)                                     v
ffbot/week.py, ffbot/lineup.py,  <──────────────  ffbot/backtest/
ffbot/board.py, ffbot/draft.py,                   rosters.py, baselines.py,
ffbot/edge.py                                     metrics.py, replay.py
      │                                                 │
      └─────────────────────┬───────────────────────────┘
                             v
                  scripts/backtest_lineup.py
              (efficiency table, paired CI, discordant
               count, naive-vs-ecr agreement)
                             │
                             v (B4/B5, not yet built)
              season simulator (real drafted rosters,
              waivers/streaming), weight sweeps
```

The load-bearing property: `as_of()` adapts straight into `week.GameInfo`/
`WeeklyIntel`/`StadiumInfo` — the exact types `build_week_brief`,
`adjusted_players`, and `rank_streamers` already consume, and
`projections.players_asof` builds `list[Player]` the same two functions take
unchanged. **Nothing in `ffbot/week.py`, `ffbot/lineup.py`, `ffbot/board.py`,
`ffbot/draft.py`, or `ffbot/edge.py` changed to make any of B1-B3 possible.**
The only production code touched outside `ffbot/history/`/`ffbot/backtest/`
is the additive `StatLine` fields in `ffbot/scoring.py` (default `None`;
every existing FantasyPros-sourced call site is bit-identical — see
`tests/test_scoring.py`'s `TestHistoricalReplayFields`).

## Milestones

- **B1 — historical data layer. Built.** `ffbot/history/fetch.py`
  (cache-first nflverse/DynastyProcess downloader; compressed sources cached
  gzipped, `.csv.gz`, not inflated to disk), `ffbot/history/actuals.py`
  (`StatLine` construction + league scoring from real box scores, plus
  `week_actuals` — the grading key), `ffbot/history/index.py` (`as_of()`,
  the point-in-time boundary), `ffbot/history/names.py` (crosswalk +
  team-relocation identity), `scripts/history_fetch.py` (bulk download +
  coverage table), `scripts/history_check.py` (scoring reconciliation
  against nflverse's own `fantasy_points_ppr`, name-match coverage).
- **B2 — point-in-time projections. Built.** `ffbot/history/projections.py`:
  two engines, both returning `{actuals_key: points}` for one
  `(season, week)`. `naive_projections` — a recency-weighted mean of the
  player's own league-scored prior weeks this season (reusing
  `cfg.projection.recency_window`/`recency_weight`), blended toward the
  prior season's per-game average for early weeks and regressed toward a
  positional replacement level (via `board.derive_replacement` — never
  hand-assumed) for a true unknown; usable back to 2009 (bounded by
  `injuries` coverage). `ecr_projections` — the latest ROS-ECR scrape
  strictly before kickoff, converted rank->points via a curve fit on
  `ECR_CLEAN_SEASONS` seasons other than the one under test (refuses
  otherwise — see the leakage register); usable 2021-2024. Plus
  `player_pool` + `players_asof`, the historical parallel to
  `ffbot/roster_source.py`'s row-to-`Player` conversion, which is what lets
  `week.adjusted_players`/`lineup.optimize` run against real history with
  zero changes to either.
- **B3 — lineup replayer + baselines. Built.** New package
  `ffbot/backtest/`: `rosters.py` (seeded synthetic roster sampling — see
  the [baselines section](#weekly-lineup--built-b3) for why sampling rather
  than real drafted rosters), `baselines.py` (all five lineups),
  `metrics.py` (efficiency, paired/discordant deltas, block bootstrap —
  implements the [statistics protocol](#statistics-protocol) exactly),
  `replay.py` (the runner). `scripts/backtest_lineup.py` is the CLI: prints
  the per-baseline efficiency table, the agent-vs-control bootstrap CI (both
  over all decisions and over discordant-only decisions), and the
  naive-vs-ecr projection agreement where the two engines' coverage
  overlaps.
- **B4 — season simulator. Not yet built.** 12 real managers drafted from
  that year's ADP (replacing B3's sampled rosters with drafted ones — the
  check that the sampling substitution didn't quietly change the
  conclusion), then a full-season replay with waivers and streaming. Note:
  `week.waiver_candidates` calls `optimize()` roughly 3x per candidate
  against `waiver_pool_size: 150` — this is the one path that needs a
  caching pass before a full 12-manager x 15-week x N-season sweep is
  affordable.
- **B5 — weight tuning. Not yet built.** Sweep `SPICE_PRESETS` and the draft
  edge weights on train seasons; report once on held-out seasons, per the
  pre-registered protocol above (including the not-yet-enforced
  train/test-split rule). Not a target to hand-hit — if the held-out result
  doesn't hold, that's the finding.

**Effort estimate:** B1 ~1 session (done). B2 ~1-2 sessions (done, same
session as B3). B3 ~1-2 sessions (done). B4 ~2 sessions (mostly the caching
pass). B5 ~1 session plus compute time for the sweep itself.

### First real run

Proof the harness works end to end, not a tuning result — read it as "the
control group in B3 is well-powered enough to say something," not as "spice
is settled." All four clean ECR seasons, `--rosters 500` (30,000 decisions,
10,708 of them decisions the spice weights actually flipped):

```bash
python scripts/backtest_lineup.py --seasons 2021-2024 --source ecr --rosters 500 --seed 11
```

```
Lineup efficiency (mean % of oracle points captured):
  control        0.862
  agent          0.862
  consensus      0.853
  random_legal   0.718

agent vs control, all decisions: mean delta = +0.00 pts, 95% CI [-0.28, +0.26]
agent vs control, DISCORDANT ONLY (10708/30000 decisions): mean delta = +0.00 pts, 95% CI [-0.77, +0.72]

NAIVE vs ECR agreement (2021, 2022, 2023, 2024, n=31663 player-weeks): Pearson r = 0.823
```

At `spice_level: 4` (today's `config.yml`), the agent is statistically
indistinguishable from the frozen-projection control — the 95% CI brackets
zero tightly even restricted to the decisions the spice weights actually
touched. Both comfortably beat the `random_legal` floor (0.718) and edge out
greedy `consensus` (0.853), so the machinery is working and the numbers are
sane; the mechanism it's testing just isn't showing a measurable edge at this
weight setting, on this baseline definition, over this window. Whether that
means spice needs different weights, needs B4's real drafted rosters instead
of B3's sampled ones, or is genuinely not adding value at the weekly-lineup
layer is exactly what B5 (and a harder look at the [open questions](#open-questions)
below) would need to resolve — this run establishes the instrument works,
not the answer.

## Open questions

- **Is 4 clean ECR seasons (down from the originally-assumed 6) enough to
  trust a held-out split at all?** ~60 decisions per season x 4 is even less
  statistical power than originally scoped. B3's `--rosters` sampling
  (below) is the mitigation, not a full substitute for more real seasons —
  `scripts/backtest_lineup.py`'s output reports a confidence interval for
  exactly this reason; treat a wide one as the honest answer, not as a
  reason to shop for a narrower one via a different seed.
- **Does sampling synthetic rosters (B3) instead of replaying real drafted
  ones change the conclusion?** A sampled roster's position mix
  (`rosters._POSITION_MIX`) is a fixed, hand-set approximation of a
  plausible manager's roster, not derived from anything — it could
  systematically differ from what real drafted rosters look like in a way
  that biases which decisions are even *available* to test (e.g., a real
  bench might carry more early-round talent at a flex-eligible position than
  the fixed mix samples). B4's drafted-roster replay is the direct check;
  until it exists, treat B3's results as informative about the mechanism,
  not as a final answer about real managers.
- **Is the `consensus` baseline's greedy "best-projected, never
  reconsidered" definition actually what "just follow the market" means?**
  It was built without live ECR *ranks* threaded through (only the ECR
  *points*, after calibration) — a purer "start the FantasyPros-ranked
  player" baseline would use `_ecr_snapshot`'s raw ranks directly rather
  than the same points dict `control` uses. The current definition still
  meaningfully differs from `control` (no optimizer, no reconsideration),
  but a future pass could tighten it.
- **Does the `naive` projection engine (2009+) actually track what a real
  manager would have believed**, or does its extra season count just buy
  noise? `scripts/backtest_lineup.py` reports `naive` vs. `ecr` agreement
  (Pearson r) on the overlapping 2021-2024 seasons automatically — check
  that number before leaning on `naive` for anything outside that window.
- **Trade support has no design** (see `CLAUDE.md`), so a season simulator
  (B4) can't model realistic in-season trades between managers — every
  manager plays out the season with their drafted/waived roster only. A
  reasonable simplification, but worth stating rather than silently assuming
  trades don't matter.
- **How much does the caching pass in B4 change the *result*, not just the
  runtime?** If any shortcut there quietly changes what `waiver_candidates`
  would have recommended, the season simulator stops actually testing the
  production code path.
- **The train/test season split the statistics protocol calls for is not
  yet enforced by any code** — today's harness will happily replay and
  report on the exact same seasons someone later tunes weights against.
  B5 needs to add this before any tuning result can be trusted; noted here
  so it isn't silently skipped.
