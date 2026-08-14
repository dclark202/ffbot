# Backtesting against NFL history

Every tunable in `config.yml` — `spice_level`, `upside_weight`, `balance_weight`,
the four `SPICE_PRESETS` rows (B7 rescaled this from five) — was set by judgment, not by evidence. The
optimizer in `ffbot/lineup.py` is provably exact, but *exactness is
conditional on the projections it's fed*, and everything `ffbot/week.py` and
`ffbot/edge.py` layer on top of those projections was, until now, never
checked against a single real season. This document is both the design for
closing that gap and the record of what's built: replay real NFL history
through the same pure functions the live paths use, and find out whether
spice/edge actually beat plain consensus, by how much, and where they don't.

**Status:** the weekly lineup, draft, and waiver/streaming paths can all be
backtested today — B1-B7 are built (`ffbot/history/`, `ffbot/backtest/`,
`scripts/backtest_{lineup,season,weather,tune,draft}.py`). Two
previously-inert spice dials (`volatility_weight`/`upside_lean_weight`) are
live via a signal-provider seam; two momentum providers (`scoring_form`,
`usage_divergence`) were added alongside the existing `usage_form`; the
weather term and `game_script_weight` were both re-specified against real
data (`game_script_weight` ultimately retired). B5's weekly ladder was
re-derived along two axes (information vs. variance) and validated on a
held-out season — level 3 clears zero on both train and test. B7 rescaled
the whole ladder from 5 levels to 4 with new user-facing semantics, kept
level 3 unchanged, and re-tuned level 4's variance pair (the old level
4/5 split collapsed into one level, stopping short of the confirmed-negative
old level 5 point). B5's draft ladder found and fixed a confirmed-harmful
live weight (`arbitrage_weight`, now retired); B7 fixed a real grading-
harness bug, folded five previously-unladdered structural terms into the
ladder (all measured no-op or non-significant, none harmful), and measured
VOR-chalk drafting's real value over blind ADP directly — see
[Milestones](#milestones) and [docs/SPICE.md](SPICE.md).

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
| Historical ADP | Fantasy Football Calculator REST API | 2015+ | `fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12&year=YYYY`, free, JSON (`fetch.Source(format="json")`/`fetch_json`, `ffbot/history/fetch.py`). Built in B4 as `ffbot.history.board`'s ADP/stdev source — see [its coverage caveat](#open-questions) |
| Historical rank-based rankings | DynastyProcess `db_fpecr.csv.gz` (FantasyPros ECR archive) | **2021-2024 clean; see below** | Ranks (`ecr`, `sd`, `best`, `worst`) — **no points**. This is the hard constraint on the whole plan; see below. Also carries **preseason** cheatsheet pages (`qb-cheatsheets.php`, `ppr-{rb,wr,te}-cheatsheets.php`, `k-cheatsheets.php`, `dst-cheatsheets.php`) — B4's draft-board rank source, distinct from the ROS pages B2 uses |

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

## Signal providers — the dead-dial finding (B4)

B3's first real run reported the agent statistically indistinguishable from
the frozen-projection control at `spice_level: 4`. Diagnosing why, at the
start of B4, turned up something more fundamental than a bad weight: **three
of `SPICE_PRESETS`' five dials were structurally inert in every backtest run
up to that point** — not mistuned, simply never connected to anything.

| Dial | Status before B4 | Why |
|---|---|---|
| `volatility_weight` | **inert** | reads `week.WeeklyPlayerIntel.volatility`; `ffbot/history/index.py`'s `_build_player_status` only ever set `status` |
| `upside_lean_weight` | **inert** | same — `.upside` was never populated |
| `streaming_weight` | not exercised | only affects `rank_streamers`, which B3's lineup-only replayer never calls (B4's `waiver_candidates` calls do exercise it) |
| `weather_weight` | live, fired on **2.2%** of player-weeks | needs wind >15mph outdoors; 29% of games are domes, ~2% are windy, `precip_pct` is always `None` in replay |
| `vegas_weight` | live, fired on **99.6%** | the only dial doing real work before B4 |

Verified directly: `spice_bonus` returned exactly `0.0` for all 598 players
in a sampled week. Levels 4→5 differed almost entirely by `vegas_weight`
0.32→0.48 — the *reason* spice levels weren't behaving as genuinely distinct
settings wasn't a tuning problem, it was that most of the dial had no wire
running to it.

**`ffbot/history/signals.py`** is the fix: a `SignalProvider` protocol —
`(season, week) -> {normalized_name: {"volatility": 0..100, "upside": 0..100}}`
— merged onto a `WeekSnapshot` via `WeekSnapshot.with_signals()`
(`ffbot/history/index.py`), called *after* `as_of()` rather than folded into
it. That split is deliberate: a form-based provider needs
`stats_player_week` (a results-bearing source, by the letter of `as_of()`'s
own leakage guarantee, even though every week it reads is safely in the
past), and keeping providers outside `as_of()` means that guarantee never
has to make an exception for "but this fetch is safe" — it stays exactly
what it says, still enforced by `TestAsOfLeakageGuarantee` untouched.

The shipped reference provider, `historical_form`, is a stats-only proxy:
`volatility` from the coefficient of variation of a player's own prior-week
league-scored points, `upside` from ceiling-over-median, both
percentile-ranked within position, both leakage-bounded by the same
`projections._game_log(..., before_week=week)` `naive_projections` already
uses. It measures whether the volatility/upside *mechanism* is worth having
at all — not whether researched intel is any good; a stats proxy can't
capture what a beat writer knows about a game plan. Future spice signals
(from a later session) plug into the same seam.

**With `historical_form` active, the level sweep changes materially** —
2021-2024, `--rosters 200`:

| level | delta (no signals, B3) | delta (with `historical_form`) |
|---|---|---|
| 1 | +0.055 | +0.062 |
| 2 | +0.038 | +0.100 |
| 3 | +0.050 | -0.056 |
| 4 | -0.042 | -0.471 |
| 5 | -0.194 | **-1.147, 95% CI [-1.65, -0.65]** |

The spread between levels widened roughly 5x (0.25 pts to 1.2 pts), and
level 5's confidence interval now clearly excludes zero — the levels are
genuinely different settings once volatility/upside actually do something,
where before they were nearly the same setting wearing five different
labels. This is a diagnostic result, not a tuning one: it says the
mechanism now matters, not that today's weight VALUES are right — see
[Open questions](#open-questions).

## Weather re-specification (B4)

`scripts/backtest_weather.py` bins every outdoor-game QB/RB/WR/TE
player-week by wind speed and reports the mean ratio of realized points to
that week's ECR-projected points per bucket — isolating the wind effect
from player quality. Run against 2021-2024:

| position | 0-10mph | 10-15mph | 15-20mph (best-populated) | 20-25mph | 25+mph |
|---|---|---|---|---|---|
| QB | 0.589 (n=1605) | 0.527 | 0.510 (n=152) | 0.639 (n=25) | 0.466 (n=9) |
| RB | 0.731 (n=2635) | 0.723 | 0.621 (n=255) | 0.787 (n=37) | 0.581 (n=16) |
| WR | 0.723 (n=3932) | 0.691 | 0.678 (n=357) | 0.867 (n=65) | 0.599 (n=24) |
| TE | 0.726 (n=1790) | 0.728 | 0.621 (n=172) | 0.495 (n=24) | 0.345 (n=12) |

Two findings. First, every position's ratio sits well below 1.0 even in
calm weather — a real methodological artifact of the rank→points
calibration curve (see [Open questions](#open-questions)), not a weather
effect; only the RELATIVE change bucket-to-bucket is informative here.
Second, the only well-populated above-threshold bucket (15-20mph,
n=152-468) showed real but modest degradation — roughly 6-15% below the
calm baseline — while the old `weather_severity` ramp (linear, full
severity at 2x `wind_threshold_mph`) implied roughly double that at the same
wind speed for any given `weather_weight`. Buckets above 20mph were too thin
a sample (n<70, often <30) to calibrate a steeper ramp against.

**The fix**: `weather_severity`'s wind ramp now reaches full severity at
**3x** the threshold instead of 2x — a flatter, more conservative curve,
without touching `wind_threshold_mph` itself or the `weather_weight == 0.0`
exact-no-op contract. Measured effect: at `weather_weight=0.55`, the
previously statistically-significant harm (`agent` vs. `control`, CI
`[-0.37, -0.00]`) is now within noise (`[-0.33, +0.02]`); same at `0.90`
(`[-0.51, -0.05]` → `[-0.38, +0.02]`). The point estimates stay negative —
this isn't a manufactured win, just no longer a confident loss at the
weights tested.

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

**Roster sampling, not real drafted rosters, in B3.** `ffbot/backtest/rosters.py`
draws seeded synthetic rosters from a fixed, documented position mix
(`_POSITION_MIX`), rather than running an actual draft. Four clean ECR
seasons x 15 weeks is ~60 manager-weeks if you replay one real team — nowhere
near enough to resolve the effect sizes the [statistics protocol](#statistics-protocol)
is built around. Sampling N rosters per `(season, week)` decouples
statistical power from how many historical seasons exist, and — unlike B4's
drafted managers — needed no draft simulator to exist first. **This was a
deliberate substitution the original plan didn't anticipate**; B4 (below) is
the check that it didn't bias B3's conclusions, though that check hasn't
been RUN yet — see [open questions](#open-questions).

### Draft — built (B4)

`ffbot/backtest/draft_sim.py`'s `simulate_draft` generalizes
`tests/test_edge.py`'s single-manager `_simulate()` (still the reusable
scaffolding it always was — same docstring warning carried over verbatim:
opponents must never be routed through `recommend()`, which applies the
AGENT's own caps/targets against the AGENT's own roster) to N managers, one
of which drafts via `draft.recommend()` while the rest draft by ADP with
seeded jitter (`_adp_order`). Running the identical seed and opponents twice
— once `agent_uses_recommend=True`, once `False` — is the draft A/B, and the
`recommend()`-drafted roster is also what replaces B3's sampled ones for a
real season replay. Needs `ffbot/history/board.py`'s historical draft board
(preseason ECR rank → season-total points, calibrated on other seasons only,
plus Fantasy Football Calculator ADP) — see its [ADP coverage
caveat](#open-questions).

### Waivers / streaming — built (B4)

`ffbot/backtest/season.py`'s `simulate_season` runs `week.waiver_candidates`
(unchanged production code) every week against a real `LeagueRosters` built
from the other simulated managers' actual drafted rosters — exact here,
unlike production where it's hand-imported from a paste. Only the ONE
manager under test mutates via waivers; the other managers are static
reference opponents (frozen at their drafted roster, lineup set by the same
greedy policy the `consensus` baseline uses) — enough to give
`waiver_candidates` a real exclusion set and a real schedule
(`ffbot/backtest/schedule.py`) without needing every manager
independently tuned. Metric is season-long points delta plus win-rate delta
(`metrics.paired_win_rate_deltas`) for the one manager under test, over a
real head-to-head schedule — see `scripts/backtest_season.py`.

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
  seasons, once. — `scripts/backtest_tune.py` (B4) enforces this: it
  **refuses to run** if `--train`/`--test` share a season. It still can't
  enforce the DISCIPLINE of picking a cell by the train column alone (that's
  a human decision, not a code path) — it prints both columns for every grid
  cell as an exploration aid and says so loudly. B5 and B7 both did this:
  chose each cell by the train column alone, then spent the held-out look
  once — see [Milestones](#milestones) for both runs' numbers.
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
| Draft-board replacement level / tiers derived from full-season actuals | `board.derive_replacement`, `assign_tiers` (via `ffbot.history.board.historical_board`) | Built from preseason ECR rank + FFC ADP only — never the target season's own results | Tested: `TestHistoricalBoard::test_refuses_fit_seasons_containing_the_target_season` in `tests/test_history_board.py` |
| Season-total rank->points calibration fit on the season being drafted | `board._fit_season_rank_to_points_curve` | Same `fit_seasons`-excludes-`season` guard as `ecr_projections`, same reasoning | Tested: same file |
| `ffbot/backtest/` code reading `WeekSnapshot.game_rows` directly instead of going through `actuals.week_actuals` | any baseline/metric | Declared as a package-level invariant in `ffbot/backtest/__init__.py`; `build_baselines`/`replay_week`/`season.simulate_season` only ever call `week_actuals` | Convention, code-reviewed — not (yet) statically enforced |
| A signal provider (e.g. `historical_form`) fetching a results-bearing source through `as_of()` itself, widening its leakage guarantee | `ffbot/history/signals.py` | Providers run OUTSIDE `as_of()`, merged in afterward via `WeekSnapshot.with_signals()` — `as_of()`'s own fetch surface never changes | Tested: `TestAsOfLeakageGuarantee` (unchanged) + each provider's own leakage test |
| Team relocations silently splitting one franchise's history in two | name/team matching | `ffbot.history.names.canonical_team`/`TEAM_RELOCATIONS` (OAK->LV, SD->LAC, STL->LAR, ...), applied before every team-keyed lookup | Structural |

## Architecture

```
nflverse (games, injuries, stats_player_week,    DynastyProcess (ECR      Fantasy Football
stats_team_week, roster_weekly) ─────────────┐   archive, playerids) ┐   Calculator (ADP) ┐
                                              v                       v                    v
                                     ffbot/history/fetch.py  (cache-first download, data/history/)
                                              │
      ┌───────────────┬───────────────────────┼──────────────────┬──────────────────┐
      v                v                       v                  v                  v
ffbot/history/   ffbot/history/         ffbot/history/     ffbot/history/    ffbot/history/
  index.py          actuals.py           projections.py       board.py         signals.py
as_of(season,    StatLine ->            naive_projections,  historical_board  historical_form
week)            score_statline,        ecr_projections,    (preseason ECR    (stats-only
-> WeekSnapshot  week_actuals           player_pool,         rank + FFC ADP    volatility/
-> WeeklyIntel/  (ground truth,         players_asof         -> season Board)  upside proxy)
GameInfo         post-hoc)                    │                    │                │
      │                │                      │                    │                │
      │                └──────────┬───────────┘                    │                │
      │           snapshot.with_signals(...) <────────────────────────────────────────┘
      v (unchanged except            v
      the weather ramp)      ffbot/backtest/
ffbot/week.py, ffbot/lineup.py,  rosters.py, baselines.py, metrics.py, replay.py (B3)
ffbot/board.py, ffbot/draft.py,  draft_sim.py, season.py, schedule.py (B4)
ffbot/edge.py  <──────────────────────┤
      │                               v
      │                    scripts/backtest_lineup.py (B3), backtest_season.py (B4),
      │                    backtest_weather.py (B4 diagnostic), backtest_tune.py (B5/B7 harness),
      │                    backtest_draft.py (B5/B7 draft-side isolation sweeps)
      v
              weight sweeps against a train/test season split (B5, B7)
```

The load-bearing property: `as_of()` adapts straight into `week.GameInfo`/
`WeeklyIntel`/`StadiumInfo` — the exact types `build_week_brief`,
`adjusted_players`, and `rank_streamers` already consume, and
`projections.players_asof` builds `list[Player]` the same two functions take
unchanged. **Nothing in `ffbot/lineup.py`, `ffbot/board.py`, `ffbot/draft.py`,
or `ffbot/edge.py` changed across B1-B4.** `ffbot/week.py` changed exactly
once, in B4: `weather_severity`'s ramp span (see
[Weather re-specification](#weather-re-specification-b4)) — every other
line of every other production module outside `ffbot/history/`/
`ffbot/backtest/` is untouched. The only OTHER production code touched at
all is the additive `StatLine` fields in `ffbot/scoring.py` (default `None`;
every existing FantasyPros-sourced call site is bit-identical — see
`tests/test_scoring.py`'s `TestHistoricalReplayFields`).

## Milestones

B1-B7 are built: the historical data layer, point-in-time projections, the lineup replayer + baselines, the season simulator + signal-provider seam + weather re-specification, weight tuning for both the weekly and draft spice ladders, a signal-scoping pass, and (B7) a full audit + rescale of the spice ladder from 5 levels to 4 with new user-facing semantics. The weekly ladder (`SPICE_PRESETS`) was re-derived along two axes in B5 and validated on a held-out season; B7 kept level 3 (the validated cell) unchanged and re-tuned only the variance pair for the new level 4. The draft ladder (`DRAFT_SPICE_PRESETS`) had one exploratory pass in B5 (found and retired one confirmed-harmful live weight, `arbitrage_weight`) and a second in B7, which fixed a real bug in the grading harness (draft cells were silently discarding `config.yml`'s own `position_targets`/`position_caps`), folded five previously-unladdered structural terms into the ladder, and measured the value of VOR-chalk drafting over blind ADP directly (+123 season pts, 95% CI excluding zero) — still not a full re-derivation of every dial, since several remain structurally unmeasurable by the historical replayer (see B7's own section below).

### B7 — spice ladder audit + 1→4 rescale

Full request: re-audit both the weekly (start/sit + waivers) and draft spice ladders against the freshest available data, and rescale the user-facing dial from 1–5 to 1–4 with new semantics (1 Baseline/blind, 2 Tactician/tactics-only, 3 Sharp/evidence-backed, 4 Use-at-your-own-risk/everything-but-confirmed-harmful). See [docs/SPICE.md](SPICE.md) for the full feature-by-level matrix and every number below in context.

**Harness fixes made first** (see each script's own docstring): `scripts/backtest_draft.py` gained `--agent-override`/`--control-override`, `--out`, `--agent-policy {recommend,adp}`, and a fix to how a cell's `DraftConfig` is built — it now starts from `--config`'s own draft block (preserving `position_targets`/`position_caps`/`depth_decay`) rather than discarding it, a real B5-era bug that made `balance_weight` sweeps silent no-ops. `scripts/backtest_tune.py` gained `NO_PROVIDER_FIELDS`/`LINEUP_INERT_FIELDS` refusals so a dead-dial sweep (e.g. `kalshi_weight`, or any waiver-only dial this lineup-only replayer can't reach) errors instead of silently reporting a flat zero. `scripts/backtest_season.py` now registers all four signal providers (previously two). `ffbot.history.projections.ecr_projections` gained a season-level coverage guard (`_season_has_in_season_ecr_coverage`) after the fix attempt at a naive per-week staleness threshold broke every clean season's normal week-1/2 cold start — see that function's own comment for the false-positive rate that ruled out the simpler approach.

**Weekly ladder.** The anchor reproduction (train-only sweep, 2021-2023, all four signals, 400 rosters/week) reproduced B5's shape closely (level 3 train delta +0.369 vs. B5's +0.392; level 5 -0.807 vs. -0.848) — small drift attributed to code that changed since B5 (the Kalshi/live-conditions commit), not a harness regression. Level 3 was kept exactly as B5 validated it (unchanged). Level 4's variance pair (`volatility_weight`=`upside_lean_weight`) was re-tuned via a 3×3 grid sweep (train 2021-2023, 400 rosters/week): the matched (0.60, 0.60) point — old level 5 — was CONFIRMED negative (train delta -0.794, 95% CI [-1.39,-0.19], excludes zero); (0.45, 0.45) was the largest matched pair whose train CI still included zero (-0.311, CI [-0.84,+0.21]) and was selected per the pre-registered rule. The one-shot held-out spend on 2024 (reused a third time — B6 and B5 both already looked at it; caveat carried forward) and a fresh 2025 naive-source robustness run are both recorded in `data/backtest/`.

**Draft ladder.** Per-dial isolation sweeps (level-1-vs-level-1 + one override, train 2021-2023, 30 seeds) for the five newly-laddered structural terms plus a re-confirmation of `scoring_arbitrage_weight`: `bye_collision_weight`, `team_concentration_weight`, `same_team_position_weight`, and `block_weight` all measured an EXACT no-op (0/90 paired drafts differed) at both their shipped value and 2x it. `balance_weight` showed a directionally positive, not-yet-significant signal at 2x its shipped value (+19.32 season pts, 95% CI [-3.80,+46.97]). `stack_bonus` showed a similar directionally positive, not-yet-significant signal at both 0.15 and 0.30 (+8.46/+8.17 pts, both CIs crossing zero). `scoring_arbitrage_weight` reconfirmed B5's exact-zero finding. None measured confirmed-harmful, so all shipped at their existing judgment-anchored (config.yml) values. Separately, the blind-ADP-vs-VOR-chalk context run (agent policy = noisy-ADP-follow, control = `recommend()` with every edge weight zeroed) found +123.15 season pts, 95% CI [+16.10,+309.86] — excludes zero, the strongest single signal this audit found, and the reason level 1 is VOR-chalk rather than literal blind-ADP-following.

**Structural, non-weight changes.** Level 1's weekly waiver ranking is now genuinely naive (`SeasonConfig.waiver_value_mode = "points"`): raw this-week projected points, no replacement subtraction, no `hold_margin`, no `ros_blend`. Levels 2-4 use the pre-existing VOR-aware machinery (`"marginal"`, the default). `policy.can_drop`/`policy.can_bid_on`'s safety guardrails apply identically in both modes.

The full session-by-session log for B7 — every command run, every intermediate result — lives alongside B1-B6's in `private/backtest-log.md` (gitignored). Raw JSON results for every sweep are under `data/backtest/b7_*.json`.

The full session-by-session log -- every weight sweep, confidence interval, and dead end along the way -- lives in `private/backtest-log.md` (gitignored; ask the maintainer if you want to see it). What's below is the durable part: caveats worth knowing before trusting a number from any of this.

## Open questions

- **Is 4 clean ECR seasons (down from the originally-assumed 6) enough to
  trust a held-out split at all?** ~60 decisions per season x 4 is even less
  statistical power than originally scoped. B3's `--rosters` sampling
  (below) is the mitigation, not a full substitute for more real seasons —
  `scripts/backtest_lineup.py`'s output reports a confidence interval for
  exactly this reason; treat a wide one as the honest answer, not as a
  reason to shop for a narrower one via a different seed. **Confirmed to
  bite harder at the season level:** B6's first-ever real run of
  `scripts/backtest_season.py` (4 seasons x 3 seeds = 12 full-season
  replays) produced a season-points-delta CI of roughly ±35 points around
  a -0.7 point estimate — wide enough that the comparison couldn't
  distinguish "the tested dials do nothing" from "they do something, lost
  in the noise." `--seeds` sampling is the same mitigation `--rosters` is
  for the weekly metric, and increasing it is the cheap next step before
  reading anything into a `backtest_season.py` result.
- **Does sampling synthetic rosters (B3) instead of replaying real drafted
  ones change the conclusion?** A sampled roster's position mix
  (`rosters._POSITION_MIX`) is a fixed, hand-set approximation of a
  plausible manager's roster, not derived from anything — it could
  systematically differ from what real drafted rosters look like in a way
  that biases which decisions are even *available* to test (e.g., a real
  bench might carry more early-round talent at a flex-eligible position than
  the fixed mix samples). **B4 built the drafted-roster replay
  (`draft_sim.simulate_draft` + `season.simulate_season`) that can answer
  this, but the actual comparison — B3's sampled-roster efficiency numbers
  vs. the same metric over real drafted rosters — has not been RUN yet.**
  Treat B3's results as informative about the mechanism until that
  comparison exists, not as a final answer about real managers.
- **The rank->points calibration curve produces realized/projected ratios
  well below 1.0 even in calm weather** (`scripts/backtest_weather.py`'s
  0-10mph bucket: 0.59-0.73 across positions, not ~1.0). This surfaced while
  diagnosing weather, not because of it — the likely cause is that ECR rank
  updates ARE somewhat driven by recent performance, so pairing "rank this
  week" with "points this same week" in `_fit_rank_to_points_curve`
  correlates the fitted curve with exactly the outcome it's predicting,
  inflating its projections relative to a genuinely blind forecast. This
  doesn't invalidate the B3/B4 comparisons that only ever compare `agent`
  against `control` under the IDENTICAL projection dict (the bias cancels in
  the paired delta), but it does mean `lineup_efficiency`'s absolute
  percentages are probably understated across every baseline, and it
  directly undermines using the raw ratio table as anything but a
  relative-across-buckets signal, which is exactly how the weather
  re-specification used it. Worth a dedicated look before trusting any
  ABSOLUTE efficiency number (not a relative comparison) from `ecr_projections`.
- **Fantasy Football Calculator's ADP coverage is shallow relative to the
  preseason ECR board** — roughly 150-210 players per season vs. ~840+ on
  the ECR-derived board (confirmed against 2023: `historical_board` returns
  844 players, FFC returns 202). Most bench-depth players in
  `ffbot/history/board.py`'s board have no ADP at all, which
  `draft_sim.simulate_draft`'s ADP-order opponents simply never draft (a
  `None`-ADP player is excluded from `_adp_order` entirely) — meaning a
  simulated ADP-only draft is implicitly shallower than `--rounds` might
  suggest once the pool of ADP-tracked players runs out. Observed directly:
  a `recommend()`-drafted roster on the real 2023 board included several
  `adp=None` late picks (backup QBs, a third-string RB) that a pure-ADP
  opponent could never have drafted at all. Not a correctness bug — `board.by_key[...].team`
  still resolves fine — but a fidelity gap worth a wider free ADP source if
  B4's draft-side conclusions ever need to bear real weight.
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
- **Trade support has no design** (see `CLAUDE.md`), so `season.simulate_season`
  cannot model realistic in-season trades between managers — every manager
  plays out the season with their drafted/waived roster only. A reasonable
  simplification, but worth stating rather than silently assuming trades
  don't matter.
- **`season.py`'s static opponents never touch waivers at all** — only the
  one manager under test (`agent_slot`) adds/drops; the other 11 are frozen
  at their drafted roster for the whole season. Real leagues don't work that
  way (every manager streams/claims), so the schedule's win-rate numbers are
  against a league that's easier to beat than a real one. A deliberate
  simplification to avoid needing 12 independently-tuned agents, but it
  means a win-rate delta from `backtest_season.py` should be read as "better
  than a fixed baseline field," not "better in a realistic league."
- **B5's weekly ladder was run to a conclusion; the draft ladder was not.**
  `SPICE_PRESETS` is re-derived and validated on a held-out season (see the
  B5 milestone). `DRAFT_SPICE_PRESETS` exists and one confirmed-harmful
  live weight (`arbitrage_weight`) got found and retired, but three of its
  variance-axis dials (`upside_weight`/`volatility_weight`/`risk_weight`)
  remain structurally unmeasurable by `backtest_draft.py` today (see
  below) and no train/test split was spent on the draft ladder as a whole
  — it's shipped on judgment, anchored to `config.yml`'s pre-existing
  hand-set numbers for continuity, not derived.
- **`ffbot.history.board.historical_board` has no researched-intel or
  multi-source-ADP equivalent**, so `DraftConfig.upside_weight`/
  `volatility_weight`/`risk_weight` are structurally dead in
  `scripts/backtest_draft.py` — confirmed directly (`BoardPlayer.upside`/
  `.adp_spread`/`.availability_risk` are `None` for every sampled player,
  every season). This is the draft-side mirror of the exact dead-dial bug
  B4 fixed for the weekly path (`ffbot/history/signals.py`); a historical
  intel-equivalent provider (stats-derived proxy for breakout potential,
  a merged multi-source ADP pull for real `adp_spread`) is the natural
  follow-up, scoped but not built this session. Until it exists, any
  `backtest_draft.py` result involving those three dials should be read as
  "this backtest couldn't see them," not "they don't matter" — a live
  draft with a real `draft/intel.yml` DOES populate these fields.
