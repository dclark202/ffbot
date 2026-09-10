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
| **Hypothesis** | A tuning claim. Ships OFF (a dial at its no-op value) until a backtest agrees. |
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

## The queue

### W1 — grade `noise_floor_weight` — blocked on harness work

See [BACKTEST.md](BACKTEST.md)'s **B15**. Gradeable on the ordinary-waiver path
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

### W4 — Kalshi forward-logging has never produced a file — **open**

[SPICE.md](SPICE.md) claims "Kalshi forward-logging is now live", and
`data/kalshi_log/` does not exist on disk. Week 1 of a live season is the first
real chance to grade that signal. Check whether the cause is
`use_untested_features` being off (the gate `kalshi_weight` sits behind) rather
than a bug.

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
