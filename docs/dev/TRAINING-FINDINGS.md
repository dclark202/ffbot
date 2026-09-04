# What human review has found, and what it queued

The standing results log for [TRAINING.md](TRAINING.md)'s training packs: one
section per review round, then the backtest cells those rounds queued and the
things they deliberately did **not** change.

The rule from TRAINING.md holds without exception here: **a finding in this
document is a hypothesis, not a change.** Anything that touches a weight goes
through `scripts/backtest_draft.py` first. What review is genuinely good at is
noticing that a number is *nonsense*, which is a different claim from a number
being *miscalibrated*, and needs a different burden of proof.

---

## Reading a round's numbers

Two traps, both of which review rounds 1 and 2 walked straight into.

**The agree rate is not comparable across packs.** It is a self-reported
button, and its usage drifts. Round 1 pressed "close" once in rounds 3-9;
round 2 pressed it six times and "disagree" sixteen, while its picks were
measurably *closer* to the engine's. Compare on `rank_in_table` and
`value_gap_to_top`, which are read off the frozen table.

**Two packs with different `--rounds-range` are not comparable at all.** Pass
`--rounds R3-9` to `scripts/training_report.py` and compare the same slice of
the draft, or the difference you measure is the sampling.

---

## Round 1 (`review-01`) — rounds 1-14, `--my-spice 2`, 6 drafts, seed 7

30 of 30 answered. Agreement was high at both ends of the draft and collapsed
in the middle: 3/4 matched in rounds 1-2 and 9/12 in rounds 10+, against 2/6 in
rounds 3-5 with a mean value gap of 10.2 points. Round 2 was built to zoom in
on exactly that band.

## Round 2 (`review-02`) — rounds 3-9, `--my-spice 3`, 10 drafts, seed 21

30 of 30 answered. Restricted to the rounds both packs cover, every measured
axis moved in the engine's favour:

| rounds 3-9 only | round 1 | round 2 |
| --- | --- | --- |
| reviewer's #1 == engine's #1 | 5/14 (36%) | 11/25 (44%) |
| reviewer's #1 within engine's top 3 | 10/14 (71%) | 22/25 (88%) |
| mean rank of reviewer's #1 | 2.9 | 2.2 |
| mean value gap to engine's #1 | 4.5 pts | 3.3 pts |

The raw agree rate reads 57% -> 17% and means nothing; see above.

**One caveat that changes how the round reads.** It was generated with
`--my-spice 3`, identical to `config.yml`'s own `draft.spice_level`, so the
partial rosters shown to the reviewer were drafted by the engine itself
(within `--bot-window 3`) rather than by a deliberately-different bot. That
contradicts TRAINING.md's stated design — but it makes the round *more*
informative, not less: every roster complaint in it is a verdict on the
engine's own roster construction. `scripts/make_training_pack.py` now warns
when the levels match, and `scripts/training_report.py` states which case a
pack is in.

---

## Finding 1 — the valuation goes flat in rounds 8-9 (the real one)

Across all seven round-8/9 situations (picks 88, 91, 94, 95, 97, 98, 103):

- `need` is exactly 0 for **every one of the 20 rows**, in all seven.
- `_depth_value` is `depth_weight * max(0, vor)`, and by then every remaining
  player is below starter replacement — so the depth column is **all zeros**.
- Top `value` is 0.33-0.42, and the spread from row 1 to row 6 is 0.09-0.21.
- `edge.decision_scale` therefore sits on its `_MIN_DECISION_SCALE` floor of
  1.0, shrinking every fraction-of-scale weight — `balance_weight`,
  `bye_collision_weight`, `upside_weight`, `block_weight` — to a couple of
  tenths of a point.
- `confidence.effective_options` reads **exactly 20.0 out of 20**: the engine's
  own instrumentation reports a literal twenty-way toss-up, seven times out of
  seven.

What survives is edge noise. Pick 98, the whole table by projection:

| engine rank | player | proj | vor | depth term | value |
| --- | --- | --- | --- | --- | --- |
| 1 | Deebo Samuel | 156 | −18.6 | 0.00 | 0.40 |
| 3 | Jordan Mason | 154 | −17.3 | 0.00 | 0.37 |
| 4 | Wan'Dale Robinson | **171** | −3.6 | 0.00 | 0.22 |
| 9 | Khalil Shakir | **171** | −3.6 | 0.00 | 0.18 |

Fifteen projected points, inverted, because the only term still awake is a
contrarian tiebreak.

**This bug is already diagnosed and already fixed in the codebase — the fix
just ships off.** `DraftConfig.bench_replacement_depth`'s docstring describes
this failure exactly (measured 2026-08-15 at pick 124 of a 14-round draft,
where "ALL 505 remaining candidates tied at exactly 0.00"), down to the
decision-scale collapse and `position_targets` becoming unsteerable. It
defaults to `0.0` and `config.yml` never sets it. Human review re-derived the
same symptom from the outside, four rounds earlier, with no view of any
internal state.

**One defect, four symptoms.** This also explains three things that look
separate:

1. The nonsense ordering above.
2. **`scarcity_weight` goes silent exactly when it matters.** `later` is
   computed from the same collapsed base values, so it is 0.0 for every
   position in all seven situations — the term that prices "can I get this
   later?" has no opinion precisely where the answer is "no, never."
3. **`position_targets` cannot steer anything.** BACKTEST.md's B8 already said
   this in as many words; it is the same collapse.
4. Finding 2, below.

## Finding 2 — an empty mandatory starting slot the engine will not fill

In five of those seven situations the roster was missing a mandatory starter,
and **zero players at that position appeared anywhere in the 20-row table**:

| pick | missing | what the reviewer said, paraphrased |
| --- | --- | --- |
| 94 | TE | said no tight end appeared in the list, and that is what he would take |
| 95 | QB | saw no quarterback offered, but called it a quarterback round |
| 97 | TE | said the roster needed a tight end by this point |
| 98 | QB | called a quarterback urgently needed |
| 103 | QB | preferred a quarterback over further backups elsewhere |

Five for five.

The mechanism is `board.replacement`, frozen at board-build time and never
updated as the draft empties the pool. At pick 98 the best available QB is
Patrick Mahomes (proj 275) against a frozen QB replacement of 283.7 — the
preseason QB12, drafted five rounds earlier. So `need` for the only player who
can fill an empty QB slot is **negative**, and he loses to twenty
sub-replacement bench darts.

`recommend()` already encodes the correct principle in its forced-fill guard
("an empty K slot scores a literal zero every week"), but only triggers it at
`my_remaining <= len(missing)` — rounds 13-14. At round 9 with six picks left
and one hole, it does nothing at all.

## Finding 3 — RB depth

Eight roster notes across the two rounds complain about RB shortage; none
complain about WR shortage. `position_targets` is symmetric (RB 5 / WR 5).
Because round 2's rosters were engine-drafted (see above), these are
complaints about the engine's own construction, which converges with the
7WR/1RB failure that motivated `scarcity_weight` in B8 — and with that
section's own "what it does not fix" note.

## Finding 4 — TE in round 3

The engine's #1 was the top TE at picks 26, 29 and 30 and its #2 at 25 and 27.
The reviewer rejected TE in round 3 every time and endorsed it in round 7. At
pick 65 he named the mechanism unprompted: the top tight end sits in a tier
whose next member is available later — which is `expected_best_later` in
plain English.

So the engine has the right term and it is losing: at pick 30 Loveland is
`need` 54.4 against `later` 65.2, and still ranks #1. The structural cause is
`need`'s one-slot bias — a single TE fills the entire TE requirement, so the
first TE's marginal beats a second RB's or a third WR's.

Lowest-confidence of the four findings, and the one most likely to be taste.

## Finding 5 — bye stacking

One clean instance: pick 76, a roster carrying McCaffrey, Montgomery and
Skattebo, all on bye week 8, all engine-recommended. `bye_collision_weight`
is a fraction of the same collapsing decision scale.

---

## The queue

**H3-H5 were unmeasurable until H1 landed** (H1 shipped 2026-08-31, so they are now runnable). Every one of them is a
fraction-of-`decision_scale` weight, and that scale is pinned to its 1.0 floor
in exactly the rounds they are meant to steer. Sweeping them today measures a
0.15-point tiebreak against noise. Order matters.

### H1 — `bench_replacement_depth` — **RUN 2026-08-31, SHIPPED AT 1.5**

Measured before the 2026-09-05 live draft. `scripts/backtest_draft.py`, this
dial alone against an otherwise-identical control, 2021-2023, 60 seeds/season
= 180 paired drafts per cell:

| depth | all drafts | 95% CI | drafts that differed |
| --- | --- | --- | --- |
| 1.25 | +6.46 | [−13.07, +25.11] | +9.38 (124/180) |
| **1.50** | **+7.78** | [−17.24, +20.59] | **+10.23 (137/180)** |
| 1.75 | +5.11 | [−17.62, +19.52] | +6.92 (133/180) |

**Positive at every value tried, no CI excluding zero.** Shipped at 1.5 under
the non-negative bar set below, which was registered in advance precisely so
it could not be rationalized afterwards. Be honest about what this is: a bug
fix adopted on a weak positive, an order of magnitude smaller than
`scarcity_weight`'s +73. The smooth peak at 1.5 is mild coherence evidence
rather than noise, and nothing stronger.

Two caveats worth carrying forward:

- **The harness cannot measure at 14 rounds.** `scripts/backtest_draft.py`
  floors rounds at 15 (`rounds = max(args.rounds, 15)`, deliberate — both
  sides must fill a comparable roster for oracle scoring), so `--rounds 14`
  is silently clamped. These are 15-round numbers, same basis as B8/B10.
- **The oracle scorer cannot see the real cost.** It plays the best lineup
  from whatever roster resulted, so it measures the player-selection
  improvement and is blind to the decision-quality one — a table that reads
  as twenty indistinguishable options is unusable to a human on a clock, and
  that is most of what this bug actually costs.

**What it actually buys: insurance, not points.** Fifteen auto-drafts on the
live board (5 slots x 3 seeds), scored on the **optimal starting lineup**
rather than the roster total — bench points are not points, and
`scripts/mock_draft.py`'s own `projected` figure sums all fourteen players,
which flatters a lopsided roster:

| | mean | min | stdev |
| --- | --- | --- | --- |
| off | 1894.2 | **1783.6** | 48.2 |
| on (1.5) | 1895.5 | 1846.9 | **33.2** |

The median draft is unchanged (+1.3). What changes is the tail: with the dial
**off**, one draft in fifteen produced `RB1/WR7/QB2/TE2` — a roster that
cannot fill its RB starting slots, the same construction failure B8
documented. With it **on**, no draft in fifteen produced an unfillable
lineup, the worst case rose 63 points, and spread fell by a third. Repeating
against chalk (spice-1) bots rather than spice-3 changed the roster shapes
but agreed on the direction: every catastrophic roster in either experiment
came from the control.

So the honest framing is **variance reduction on roster construction**, not a
scoring upgrade — and the backtest's +7.78 should not be quoted as a points
gain. Two things follow. First, the mean-based backtest was always going to
struggle to see this; a tail effect is exactly what a mean with a
±20-point CI hides. Second, an intervention whose value is insurance deserves
a lower bar than one claiming to add points, which is roughly the bar that
was set — by luck rather than by reasoning, so say so.

**Direct evidence on the mechanism.** Replaying the
review pack's own round-8/9 situations on the live board, off → on at 1.5:

| | off | on |
| --- | --- | --- |
| top row's value | 0.37 – 0.42 | 3.34 – 5.25 |
| spread, rows 1→6 | 0.05 – 0.18 | 1.67 – 2.73 |
| `effective_options` | 20.0 / 20 | 19.5 – 19.7 / 20 |

The base values are restored roughly tenfold and the ordering is driven by
real bench value again, promoting Stevenson / Price / Golden over
sub-replacement dart throws — the exact correction the review predicted.

**What it does not fix, and this matters.** `effective_options` barely moves,
because `pick_confidence_scale` is an absolute 8-point scale and a 2.4-point
spread across twenty rows is still genuinely flat. That is the *correct*
readout: those picks really are close. Nor does it fill an empty mandatory
starting slot — at pick 98 with no QB rostered the top row is still a
receiver. That remains H2 below, untouched.

### H1 (original write-up)

```bash
python scripts/backtest_draft.py --seasons 2021-2023 --seeds 60 \
    --agent-spice-level 3 --control-spice-level 3 \
    --isolate bench_replacement_depth=1.5
```

Sweep **1.25 / 1.5 / 1.75 only**. Do *not* sweep 2.0 or higher: derived from
review round 2's own board, RB bench replacement falls off a pool-shape cliff
there, and the asymmetry it creates has nothing to do with football.

| depth | QB | RB | WR | TE |
| --- | --- | --- | --- | --- |
| 1.0 (starter replacement) | 283.7 | 171.0 | 174.3 | 161.0 |
| 1.5 | 261.9 | 144.1 | 138.1 | 142.4 |
| 2.0 | 214.8 | **69.9** | 105.7 | 128.4 |
| 2.5 | 129.9 | 60.5 | 86.4 | 100.2 |

`experiments/README.md` records an earlier "inconclusive, CIs crossed zero"
result for this dial. Re-run it regardless: that verdict predates or coincides
with three separate bugs that specifically neutered board-derivation dials —
`_run_season` sharing one board between both sides, `historical_board`
bypassing `_finalize_board`, and `intel.apply_intel` dropping
`bench_replacement` when copying a `Board` (all three in BACKTEST.md's B9).

**Recommended bar, stated openly because it is a judgment call:** this is a fix
for a demonstrated defect, not a contrarian dial, so a **non-negative** result
(positive point estimate, CI containing zero) should be enough to ship. That
is a lower bar than `scarcity_covered_damping` faced, and deliberately so:
there the burden was to overturn a working default; here the burden is to show
that ordering twenty players by noise is not somehow better.

**Offline pre-check first, no network needed.** Replay review round 2's frozen
scenarios with the dial on and confirm the round-8/9 tables reorder sensibly
(Stevenson and Godwin above Deebo and Jordan Mason) and `effective_options`
comes off its 20.0 ceiling. `ffbot.training.read_pack` plus
`board.load_board_from_config` is the whole harness.

### H2 — the late-draft empty starting slot

Minimal and config-not-code: `DraftConfig.forced_fill_slack: int = 0`, widening
`recommend()`'s existing guard to `my_remaining <= len(missing) + slack`. `0`
stays an exact no-op. Sweep 0/2/4/6.

Explicitly *not* the first thing to try: recomputing replacement level against
who is actually still available is the more principled fix, but it changes
`need` on every path including the weekly `ros_board`, which is far too much
surface for a first measurement.

**This is a different claim from `scarcity_covered_damping`, which measured
harmful.** That dial preferred an empty starting slot in the *early* rounds,
where a later equivalent genuinely exists — which is exactly what it got
wrong. By round 9 the position is exhausted and `later` is exactly 0. Same
instinct, opposite situation; the sweep is the arbiter.

### H3 — RB/WR target asymmetry (after H1)

Sweep `position_targets` at RB 6 / WR 4 and RB 6 / WR 5, and re-sweep
`balance_weight` at 0.3 / 0.5 / 0.7.

### H4 — TE timing in round 3 (after H1)

Check first whether H1 moves it at all; it should not, since round-3 base
values are healthy. If it survives, test it as a `scarcity_weight` > 1.0 sweep
or a `depth_decay` interaction. **Do not invent a TE-specific dial.** Measure,
don't patch.

### H5 — `bye_collision_weight` at 0.30 (after H1)

Cheap, and one clean motivating instance.

---

## Not changing, so it is not re-litigated

**`scarcity_covered_damping` stays at 0.0.** The reviewer's round-3/4 instinct
to fill an empty starting slot is the same claim this dial makes, and it
measured harmful at −26.5 season points, CI [−38.5, −5.1] — a CI excluding
zero, the same standard that retired `arbitrage_weight`. Human review alone
does not reopen that. The *conflict* is the finding worth keeping: a
thoughtful drafter's read of these situations and the measured outcome over
180 paired drafts point in opposite directions, and that is a fact about how
much a single situation's intuition is worth.

**`rank_calibration` stays off.** The pack's only high-confidence call was Josh
Allen in round 3 (`p_best` 0.95, 1.4 effective options) and the reviewer
disagreed — as he had in round 1, unprompted: *"Never a QB in early rounds
though so Josh Allen is a big NO."* The engine already takes Allen at pick 27;
`rank_calibration` would move elite QBs earlier still (Allen's VOR 67.8 ->
122.6). B9 established that this harness structurally cannot grade the dial.
This is a second, independent reason to leave it off — recorded, not acted on.

**Anything resting on a single situation.** The
single-draft-evidence rule now has a second form: one *reviewer* situation is
also just a hypothesis. What earned Finding 1 its priority is not that one
pick looked wrong, it is that seven out of seven situations showed the same
structural collapse, and the codebase had already independently diagnosed it.

---

## What the trainer itself changed as a result

Six instrument fixes, all shipped, none of which touch a weight:

- **Verdict-independent metrics.** The report now prints a rank histogram and
  a "within the engine's top 3" rate next to the agree rate, plus a caution
  that the agree rate drifts. `--rounds R3-9` makes a like-for-like comparison
  one flag instead of hand arithmetic.
- **A "none of these" answer is now gradeable.** `web/train.html` asks for a
  position when the reviewer names nobody, and it reaches the positional-bias
  matrix. Both of round 2's "none" answers said "TE" in free text and neither
  reached a single table.
- **Packs record every valuation dial.** `_TUNING_FIELDS` was missing
  `scarcity_covered_damping` and `predictiveness_shrinkage_blend`, so a pack
  could not say whether they were on. `tests/test_draft_report.py` now pins the
  list against the dials that actually change a value — the fourth
  hand-maintained field list in this repo to go stale silently.
- **Re-grading is idempotent.** `training/feedback/*.jsonl` was append-only, so
  every re-run doubled it. Replace is now the default, `--append` the opt-in.
  (`training/feedback/<pack>-<reviewer>.jsonl` is a stale artifact of the
  old behaviour and can be deleted.)
- **`--reviewer`** overrides the blank name reviewers keep leaving in the file.
- **A `--my-spice` warning** when the level matches the config's own, plus a
  line in the report saying which case a pack is in.

---

## B12/B13 — the 2026-08-31 mock, and three dials that did not ship

A full 12-team Sleeper mock (draft `1401320250372317184`, 90-second clock,
the human taking the engine's top row every round) produced a roster whose
week 8 held **four starters on bye including the only quarterback**, with the
QB slot literally unfillable. The engine had gone nine rounds with zero QBs
while stacking bye 8, and at pick 115 — round 10, five picks left — it still
ranked a receiver above the only startable QB.

Two candidate fixes were swept against 2021-2023, 60 seeds/season. Neither
shipped.

**`bye_collision_weight` (live 0.15) — noise.**

| agent | vs control | all drafts | differed |
| --- | --- | --- | --- |
| 0.30 | 0.15 | **−5.28** [−10.98, +1.48] | −28.80 (33/180) |
| 0.50 | 0.15 | **+14.32** [−9.32, +40.81] | +28.01 (92/180) |

Non-monotonic with both CIs crossing zero. Turning a dial up should not flip
the sign; this is sampling noise, not a signal. Left at 0.15. Note the
override form: this dial is live in `config.yml`, so `--isolate` would have
compared against the dataclass default of 0.0 and measured "the term vs
nothing" rather than "more of it vs what we run" — the trap B8 documents.

**`balance_weight` (live 0.30) — positive, still below the bar.** +9.44 at
0.50 and +12.51 at 0.70, both CIs crossing zero. Mechanically it would
counteract the RB6/WR4 skew H1 introduced, which is exactly why it was
tempting — and exactly why it was declined. It is a tuning dial, not a bug
fix, so it faces the CI-excludes-zero standard that retired
`arbitrage_weight`; and shipping it would stack a second unmeasured change on
top of H1 to cancel H1's side effect, which is the compounding this project
has been burned by three times.

**`forced_fill_slack` (new, H2) — this harness structurally cannot grade it.**
Identical results at slack 1, 2 and 3: `+0.42` season pts, with the **same
single draft out of 180** differing in every cell. The dial barely fires here
for two compounding reasons: `backtest_draft.py` floors rounds at 15 against
the live league's 14, which removes the end-of-draft squeeze the guard
depends on; and `ffbot.history.board`'s ECR-derived board **overstates** QB
spread where the live Sleeper board understates it (B9), so quarterbacks are
drafted earlier and the empty-QB-slot failure this dial exists to fix
essentially does not occur in the sample.

Shipped **off** (`forced_fill_slack: 0`, an exact no-op) with the code and
its tests in place. This is the second dial after `rank_calibration` that
this harness cannot judge, and the reason is the same board-bias mismatch —
which is now a pattern worth naming rather than a one-off caveat. Grading it
needs either a harness that honours the real round count, or live draft
reports accumulated across seasons.

**What this leaves as the actual mitigation: the human.** Bye stacking and an
empty mandatory slot are both trivially visible to a person reading the
roster panel and both currently invisible to the ranking. Until one of the
above is measurable, that check belongs to the drafter, not the engine.
