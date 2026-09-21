# What the live season has found, and what it queued

The standing results log for the in-season path — the weekly manager, waivers,
streaming and lineup recommendations — and the weekly twin of
[TRAINING-FINDINGS.md](TRAINING-FINDINGS.md), which covers the draft path.
Before this existed the draft side had three layers of findings narrative
(that file, [BACKTEST.md](BACKTEST.md)'s milestones, and the gitignored
`private/backtest-log.md`) and the weekly side had none, so a live week-1
recommendation had nowhere to land.

The rule from TRAINING-FINDINGS.md holds here too, and harder: **a finding in
this document is a hypothesis, not a change.** B10's "Pattern worth naming" is
the precedent — three behaviour changes proposed on strong single-event
evidence, all three measured neutral-to-negative over 180+ drafts.

But the weekly path needs one more distinction the draft path does not,
because most of it is not gradeable at all:

| Status | Meaning |
| --- | --- |
| **Bug** | The code contradicts its own documented contract, or is provably incapable of the behaviour it claims. Ships on the correctness argument; no performance claim is made and none is needed. |
| **Design** | A judgment about what a number should mean. Argued, not measured. Ships only when the argument stands on its own and the alternative is indefensible. |
| **Hypothesis** | A tuning claim. Ships OFF (a dial at its no-op value) until a backtest agrees — unless the manager overrides that, in which case the entry says so and names it a judgment, not evidence (the noise floor, 2026-09-13, is the one case so far). |
| **Ungradeable** | No harness in this repo can reach the code path at all. Said explicitly, rather than quietly filed as a Hypothesis that never comes out. |

## Where the evidence lives

Every run writes a machine-readable record to
`weekly/reports/{season}-w{week:02d}-{source}.json` (`ffbot/week_log.py`) — the
full recommendation set with `PlayerMetrics`/`DecisionMetrics` per row, both
lineups, the per-seam live sources, and the resolved tuning block. Use it
instead of reading the rendered report: the typed fields are what a claim here
should cite. `reports/*.md` are the human-readable twins.

`scripts/autorun.py` passes a unique trigger id as `source`, so its scheduled
snapshots persist separately; a repeat run of the same surface (`gui`, `cli`)
overwrites in place.

---

## 2026 week 1 — a +0.6-point waiver claim reached a phone

**What happened.** The 2026-09-09 pre-kickoff autorun pushed a notification
recommending a waiver CLAIM of Kansas City DEF over the incumbent Detroit DEF
for a net of **+0.6**. The same run's other claim, David Montgomery at
**+54.4**, was correct — he had reached the wire through another manager's
error. Two recommendations out of the same code at the same moment, two orders
of magnitude apart, and nothing in the pipeline could tell them apart.

Evidence: `reports/2026-w01-pre_kickoff_2026-09-09T20-20-00.md` and its JSON
twin.

**The instability that makes this more than one data point.** The previous
day's run (`2026-w01-pre_waiver_2026-09-08.md`) emitted **no DEF claim at
all**, with KC also ranked #1 DEF streamer at the same 87.5 value. Nothing
real changed about either team overnight. A recommendation that flips from
silent to CLAIM on noise is not a close call that happened to land wrong.

**Seven distinct defects, not one.**

### 1. The CLAIM/HOLD test could not reject a small gain — **Bug**

`week.claim_verdict` priced a waiver claim as a constant FRACTION of the
candidate's own gain, so `claim_cost < gain` reduced algebraically to
`priority_value * (1 - p/n) < 1` — independent of the magnitude of `gain`. At
the shipped `priority_value=0.3` that holds for every positive gain at every
priority, so `HOLD PRIORITY` was **unreachable dead code** and no test
noticed.

A bug rather than a tuning disagreement, because the repo already documented
the correct behaviour and the code disagreed with it.
`SeasonConfig.priority_value`'s comment described an absolute cost "as a
fraction of decision scale"; `waiver_candidates`' docstring justified the
proportional form by asserting no genuine decision scale existed for a waiver
claim — and `week.decision_scale` is defined in that same module, 900 lines
above. The docstring also pointed at a `week.claim_cost` that has never
existed.

Fixed: `week.priority_option_cost` is absolute, a function of the slot and the
week's decision scale and never of the gain. The fade was also
reparameterised, because `(1 - p/num_teams)` was exactly 0.0 at the worst
priority *and* an unknown priority is assumed to be the worst — so a claim was
literally free in the two cases where the tool knows least.

At the real numbers (scale 7.1, priority 5/12) the slot costs a flat **1.42**:

| | gain | cost before | cost after | verdict after |
| --- | --- | --- | --- | --- |
| KC DEF | 0.75 | 0.13 | 1.42 | **HOLD PRIORITY** |
| D. Montgomery | 13.9 | 2.43 | 1.42 | **CLAIM**, clearing it ~10x |

Montgomery's net rose slightly (+54.4 → +55.4): a flat cost is cheaper than
the old proportional one at a large gain, so the fix prices the legitimate
claim *better* while reclassifying the noise one.

### 2. Half of a streamed position's gain was rest-of-season value — **Design**

`ros_blend: 0.5` applied uniformly, and for the KC row the rest-of-season half
(**+1.3 season points, i.e. +0.08/wk**) was the *larger* contributor —
inflating the blended gain to 3.75x the move's actual this-week value of +0.2.

Rest-of-season value is something you ACQUIRE, and at a streamed position you
never do: the counterfactual to holding this DEF all year is not "no DEF all
year", it is "whatever DEF is best next week", which costs nothing to obtain.
Crediting a DEF swap with half a season of value pays for an asset the tool
will re-decide in seven days.

`SeasonConfig.stream_ros_blend` now applies to `stream_positions` only.

**Shipped at 0.25, not 0.0 — and the first attempt is itself the finding.**
Pure this-week value was tried first and INVERTS the board: on the same live
week-1 data it ranked Las Vegas (rest-of-season **−2.0/wk**, VOR −29.2) above
Kansas City on a +1.2-point weekly matchup edge — cutting a tier-3 defense for
a tier-4 one to rent one good matchup. Reduce the dependence, do not remove
it: you still have to hold whoever you pick up, and the tool recommends one
move, not a season of churn. Ordering flips back above ~0.03, so 0.25 sits
with a wide margin on the right side of both failure modes.

What this dial does *not* do: with defect 1 fixed the KC row is a HOLD at
every blend in [0, 0.5]. This is about which candidate ranks first.

### 3. Claim-now vs. wait-for-free-agency was never modelled — **Bug** (missing input)

The alternative to claiming is not "don't get him" — it is "get him Wednesday
for free, at the risk someone takes him first." `ffbot/denial.py` already
calls that "an ordinary, non-speculative reason to move now" and computes it
as the `urgency` term, fungibility-discounted by
`denial.best_available_by_position`. The report had already printed the answer:
Montgomery carried **+42.9 claim urgency**, KC DEF carried **none**.

But `gameplan._stream_swap_rows` received neither `league_rosters` nor
`alternatives`, so the path that produced the KC row **could not compute
urgency at all** — while the ordinary-waiver path next door always had it. Now
wired through.

This needs no per-position rule, which is why it beats a threshold: a fungible
DEF nobody needs scores ~0 urgency and is never worth a slot, while a
genuinely scarce one a threatened rival needs still clears. It generalises to
the fungible tier 3-4 skill player for the same reason.

A declined stream claim now reads **WAIT FOR FREE AGENCY** rather than only
"HOLD PRIORITY" — "not worth a slot" and "pick him up Wednesday" are different
advice, and the second is actionable.

### 4. `net` was displayed in no unit at all — **Bug**

`+0.6` was `0.5 × (a rest-of-season points total) + 0.5 × (one week's points)
− claim cost`: an average of two scales differing by ~17x, which denominates
nothing. `week.waiver_candidates`' own SCALE note warns about exactly this
class of comparison and names the blend as the deliberate exception; the
exception was the defect.

In one unit, both halves agree the row is nothing:

| | as shown | in points/week |
| --- | --- | --- |
| rest-of-season | `+1.3 ros` | **+0.08/wk** |
| this week | `+0.2 wk` | **+0.20/wk** |
| the blend | **`+0.6`** | *no unit exists* |

Every recommendation is now headlined in points per week with the
rest-of-season case stated separately. The blend survives as the internal
ranking key — changing what `net` *is* would be a valuation change — but is
labelled `rank key … (blend)`, never as points. This is the defect that would
have let the row be dismissed on sight without any of the others being found.

### 5. `net` was re-priced after every filter, with no re-check — **Bug**

Repricing against the drop actually assigned (`gameplan.build_gameplan`) ran
after the `net > 0` bars *and* after the `recommend_count` slice, with nothing
re-examining the result. A row could qualify at +8, be re-priced to +0.4 by a
handoff penalty, and keep the word CLAIM — which is exactly what
`scripts/autorun.py` notifies on. Rows are now requalified, re-filtered and
re-sorted, the slice happens once at the end, and an add repriced negative
releases the drop key it had consumed (it previously held it, silently denying
the next add a legal drop).

Not the cause of the KC row — that came from `_stream_swap_rows`, which the
claims reprice loop skips. Found while tracing it.

### 6. `notify.min_waiver_net` was 0.0 — **Design**

Its own comment said a marginal claim "should stay quiet" and
`scripts/autorun.py`'s docstring said "not buzz a phone for a marginal one". At
0.0 both were false. Now 2.0, judgment-set: a notification gate is not a
valuation and no harness here models one, so evidence for it is unobtainable.
Defence in depth only — defect 1's fix retypes the KC row long before it
reaches this gate.

### 7. A declined move was still applied to this week's lineup — **Bug**

Found in the browser, after everything above was passing. With the KC row
demoted to an ordinary `add`, it went into `accepted_adds` — which feeds
`post_roster` — so the optimizer seated Kansas City and `pair_moves` rendered
**"Add & start: Kansas City Chiefs / Sit: Detroit Lions"** directly above the
row saying not to claim him yet. Two pieces of contradictory advice out of one
plan, and the lineup half was the one a human would act on.

The demote-to-`add` decision is what exposed it: `add` had always meant
"executable now", and "wait for free agency" is the first row type that means
"executable later". A waiting row is now kept visible but excluded from
`post_roster`, and releases the drop key it had consumed (the drop is not
happening either). `WAIT FOR FREE AGENCY` is also suppressed when
`incumbent_out` — a K/DEF on bye or OUT has to be replaced now, and telling
someone to wait would leave a starting slot at zero points.
`tests/test_gameplan.py::TestWaitForFreeAgencyIsNotAppliedToThisWeeksLineup`
covers all three.

Worth naming as a pattern: the full test suite was green and the CLI output
looked right, because the CLI renders the lineup and the add/drop list far
enough apart that the contradiction did not read as one. It took looking at the
rendered GUI, where they sit adjacent.

### 8. The noise floor — **Hypothesis**, shipped at 0.0

> **Superseded 2026-09-13.** The floor now compares points per week rather
> than the blend, and ships ON at 0.10 as the manager's call — see
> "our numbers didn't match the Sleeper app" below. What follows is the
> reasoning as it stood on 2026-09-09.

`policy.can_claim` is a `Verdict`-returning guardrail (per the invariant that
an irreversible action never gets a silent allow/deny — a claim burns priority
and drops a player), sized as `noise_floor_weight * decision_scale /
predictiveness[position]`. B10 measured projection-to-outcome correlation at
0.23 for DEF and 0.20 for K against 0.35-0.52 for the skill positions, so a
DEF needs roughly twice a WR's margin before a difference means anything.

Deliberately NOT B10's `predictiveness_shrinkage_blend`, which measured −25.8
over 180 drafts and ships off: that reshaped the board across every position
and could reorder picks, while this raises a recommendation threshold and
cannot reorder anything. B10's own section notes that no cheaper hypothesis
was tested; this is one.

It ships at **0.0, an exact no-op**, because the structural fixes above are
what actually addressed the reported row, and a tuning dial justified by one
observation is precisely the pattern that has gone 0-for-3 here. It is a
backstop for the residual case of two genuinely scarce players separated by
noise. Turning it on is a one-line change behind B15.

**Also done, on its own merits:** `draft.rank_calibration` now points at
`data/history/rank_curves.json` purely so `Board.predictiveness` is populated
on live boards — it was `{}` on every one of them, so 180 drafts' worth of
measurement was visible only to the historical replayer. Both blends stay 0.0
and both transforms early-return there, proved bit-for-bit in
`tests/test_board.py::TestPredictivenessWithoutRecalibration`.

---

## 2026 week 1 (Sep 10) — a quiet check looked exactly like a dead one

**What happened.** No notification arrived before Thursday night's game. The
18:47 check had run correctly and found nothing to do — and a silent phone
looks the same whether the check found nothing or never ran.

**Three changes, none a tuning claim.**

- **A pre-kickoff all-clear — Design.** `notify.heartbeat`, on by default,
  sends a message even when nothing changes. Every line is evidence rather
  than reassurance: starters locking at that kickoff, the projected total, the
  closest declined call, whether every live feed answered. A missing message
  is now the failure signal.
- **Kickoff times were never converted from Eastern — Bug.** `autorun.py`
  compared the schedule's naive ET kickoffs with the machine's local clock, so
  the documented "2h" lead ran 1h out in Central and would have fired at
  kickoff in Pacific. Fixed with a stdlib US daylight-time rule (Windows ships
  no IANA database, and the repo takes no tzdata dependency); trigger ids stay
  keyed on ET so the state file still matches. Three frozen-clock tests had
  been passing only because the dev machine is in Central.
- **Research runs unattended — Design.** The scheduled checks had run on live
  feeds alone all season while `weekly/week-NN.yml` sat empty.
  `ffbot/research.py` now runs `/research-week` headless: full passes before
  Tuesday's waiver check and after Friday's designations, and a quick pass
  just after inactives for each kickoff slot. The guardrails are enforced in
  code: a tool allow-list under `dontAsk`, official-source-only statuses,
  validate-or-roll-back, and slot passes that never delete.

---

## 2026 week 1 (Sep 10) — in-season valuation now reads live numbers only

**The question.** Why would waiver math read the draft at all? It shouldn't —
and for the most part it didn't, but not entirely.

**Corrected on the way.** `draft/intel.yml`'s risk and upside scores never
touched a weekly number; they feed draft valuation and a few display columns.
An earlier note here said otherwise and queued a refresh for them. Both were
wrong and have been removed.

**What was real — Bug.** The in-season pool was a copy of the draft board with
live rest-of-season points poured in, and the draft board leaked through:

- A player the live feed didn't cover kept his draft-board total. Measured
  live: 206 of 686 pool players, none rostered and none in the top-150 waiver
  scan — but including 13 kickers the K/DEF stream scan compares against real
  rest-of-season totals, a number that grows more wrong every week.
- A failed rest-of-season fetch fell back to the draft board for the whole pool.
- The CLI's streamer ranks, the CORE/STREAM roster status and the matchup lean
  all read the draft board directly.

**Fixed.** `gameplan.valuation_pool` is the one pool every weekly consumer
reads. Under a live source it is the rest-of-season board built `live_only`:
no player without a live projection, no ADP or draft intel. A failed fetch
skips waiver and streaming valuation with a note. Offline configs keep their
season board, the only data they have.

**Deliberately untouched:** hold/drop valuation still reuses the draft-named
`position_targets`, `depth_decay`, `depth_weight` and `bench_replacement_depth`
dials. They are tuning settings, not rankings.

---

## 2026 week 1 (Sep 13) — every free agent was priced as a waiver claim

**What happened.** Sunday's pre-kickoff push said "QB: Add & start Cam Ward —
Bench Trevor Lawrence" to a manager with a full 14/14 roster. The row behind
it said the opposite: "HOLD PRIORITY — not worth a claim". Tracing that turned
up a bigger mistake. The engine priced **every** unrostered player as a
rolling-waiver claim. On a Sunday almost everyone is a plain free agent you
add instantly at no priority cost, so Tyler Shough, Cam Ward and the rest were
labelled "not worth a claim" when they cost nothing to add.

**Two bugs.**

- **A declined add was seated by matching note wording — Bug.** Defect 7
  above excluded waiting rows by the `"WAIT FOR FREE AGENCY"` prefix. Only
  `_stream_swap_rows` writes that prefix; ordinary rows said `"HOLD PRIORITY"`
  and went straight into the lineup. "Act now" is now decided by
  `AddDropRec.kind` (`add` / `claim` / `wait`), never by a note's text.
- **Free agent vs. waivers was never modelled — Bug (missing input).**
  Sleeper publishes no per-player waiver flag. `ffbot/availability.py` now
  works it out every run from the league's `settings`, its transaction log
  (current and previous round) and this week's kickoffs:
  - **On waivers:** a player whose last transaction was a drop stays on
    waivers until the waiver run on the Eastern calendar date
    `waiver_clear_days` after the drop.
  - **Checked against the real claim:** this league's one week-1 claim was
    dropped Tue 7:09pm ET and awarded Thu 6:37pm ET. That fits the date rule,
    not a flat 48 hours and not `waiver_day_of_week`.
  - **Time of day:** it's learned from the latest processed claim. With none
    to learn from, the player stays on waivers through the end of his clear
    day.
  - **Locked:** once his game starts he can't be added until it ends (the
    manager's rule; he does not go to waivers), and he brings zero this-week
    points either way.

**Fixed.** `week.acquisition_verdict` turns a player's status into what a
move costs:
- **Free agent:** a zero-cost `add`, baked into the recommended lineup.
- **On waivers:** an ordinary `claim`/`wait` decision.
- **Locked:** a `wait`.
- **Status unknown:** the fetch failed or it's off. Every add is priced as a
  claim, with an alert.

The report splits into FREE AGENT ADDS, WAIVER CLAIMS and WAITING. A
free-agent add over `notify.min_waiver_net` notifies as
`ADD (free agent) …`, and every check's message says how many players are on
waivers and how many teams are locked mid-game. Research candidates carry
their status too.

**Judgment left as-is:** a free-agent sidegrade like KC DEF over DET
(+0.1/wk) is now an "Add & start" rather than "wait". It costs nothing but the
churn. Whether a noise floor should hide it is W1's question, not a new one.
*(Resolved later the same day: the floor now hides it — see the next entry.)*

---

## 2026 week 1 (Sep 13) — our numbers didn't match the Sleeper app, and noise read as a move

**What happened.** Two complaints from the Sunday run. "Drop DET for +0.1
pts? That's noise." And: "the score predictions don't line up with what I'm
seeing in Sleeper."

**The yardstick.** Sleeper's app number is each player's projected stats
multiplied by the league's own `scoring_settings`. Doing exactly that
reproduced Sleeper to within 0.03 points for Lawrence (18.91), Andrews
(10.24), KC DEF (8.51), DET DEF (7.35) and Cam Little (6.53).

**Four causes.**

- **Kickers inflated — Bug.** `score_statline` valued `fgm` with the
  league-wide `fg_distance_mix` and ignored Sleeper's per-distance bands.
  Sleeper's bands don't sum to `fgm`, and it pays nothing for the unbanded
  remainder; we paid for it. Little: 9.44 vs 6.53, on every kicker, weekly
  and rest-of-season.
- **Defense points allowed one tier off — Bug.** A projected `pts_allow` of
  20.5 missed the `max: 20` tier. Sleeper buckets it as 14-20. KC: 7.51 vs
  8.51.
- **Adjustments hidden in the one number shown — Design.** Research wrote
  `wind_mph: 40` for JAX from a note about storm *gusts*, and the weather
  ramp (calibrated on data that thins out past 20 mph, B4) cut Lawrence and
  Little by 21%. Nothing on screen said so.
- **Finished games showed projections — Design.** JSN still read 19.2 after
  SEA–NE went final at 26.2.

**Fixed.**
- Sleeper rows keep their raw stats and are scored by
  `scoring.score_sleeper_stats` against the live `scoring_settings` (copy in
  `league.yml` as the offline fallback). The `StatLine` fallback now maps FG
  bands and floors points allowed, so it lands near Sleeper too.
- Every surface shows "Sleeper 18.9 · ours 15.2 (weather: wind 40 mph −3.9,
  …)". The optimizer still ranks on ours; only visibility changed. Wind above
  30 mph is tagged "check: above calibrated range", and research is told
  `wind_mph` means sustained wind.
- Started games show LIVE/FINAL points from the matchups feed — descriptive
  only, never read into a valuation.
- The matchup header puts both teams on Sleeper's scale. It had compared
  our adjusted total against the opponent's unadjusted one.

**Found while verifying — Bug.** With the fixes in, the live report said
"Add & start Matt Gay — Drop Cam Little" while Little's game was in
progress. Sleeper locks a player at kickoff for the rest of the week. A
rostered player whose game has started is now `Player.game_locked`;
`policy.can_drop` refuses him and a locked K/DEF incumbent gets no swap row.
`lineup.optimize` holds him too: a locked starter is pinned to his slot
(removed from the matching), a locked bench/IR player is never a candidate,
and neither produces a `Move`. Everything else is solved exactly over what's
left. With nobody locked the output was checked identical to the previous
optimizer on 3,000 random rosters (seating, order, moves and reasons).

**Noise — see the floor entry below.** With DEF scoring corrected, KC over
DET is really about +1.2 this week, not +0.1, which is still inside what a
DEF projection can distinguish (B10: DEF projections explain ~23% of
outcome variance). `policy.can_claim` now compares points per week — this
week's gain or the rest-of-season gain per week, whichever is larger —
instead of the dimensionless blend, and a sub-floor start/sit swap is
labelled a toss-up rather than hidden.

**Shipped `noise_floor_weight: 0.10` — the manager's call, not evidence.**
At this week's decision scale that is roughly DEF 3.1, K 3.6, QB 1.9, RB 1.8,
WR 1.7, TE 1.4 points per week: KC/DET and Willis/Spears are floored, the
Montgomery-shaped claim is not (pinned in `tests/test_policy.py` and
`tests/test_projection_display.py`). The stream path both noise rows came
from remains ungradeable (B15 item 4). B15's pre-registered ordinary-waiver
sweep `{0.0, 0.05, 0.10, 0.20}` (train 2021-2023, 5 seeds) ran the same day
under the per-week floor. **It kept 0.10.** Measured as the agent against
itself on the same draft, 0.10 scored +9.2 season points vs no floor
(95% CI −3.7 to +30.8) and made 183 waiver adds instead of 225; 0.05 was
+3.2, and 0.20 was −5.1 and erratic. So the floor costs nothing measurable
and cuts churn. It's not evidence of a gain: the CI spans zero and 2022
carries the mean. The stock `backtest_season.py` agent − control numbers
couldn't answer this, because control applies the floor too. Full table in
[BACKTEST.md](BACKTEST.md)'s B15.

---

## 2026 week 1 (Sep 14) — a guessed wind number benched a quarterback

**What happened.** Week 1 was a 165.0–97.4 win (Rashee Rice still to play
Monday night). The post-mortem compared every projection in the week's logs
with real points. Most misses were ordinary noise: the flex call, Gainwell
(12.6) over Reed (12.1), scored 2.8 vs 5.0 while Coker scored 33.8 on the
bench, and three projections within a point is not a signal. One miss was not
noise.

**The wind — Bug (precedence) and Design (who owns weather).** Research wrote
JAX–CLE as `wind_mph: 40, precip_pct: 40` from a note about storm gusts
"near the I-95 corridor". Open-Meteo's reading for the stadium at kickoff was
about 6 mph sustained, gusts under 10, no rain. The cut landed on four players:

| Player | Sleeper | Ours | Wind cut | Actual |
| --- | --- | --- | --- | --- |
| Trevor Lawrence | 18.9 | 15.2 | −3.9 | **26.1** |
| Parker Washington | 12.1 | 9.7 | −2.5 | **19.3** |
| Cam Little | 6.5 | 5.2 | −1.4 | **12.0** |
| Quinshon Judkins | 11.1 | 8.9 | −1.2 | 7.0 |

It was not harmless. Sunday's 1pm push said "Add & start Cam Ward, bench
Trevor Lawrence", and on Sleeper's 18.9 Lawrence beats Ward's 17.4, so the
wind cut alone made that call.

The prompt fix from Sep 13 (`wind_mph` means sustained wind) would not have
been enough. `live.conditions.merge_conditions` gave a researched team
whole-entry precedence, so once research wrote Vegas totals for the game its
guessed wind also replaced the forecast.

**Fixed.** The merge is now field by field:
- **Weather** (wind, precip, gusts, temperature): the live forecast wins
  whenever it has a reading. Research only fills a gap, and a large
  disagreement is an alert ("JAX: research said wind 40 mph, forecast 6 mph —
  using the forecast").
- **Everything else** (kickoff, venue, Vegas totals): research still wins, and
  auto-fetched odds now fill totals research left out, instead of vanishing.
- **Dome games** get no forecast, but `weather_multiplier` is exactly 1.0 there.

Re-running the week-1 report against the unchanged week file puts Lawrence
back at 19.2 and raises the alert for both teams.
`TestMergeConditions::test_jax_cle_2026_week_1_regression` pins the case.

**W4, the Kalshi log — Bug.** Root cause confirmed (see the queue).
`SeasonConfig.kalshi_forward_log` (on in `config.yml`) now fetches and logs
while `kalshi_weight` is 0.0, and merges nothing into valuation. An empty prop
signal is an alert rather than a silent no-op, since silence is what hid this.
The first run wrote `data/kalshi_log/2026.jsonl`.

**A grader, so this is not found by hand again — new evidence source.**
`scripts/grade_week.py` (`ffbot/week_grade.py`) reads the week logs back. For
each adjustment family it asks whether the adjustment moved the projection
toward the real result, holding the row's other adjustments fixed. Descriptive
only: nothing in `ffbot/` imports it, and a test enforces that.

- **Projection graded:** the last pre-kickoff snapshot. If a log predates the
  adjustment breakdown, it falls back to the first post-kickoff snapshot, and
  says so.
- **Actuals:** FINAL points from the logs. Otherwise Sleeper's matchups feed,
  used only for games the schedule says are over, because that feed reports
  an unstarted game as 0.

Week 1:

```
ADJUSTMENTS  (did each one move the projection toward the real result?)
  Vegas      rows  12  applied   +0.8  helped  8 / hurt  4  net   +1.6 pts (helped)
  opponent   rows   2  applied   -0.9  helped  1 / hurt  1  net   +0.0 pts (neutral)
  weather    rows   4  applied   -9.0  helped  1 / hurt  3  net   -6.7 pts (hurt)
```

All four weather rows come from one game with a bad input, so this grades the
input, not `weather_weight` — the fix above is the response, not a dial change.
Vegas +1.6 over 12 rows is noise-sized. Both are hypotheses until more weeks
accumulate (`grade_week.py --all`).

**Scheduled, and gated — Design.** `scripts/autorun.py` now runs the grade
every Tuesday at 08:00 (`config.yml` `grade:`) and pushes it. The manager asked
for the grade to feed back into the model; it does so as a **proposal**, never
an automatic change.

- **Gate:** a dial is named only when its family has at least 4 weeks and 8
  games of evidence, with a ~95% interval on per-game error removed that
  excludes zero.
- **Counted per game, not per player:** the four wind rows above are one
  observation. Auto-shrinking `weather_weight` on them would have "fixed" an
  input bug by weakening a validated dial (B4).

The human, and a backtest, still move the dial.

---

## 2026 week 2 (Sep 15) — three defenses for one drop, and everyone was a free agent

**What happened.** The Tuesday 20:00 waiver check pushed this:

    Lineup: 3 move(s)
    RB: Start Quinshon Judkins (CLE) — Bench D'Andre Swift (CHI) — toss-up (…)
    FLEX: Start Jayden Reed (GB) — Bench Kenny Gainwell (TB)
    DEF: Add & start Tampa Bay Buccaneers — Drop Detroit Lions (DET)
    ADD (free agent) Kansas City Chiefs (net +2.8, drop Detroit Lions)
    ADD (free agent) Green Bay Packers (net +2.6, drop Detroit Lions)
    Availability: 0 player(s) on waivers; every other unrostered player is a free agent
    Research FAILED: research failed (exit 129: …)

The manager's read: incoherent (three defenses against one drop), false (on
a Tuesday evening every player who played is on waivers until Wednesday's
run), and beside the point (the Tuesday check exists for waiver claims; no
game until Thursday). The trace found four defects and one design gap. All
of it shipped on 2026-09-15; the four decisions it needed were the manager's.

### 1. After kickoff an unrostered player is on waivers — **Bug** (missing input)

`ffbot/availability.py` modelled only the per-drop half of Sleeper's rule: a
player was on waivers only if his last transaction was a drop, so once week
1's two drops had cleared the whole pool read as free agents and every DEF
row was typed `add` — zero cost, seated in the lineup. Sleeper's own support
article: "if a player's game begins on Thursday night, they will lock at
kickoff and remain on waivers until your selected waiver clear day." The Sep
13 entry recorded the opposite ("locked; he does not go to waivers") as the
manager's rule; the manager reversed it against the doc.

The model now: on waivers from his kickoff until the league's next weekly
run (`waiver_day_of_week`, Monday = 0, at `weekly_run_time_et` ≈ 12:05am
Pacific until a claim processed on that weekday teaches the real time); a
dropped player until the run on his clear date, the later of the two if
both; everyone else — a bye team's player, everyone after the run until his
next kickoff — a free agent. Last week's kickoffs come from the live schedule
(`report.load_everything`, in its own `except`) and feed ONLY this rule:
`game_states()` still reads this week's, because `gameplan` locks every
rostered player whose team is in it, and last week's games would have locked
the whole roster on a Tuesday. The single "learned run time" was also split
in two — the one week-1 data point (Thu 6:37pm) was a drop clearing, and
applying it to the weekly run would have been wrong.
`tests/test_availability.py::TestWaiverCycle` pins Tuesday evening,
Wednesday morning, Thursday night, and the drop-that-also-played case.

### 2. Four candidates for one slot were four accepted adds — **Bug**

`_stream_swap_rows` prices every unrostered DEF against the same incumbent
and returns up to `recommend_count` rows; the acceptance loop exempted stream
rows from the distinct-drop rule, so all four were accepted, `post_roster`
removed Detroit once and added four defenses (seventeen on a fourteen-man
roster), the optimizer seated Tampa Bay (best this week) while the list led
with Kansas City (best by `net`), and only `notify.min_waiver_net` hid two of
the four from the phone. Reproduced on
`tests/test_gameplan.py::_demo_shaped_loaded(stream_positions=("K", "DEF"))`:
nine players on a seven-slot roster.

Now one executable move per slot: `gameplan.fold_backups` keeps the best row
of a group and attaches the rest as typed `AddDropRec.backups`, best first —
every candidate for the same streaming slot, and every claim at the same
position spending the same drop (Sleeper processes one manager's claims in
order and fails a later one once the drop is spent, so "claim KC; then GB,
SF, TB" is one decision with an ordered fallback). The acceptance loop also
consumes the incumbent's drop key, so a second row naming him can never be
accepted. Backups render everywhere the row does (CLI, GUI, week log,
notification, research context) and carry their own `if_clears`. Claims at
different positions that share a drop stay separate rows — the documented
"each claim is its own scenario" design, which
`TestDenialFungibilityRegression` depends on.

### 3. Every check sent the same lineup-first message — **Design**

`notification_for` had no notion of purpose: the Tuesday check led with
`actionable_summary`'s lineup lines and appended ADD rows, exactly as a
pre-kickoff check does. Now `Trigger.kind` shapes the message. The
waiver-claims check (`autorun.waiver_weekday`/`waiver_hour` in `config.yml`;
the CLI flags override) sends your rolling priority, each claim over the bar
with its gain, clear time, seating consequence and backups, any free agent
worth adding tonight, then what to leave for free agency — never a lineup
line (the manager's call: no game for days). Nothing over the bar is said
out loud (`waiver_heartbeat`), not left silent; the Sep 10 entry's reasoning
about a dead task applies harder on the one night the check exists for.

### 4. A free-agent check the morning after the run — **Design**

New `post_waiver` trigger (`autorun.post_waiver_*`, Wednesday 07:00 by
default, after Sleeper's ~3am ET run): what Sleeper did with your claims
(`availability.claim_outcomes`, from the transaction log since the run), then
each free agent worth adding with where he starts and his backups. No
research pass — Tuesday's covers the week. Config-driven, like `grade:`, so
the registered 15-minute task picks it up with no re-registration.

### 5. `Write(path)` is not a permission rule the CLI accepts — **Bug**

`ffbot/research.py` passed both `Edit(weekly/week-02.yml)` and
`Write(weekly/week-02.yml)`; the Claude Code CLI now rejects the second form
(exit 129: "only Edit(path) rules are … Edit rules cover all file-editing
tools"), so the Tuesday research pass failed before it started and the check
ran on live data alone. `Write(...)` is gone; `Edit(path)` covers writes, and
the skeleton file is written before the CLI runs. The state file had already
recorded the failed pass, so it did not retry this week.

---

## 2026 week 2 (Sep 16) — a rookie a quarter of the league claimed, and the tool said nothing

**What happened.** Kaelon Black (RB, SF) was claimed off waivers by 3 of the
league's 12 managers at the 2026-09-16 00:00 run — rosters 2 (won), 5 and 12.
Tuesday's claims check produced no row, no note and no record of him. The
manager's read: *"there's some merit to having a way to weigh hot unknowns
against my worst players as possible streamers... it was just odd to me that
1/4th of my league put a claim in for him and you were silent."*

Evidence: `weekly/reports/2026-w02-pre_waiver_2026-09-15.json` — and the
finding is what that file does **not** contain. He appears in no list, under
no key, in any of the fifteen run records this season.

**The trace.** He was priced out, not missed. Week-2 projection 7.01 and
rest-of-season 83.66 (4.9/wk) against the worst rostered RB — Tyjae Spears,
7.65 and 123.51 (7.3/wk) — on a full 14/14 roster. He is worse on **both**
horizons than the man he would have cost, so `gain` was negative and
`week.waiver_candidates`' `gain <= 0.0` filter discarded him. Correct
arithmetic, silently applied.

**Seven sub-claims.**

### 1. A candidate can be discarded with no record — **Bug**

The `gain <= 0.0` filter `continue`s with no note, no count and no trace. Four
lines below it, in the same function, `noise_floored` exists under its own
stated contract: *"Surfaced as alerts rather than silently dropped -- 'nothing
worth recommending' and 'nothing priced' must not look the same."* The filter
immediately above violates that contract in the identical function. Fixed by a
`ScanTrace` out-parameter (the `_stream_swap_rows(floor_notes=...)` pattern
already in the repo), aggregated into `plan.notes` beside the existing
noise-floor line. `tests/test_week.py::TestScanTraceAccountsForEveryCandidate`
pins that every scanned candidate lands in exactly one bucket.

### 2. The candidate pool is truncated by VOR with no record — **Bug**

`waiver_candidates` slices the unrostered pool to `season.waiver_pool_size`
(150) by board VOR order. The block's own comment concedes the hazard — the
cut *"can starve a whole streaming position ... out of the scan entirely"* —
then mitigates exactly one instance of it (`stream_positions` get a backfill)
and leaves the general case live and unrecorded. It did **not** bite here:
Black was unrostered rank 75 of 387, well inside the slice. Recorded anyway,
because "never looked at" and "looked at and rejected" are two different
answers and the tool could not previously tell them apart.

### 3. Five dials are set, sliderized, and structurally inert — **Bug**

`usage_weight` 0.15, `momentum_weight` 0.15, `divergence_weight` 0.05,
`volatility_weight` 0.05 and `upside_lean_weight` 0.05 read
`WeeklyPlayerIntel` fields written only by `weekly/week-NN.yml`
(`week._parse_player_entry`) or the hand editor.
`.claude/commands/research-week.md` asks for none of the five and routes role
changes to `note`, which is unscored prose. `weekly/week-01.yml` carries 8
entries with `note` only; `week-02.yml` does not exist. Across all fifteen
logs in `weekly/reports/`, the only adjustment labels ever emitted are
`Vegas:`, `weather:` and `opponent:` — `_momentum_multiplier` has returned
exactly 1.0 for every player, every run, all season. Against *"never a silent
success."* Same shape as **W4**, different mechanism: a signal that cannot
fire, behind a weight that says it does.

The alert is the whole fix, and its **partial**-coverage variant matters more
than its zero-coverage one: `week.usage_score(None)` returns 0.0, not a
neutral 0.5, so which players are covered is itself a ranking effect rather
than merely less signal. The wire-or-retire decision is **W8**.

### 4. A hot unknown has no surface at all — **Design**

Not a bug: nothing is contradicted. The tool prices lineup gain, and a
bench-quality add has none — that is what `gain <= 0` correctly says. The
judgment is that the set of things worth **saying** is larger than the set
worth **doing**, and `ir_stash` is the standing precedent that this repo
already accepts the distinction. Ships as a parallel `GamePlan.speculative`
list: no `net`, no drop consumed, no `recommend_count` slot, never seated by
the optimizer, never a notification of its own.

### 5. League demand is fetched every run and discarded twice — **Bug** (missing input), then **Design**

The evidence the manager noticed is already in hand. Rivals' failed waiver
claims land on `LoadedReport.transactions` every run carrying
`metadata.notes: "This player was claimed by another owner."` and
`roster_ids`, then are dropped twice: `availability.derive` filters to
`status == "complete"`, and `availability.claim_outcomes` filters to the
user's own `roster_id`. An unread input already fetched — the same class as
week 1's finding 3.

Separately, and not a bug because nothing ever claimed they were wired:
`SleeperClient.trending` is fully built and tested with **zero callers**
anywhere in `ffbot/` or `scripts/`, and `client.ownership()` returns every
player while `sleeper_roster` joins it only to the user's own roster, with
week-over-week snapshots accumulating on disk that nothing reads. Wiring them
is the Design call; `ffbot/demand.py` is where it lands.

`denial.denial_value` ("a rival needs him too") infers contestedness from
rival rosters and curated standings alone — it reads no transaction log, no
ownership and no trending — and is computed some thirty-five lines **after**
the `gain <= 0` continue, so it can never rescue a filtered candidate.

### 6. The best demand signal is not available when the decision is made — **Ungradeable**

A fact, not a defect, recorded so it is not rediscovered. Failed claims
materialise only after Sleeper processes the run. On Tuesday evening nothing
is pending-visible; no cached transaction file in this repo has ever contained
`status == "pending"`, and whether the endpoint returns pending claims at all
is unverified (**W7**). So the rival-claim count is a **Wednesday
retrospective**, and `trending(kind="add")` plus the ownership delta are the
only pre-run signals. `DemandSignal.is_retrospective` carries the distinction
in the type rather than in a growing list of source-string special cases.

### 7. A stats-derived usage trend on the live path — **Hypothesis**, ships OFF

The obvious fix for defect 3 is to feed the dials from
`ffbot/history/signals.py`'s providers, which the backtest already uses.
Investigated and rejected for now: `usage_form` needs `min_games=3` over weeks
`< week`, so it is **empty for every player in weeks 1-3** and could not have
spoken on the day; it is built on WOPR (1.5 x target share + 0.7 x air-yards
share) while Black's week 1 was 14 carries and one target; and
`_USAGE_POSITIONS` is `{RB, WR, TE}`, so it never speaks about the two
positions the weekly manager actually rotates. It measures acceleration
*within an established role* and is blind to role *creation*, which is what a
hot unknown is. See **W5** and [BACKTEST.md](BACKTEST.md)'s **B16** item 4.

## 2026 week 2 (Sep 20) — MONITOR offered to drop the only defense, twice, for the same made-up number

**What happened.** The Sunday 15:05+15:25 pre-kickoff push carried:

```
MONITOR
  DEF Tampa Bay Buccaneers  -9.1 vs Kansas City Chiefs  +72% owned
  WR Xavier Hutchinson  -9.1 vs Kansas City Chiefs  +15% owned
```

The manager: *"These don't make any sense — -9.1 points? It's also
recommending to 'monitor' perhaps dropping my only defense for a backup WR."*

Evidence: `weekly/reports/2026-w02-pre_kickoff_2026-09-20T16-05-00.json`, and
its own 13:00 sibling three hours earlier, where the identical Tampa Bay row
read **+0.7**.

**Five sub-claims.**

### 1. Mid-slate, the clock chose the drop — **Bug**

`drops.protect_pct_owned` (60) already held twelve of the fourteen rostered
players undroppable. The survivors were Tyjae Spears (39.6% owned) and the
Kansas City defense (39.8%) — and Spears's game had kicked off, so
`policy.can_drop` refused him as well. `ranked_droppable` therefore returned
`[Kansas City]`, and `best_drop_key` took its `[0]` and labelled it *"worst
hold value on your roster"* — a player whose `hold_margin` was **107.5**,
earned precisely because dropping him empties the DEF slot. Every speculative
row was then priced against dropping the only defense.

The real recommendations were never exposed: they pay `best_drop_cost`, so a
107.5-point drop drives `net` far negative and the row disappears. A
`SpeculativeCandidate` carries **no** `net` and **no** `claim_cost` by design
("the absence IS the invariant"), so it printed the drop's name without the
cost that would have killed it.

A lock is a fact about the clock, not about value, and a speculative row is
not executable anyway — so the value question has one answer all Sunday.
`ranked_droppable(ignore_game_locks=True)` is that answer, used only by the
speculative path; every executable path keeps the default.

### 2. An already-played candidate's week number is about somebody else — **Bug**

Once a candidate's game kicks off, `waiver_candidates` zeroes his this-week
points — correctly, since none of them can be yours. `week_delta` then
degenerates to `-drop_week_proj`: the **incumbent's** projection, negated.
That is why a defense and a backup receiver printed the identical `-9.1`, and
why the same Tampa Bay row read `+0.7` at 13:00 and `-9.1` at 16:05 without
anything about Tampa Bay changing.

It also defeats the gate above it. `_is_below_your_worst`'s `week_delta < 0.0`
clause becomes true by construction, so a backup at a filled position — the
exact case that gate was written to exclude — walks through it at 1:01pm
having been correctly excluded at 12:59.

MONITOR means "came close to a bar this week and could clear it next week". A
player you can no longer collect a single point from this week did not come
close to anything. `_already_played` drops the row; he is eligible again on
Tuesday.

### 3. `drop_hold_margin` reported a different player's number — **Bug**

`_drop_context(key)` computed `hold_margin(best_drop_key, ...)` — the shared
drop's margin — while filling in the `drop_name` of whichever key it was
called with. So every incumbent-priced row (`_incumbent_drop`, the K/DEF
rule) quoted a margin belonging to someone else. Not user-visible in the push,
but it is a typed field on the row and `week_log` writes it. Reads `key` now.

### 4. MONITOR alone woke the phone — **Bug**

The deeper reason this reached a phone at all. The check had no lineup move
and no add; the body was a MONITOR section and the research line. CLAUDE.md's
standing guard is explicit — *"a speculative row may ride along on a message
already being sent, never trigger one"* — and
`TestSpeculativeNeverTriggersANotification` asserts it. It passed **vacuously**:
it called `actionable_summary(run, min_waiver_net)` with `cfg` defaulted to
`None`, which skips the MONITOR section entirely, while `notification_for`
always passes a `cfg`.

`actionable_summary` now renders the decision sections first and appends
MONITOR only once they are non-empty. Without the MONITOR-only push masking
it, a second gap surfaced: `heartbeat_message`, the pre-kickoff all-clear, was
the only one of the four quiet messages with no MONITOR section — the
speculative rows had simply been arriving as their own notification instead.
It now carries the same `MONITOR`-or-`_closest_call` pair
`waiver_heartbeat`, `post_waiver_heartbeat` and `look_heartbeat` all carry.

The same check now reads:

```
ffbot W2: all clear for Sun 15:05 kickoff
No lineup changes. Nothing worth a waiver claim.
Locking at 15:05: Trevor Lawrence (QB), Jaxon Smith-Njigba (WR), Parker Washington (W/R/T), Cam Little (K)
Projected lineup: 118.2 pts

MONITOR
  DEF San Francisco 49ers  +0.1 this wk vs your Kansas City Chiefs  +66% owned
  WR Antonio Williams  -0.0 this wk vs your Tyjae Spears  +20% owned
```

### 5. The all-clear had become a wall of reassurance — **Design**

With the MONITOR-only push fixed, the manager saw the all-clear it had been
bypassing and marked **every line of it** unnecessary:

```
ffbot W2: all clear for Sun 19:20 kickoff
No lineup changes. Nothing worth a waiver claim.          X
Locking at 19:20: Rashee Rice (WR), Kansas City Chiefs (DEF)   X
Projected lineup: 118.7 pts                               X
MONITOR
  RB Emmett Johnson  -4.0 this wk vs your Tyjae Spears  +9% owned
  WR Malachi Fields  -1.3 this wk vs your Tyjae Spears  87k leagues adding
Availability: on waivers until Wed 2:08AM -- 28 team(s) ...     X
Research: updated -- no official status changes           X
Live data: every Sleeper feed answered.                   X
```

Then: *"START/SIT, ADD/DROP, WAIVERS, MONITOR. That's it. Nothing else. Merge
the fields as needed too."*

Every X'd line was added for a reason, and each reason was locally sound: the
2026-09-10 silent check made "ran and found nothing" indistinguishable from
"never ran", so the all-clear became evidence rather than reassurance. The
over-correction is that evidence a human never acts on is indistinguishable
from noise, and there were six lines of it around two that mattered.

A push body is now exactly four sections, `START/SIT`, `ADD/DROP`, `WAIVERS`,
`MONITOR`, and nothing else. What survived was MERGED into a section rather
than deleted: rolling priority and "wait for free agency" are `WAIVERS` rows;
what the run did with your claims is the top of `WAIVERS` (the separate
`WAIVER RESULTS` block is gone); `_closest_call` is a `MONITOR` row, which is
what that section is for. `WAIVER CLAIM` is renamed `WAIVERS`.

Four empty sections is an empty body, and the title alone is the push. That
still satisfies the standing rule — the absence of the notification is the
failure signal, and the notification still arrives. `ffbot/notify.py` sends a
single space rather than an empty payload, because ntfy substitutes the
literal word "triggered" for an empty message.

The one thing that could not simply be dropped is a run built on broken
inputs. A failed research pass or a live feed that fell back means the four
sections were computed from partial data, and with the body stripped there is
nowhere in it for that to go. `_title_suffix` puts it in the TITLE
(`-- RESEARCH FAILED, check the report`, `-- NOT fully live, check the
report`), where it costs no body line and is still unmissable. Empty on a
healthy run, which is every run.

Casualties, all now dead code and removed: `availability_line` and
`_lock_lines`.

### What was NOT changed

`protect_pct_owned: 60` is doing far more work than it looks like. On this
roster it leaves exactly **two** droppable players all season, which is why
every waiver row is priced against Spears or the defense, and why the K
incumbent rule silently falls back to the shared drop (Cam Little is 94%
owned, so "Matt Gay K vs your Tyjae Spears" is still what the trace note
prints). That fallback is deliberate and documented in `waiver_candidates`.
Whether 60 is the right number is **W9**, not a bug.

---

---

## The queue

### W9 — grade `drops.protect_pct_owned` — **moved to 95 by the manager's call 2026-09-20, ungraded**

At 60 it held twelve of fourteen rostered players undroppable, so every waiver
and speculative row this season was priced against one of two names, and the
K/DEF incumbent rule fell back to the shared drop for any incumbent above the
bar (Cam Little, 94% owned). That is a large, invisible constraint on the
whole in-season path, and it is what let one kickoff collapse the list to a
single player.

At 95 this roster has nine droppable players instead of two, and the dial
becomes what its comment says it is — a backstop against dropping someone the
whole league wants — rather than the de-facto valuation. `week.hold_margin`
does the actual work. Note this changed no output on the 2026-09-20 re-run:
Tyjae Spears was already the worst by hold margin, so widening the set did not
move `[0]`. The effect is on future weeks, and on the K incumbent rule.

Ungraded. It is an ordinary-waiver dial, so `scripts/backtest_season.py` can
sweep it the way **B15** swept the noise floor, and until it does 95 is a
judgement call, not evidence.

### W1 — grade `noise_floor_weight` — ordinary-waiver sweep running; stream path blocked on harness work

See [BACKTEST.md](BACKTEST.md)'s **B15**. Ships at 0.10 by the manager's call,
and must stay on: the sweep may move the value, never set it to 0.0.
Gradeable on the ordinary-waiver path
via `scripts/backtest_season.py`; **ungradeable on the path that produced this
finding**, because `ffbot/backtest/season.py` never calls
`gameplan.build_gameplan`, so `_stream_swap_rows` never executes in replay.
B15a scopes the harness work that would change that.

### W2 — is `urgency` over-weighted relative to real gain? — **open, not investigated**

Montgomery's row was `net +55.4` of which **+42.9 was urgency**, against a real
lineup gain of +13.9 — the denial term was three times the actual benefit. The
recommendation was correct, so nothing here is evidence of a defect, and it was
explicitly left alone while fixing the KC row. But a term that can outweigh the
gain it is attached to by 3x deserves a look before it produces a bad
recommendation of its own. Nothing has graded
`denial_weight`/`denial_opponent_boost` on the in-season path.

### W3 — does `ros_blend: 0.5` mix scales for non-stream positions too? — **open**

Fixing the display (defect 4) does not fix the ranking key: `net` is still an
average of a season total and a single week at every position.
`stream_ros_blend` narrows the damage where it was worst. Whether the blend
should be a per-week-normalised quantity everywhere is a valuation change and
belongs in a backtest, not a judgment call.

### W4 — Kalshi forward-logging has never produced a file — **fixed 2026-09-14**

[SPICE.md](SPICE.md) claims "Kalshi forward-logging is now live", and
`data/kalshi_log/` did not exist on disk. The cause was the suspected one: the
log call sat inside `if kalshi_weight != 0.0`, and `use_untested_features: false`
holds that weight at 0.0. So the log that could earn the signal a place could
never run. See the 2026-09-14 entry above.

### W5 — does a stats-derived usage trend belong on the live path? — **open, blocked on two unknowns**

nflverse's in-season release latency for `stats_player_week` (reached through
the historical `ffbot/history/fetch.py` path, never used live) is unverified,
and so is the fix for the neutral-point defect: `usage_score(None)` returns
0.0 rather than 0.5, so switching a partially-covering source on multiplies
every covered candidate up and leaves every K/DEF at exactly 1.0 — a
cross-positional bias produced by coverage, not by signal. In the backtest
this was masked because both arms covered the same population. Ships OFF
meanwhile. B16 item 4 pre-registers the sweep; that item is itself blocked on
B15's `--sweep` harness work.

### W6 — should league demand ever enter `denial_value`? — **open, deliberately not done**

The tempting v2: `denial_value` already means "a rival needs him too" and
currently *infers* it from rosters, while trending, ownership and failed
claims would *measure* it. Not done in v1 because it is a valuation change and
**permanently ungradeable backwards** — Sleeper's trending and ownership
endpoints have no historical archive, so no replay of any past season can
reconstruct them (B16 item 2). A valuation version could only ever be graded
forward.

### W7 — does Sleeper's transactions endpoint expose pending claims? — **unverified, one request answers it**

The cheapest open question here, and it decides whether the league's own
demand signal is a Tuesday input or a Wednesday retrospective. `ffbot/demand.py`
is built to record the answer at runtime either way — a pending row, if one
ever appears, is treated as a live league-specific signal and noted once, the
same way `weekly_run_time_et` learns the real run time from the log.

### W8 — wire the five intel dials, or retire them? — **wired 2026-09-16**

`usage_weight`, `momentum_weight`, `divergence_weight`, `volatility_weight`
and `upside_lean_weight` have never had an input in this repo's history (see
the 2026-09-16 entry, defect 3), and each carries a GUI slider implying it
does something. Either a source writes them — W5, or `research-week.md`
starts asking for the five keys — or they go to 0.0 and the sliders come off,
following `game_script_weight`'s retirement precedent. Note that *making them
live is itself an ungraded valuation change*: 0.45 of combined weight that has
never fired, and weights selected by backtests fed from stats-derived signals
rather than from research prose. "Retire them" is a legitimate outcome and
should be decided on its merits, not deferred by default.

**This is not housekeeping (added 2026-09-16, after auditing SPICE.md).** The
shipped weekly baseline is validated as a BUNDLE -- train 2021-2023 +0.369
[+0.08, +0.66], held-out 2024 +0.360 [-0.02, +0.75], fresh 2025 +0.78 [+0.41,
+1.18]. But [SPICE.md](SPICE.md)'s own note on that 2025 run records that a
first pass without `--signals`, which structurally zeroes every trend-based
dial, "read as a near-null +0.04/+0.01 pts, confirming the significant result
above comes specifically from the trend signals firing, not from weather/Vegas
alone."

The dials `--signals` feeds are exactly the five that are inert in production
(`historical_form` -> volatility/upside, `usage_form` -> usage, `scoring_form`
-> momentum, `usage_divergence` -> divergence). So the configuration actually
running live is much closer to the near-null arm than to the arm that
measured +0.78. Nothing here is HARMFUL -- that is a separate and still-true
claim -- but the weekly ladder's measured edge has never been demonstrated
without these dials firing, and live they never have. W8 is therefore about
the largest known gap between what was validated and what is deployed, not
about tidying up five unused keys.

Two caveats kept deliberately: the without-signals comparison was reported
only for the 2025 run, which used `--source naive` (lower fidelity than ECR),
so no equivalent figure exists for the ECR train/holdout columns; and one run
is one run.

**Resolved: wired, not retired (2026-09-16).** The manager's call, on the
reasoning above -- these are the Validated dials, so the gap was between what
was measured and what was deployed, and closing it restores the measured
configuration rather than inventing a new one. `ffbot/live/form.py` computes
all five each run from Sleeper's own realized weekly stats; `ffbot/form.py`
holds the math, shared with `ffbot/history/signals.py` so the live and
historical feeds cannot drift. `kalshi_weight` stays OFF: it is classed
**Untested**, and the `use_untested_features` gate still forces it to 0.0.

Three things found in the wiring, all now pinned by tests:

1. **Ties manufactured a ranking.** `percentile_rank_within_position` spread
   identical values across 0-100 by dict order. With `min_games ==
   recent_games == 3`, EVERY player's trend in week 4 is identically 1.0, so
   the first week these dials ever spoke would have handed five identical
   players 0/25/50/75/100. Ties now share the average rank, which makes that
   week a uniform 50.0. This very slightly changes the historical providers
   too, and [SPICE.md](SPICE.md) records it.
2. **Three completed games are required**, so the five stay silent through
   week 3 of a season and speak from week 4. Not a defect; the coverage alert
   now says it in those words each run until then.
3. **The feed is Sleeper, not nflverse.** nflverse is the more literal
   transfer of what was validated, but its in-season latency is unverified and
   a feed that silently finds nothing is the exact failure being fixed.
   Sleeper is the same endpoint, cache and failure mode as every other live
   seam here, and the points half is scored by the league's own settings
   rather than approximated. The usage half derives WOPR one step earlier
   (per-player targets and air yards against team totals) instead of reading
   a precomputed column -- same definition, different arithmetic path.

What is NOT claimed: that this reproduces the +0.78. The live feed differs
from the measured one as above, and nothing has graded the live path. W5's
neutral-point question stays open and is now more visible, not less --
`usage_score(None)` is still 0.0 rather than 0.5, `USAGE_POSITIONS` is still
RB/WR/TE, and the partial-coverage alert reports the resulting cross-positional
effect every run.

---

## Not changing, so it is not re-litigated

**The Montgomery claim.** Correct, and the control case for all of the above:
`tests/test_gameplan.py::TestALegitimateClaimSurvivesEveryGuard` pins it. A
change that quiets the noise row by also dulling that one is a failed change,
not a tradeoff. That he reached the wire through another manager's error, and
that the user chose not to act on it, are facts about the league — not a reason
for the tool to have flagged him differently.

**The bottom-of-list priority stays permissive.** At priority 12/12 a slot is
worth ~0.18 points, so a +0.6 gain legitimately clears it, and
`test_hold_priority_is_reachable_at_the_shipped_default` asserts the refused
set is a prefix of the list rather than the whole of it. Guarding a gain that
small on its own merits is the noise floor's job, not the claim economics'.

**The `gain <= 0` bar itself.** `week_gain` is exactly 0.0 for anyone who does
not crack the starting lineup (`_week_score` sums starters only), and
`ros_gain = marginal_x - repl_marginal[pos]` is strictly negative at any
position where the lineup has a hole. So a bench-quality add is `<= 0` **by
construction**, not by mispricing. The 2026-09-16 fix is a *record* of the
filtering, not a removal of the filter, and the speculative surface exists
precisely so that record has somewhere to go. A future session reading defect
1 as licence to delete the bar has misread it.
