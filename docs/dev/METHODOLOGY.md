# Methodology: how a recommendation gets made

What actually happens between "projections come in" and "the tool tells you
to start this player, not that one" — for the draft assistant and the
weekly manager alike, since both are built on the same engine. No formulas;
see the linked source files if you want those. This is the doc to read
before deciding how far to push `spice_level`, or before trusting a
recommendation you don't understand.

## The pipeline, in order

1. **Projections come in.** For the draft board: FantasyPros CSV exports,
   optionally overlaid with live Sleeper season projections. For a live
   week: Sleeper's real weekly projections (or the frozen board, divided
   down to a per-week rate, if you're running offline). Either way you get
   one number per player: their expected fantasy points.

2. **Every projection is re-scored under *your* league's actual rules**
   (`league.yml`) — not generic PPR. Distance-tiered field goals, a
   points-allowed ladder for defenses, whatever your league's interception
   penalty is, TE-premium receptions if you have it. FantasyPros' exports
   are built under their own consensus scoring; this step is what makes a
   number in this tool's output match a number your league would actually
   award.

3. **The optimizer finds the exactly-optimal legal lineup.** This is the
   one piece of the system that isn't a heuristic: `ffbot/lineup.py` models
   "which players fill which slots" as a bipartite matching problem and
   solves it exactly (Kuhn's algorithm), so the lineup it returns is
   *provably* the highest-scoring legal lineup available — not a good
   guess. Ties break deterministically (by player name), and among
   equally-optimal lineups it prefers the one that changes the fewest slots
   from what you started last week, so it doesn't churn your roster for
   zero point gain.

4. **Value is measured as marginal points over a derived replacement
   level** — never a hand-assumed one. To value a draft pick or a waiver
   claim, the tool runs the optimizer over the *entire remaining pool* to
   figure out what a realistic replacement-level player at that position is
   actually worth in *your* league, given *your* roster shape. This is why
   the same "how much is this player worth" question gets a consistent
   answer whether you're asking it in round 3 of a draft or in week 9 on
   the waiver wire — it's the same engine both times.

5. **Spice signals adjust the margin.** Weather, Vegas implied totals,
   researched upside/risk, ADP disagreement, usage/scoring trend — all of
   it is expressed as a *fraction* of the point spread actually at stake in
   the decision in front of you, never a flat bonus. A round-1 draft
   decision has a 100+ point gap between the best player and a replacement;
   a round-12 decision has a 2-3 point gap. A fixed bonus would be
   invisible in the first case and would completely dominate the second, so
   every signal scales with the decision it's affecting. This is the
   `spice_level` dial — see the table below.

6. **Guardrails gate anything irreversible.** A lineup swap costs nothing
   to undo, so the optimizer runs unsupervised. A drop is different —
   another manager can claim the player the instant you cut them — so
   every drop goes through `ffbot/policy.py`'s explicit rules first:
   undroppable flags, a protected-players list, early-draft-round
   protection, high-ownership protection, and a refusal to drop someone
   who's only *temporarily* unavailable (bye week, questionable). Every
   rule is phrased as "would you regret this Monday?" Nothing here executes
   anything — the tool has no write access to Sleeper at all (see below) —
   but the same discipline applies to what it *recommends*.

## The spice ladder

`spice_level` is the one dial (1–4 as of the B7 rescale — was 1–5 before)
that controls how far the tool leans into signals beyond plain top-projected
consensus. It exists on both the weekly path (`season.spice_level`) and the
draft path (`draft.spice_level`), tuned separately but built to feel the
same at each level. Any individual weight a level sets can still be
hand-overridden in `config.yml` without losing the rest of that level's
shape. See [docs/dev/SPICE.md](SPICE.md) for the full feature-by-level matrix,
the evidence class behind every single dial, and the B7 audit's run results
— this section is just the summary. The user-facing description of what
each level actually does is [docs/REFERENCE.md](../REFERENCE.md#spice-levels).

| Level | Name | Weekly feel | Draft feel |
|---|---|---|---|
| **1** | Baseline | Blind highest-projected-points — no VOR-aware waivers, no blocking/denial, no bye planning, no outside data at all. | VOR-chalk: `recommend()` with every edge term at zero. Measured +123 season pts, 95% CI excluding zero, better than following blind ADP. |
| **2** | Tactician | VOR-aware waivers, tactical blocking/denial, bye-collision awareness turn on. Still no outside data (weather/Vegas/trend/Kalshi all 0). | Anti-over-stacking, tactical block, bye-collision awareness, roster-balance urgency turn on. |
| **3** | Sharp | **The default.** Every evidence-backed outside feature turns on — the one cell in this project's history validated on both train AND a held-out season. | Upside/risk/volatility research and pro-stacking start mattering — a real but still-cautious risk lean. |
| **4** | Use at your own risk | Every feature this repo has, including untested ones (per-player Kalshi odds, venue disruption). Deliberately contrarian/high-variance; excludes only CONFIRMED-harmful weights, not merely unproven ones. | Deeper risk tilt, Kalshi turns on, risk tolerance kicks in earlier. Same "use at your own risk" framing. |

Level 1 is an *exact* no-op on the weekly optimizer, not an approximation of
one — asserted directly in the test suite (`TestSpiceLevelOneIsControl`).
Level 3 is **the default and the recommended setting** — the only level ever
validated out-of-sample on a held-out season. Level 4 is deliberately not
mean-optimized past that point: its variance dials stop at the largest
value that still measured statistically neutral on train data, not the
largest value tested. If old configs used levels 1–5, see docs/dev/SPICE.md's
migration note — the semantics shifted, not just the range (old 3–4 map to
new 3, old 5 maps to new 4).

See `ffbot/config.py`'s `SPICE_PRESETS` / `DRAFT_SPICE_PRESETS` for the
exact numbers behind each level.

See [docs/GUIDE.md](../GUIDE.md) for how this actually gets used week to
week and on draft day.

## Honest caveats

This section is here because the tool's credibility depends on being clear
about what's actually been checked against real outcomes and what hasn't.

- **The weekly spice ladder has been backtested against real NFL seasons**
  and validated on a held-out year: level 3 clears statistical
  significance on both train and test data (the one dial in this project's
  history to hold up out of sample). Level 4's variance pair (B7) was
  re-tuned to the largest value that stayed statistically neutral on train
  — its point estimate is negative, its confidence interval still includes
  zero.
- **The draft ladder's structural terms (anti-stacking, blocking, bye
  awareness, roster balance) were isolation-swept in B7** — every one
  measured either an exact no-op or a wide, zero-crossing confidence
  interval at its shipped value; none is confirmed harmful, none is
  confirmed positive. Three of its other dials (researched upside, risk,
  and cross-site ADP volatility) are structurally unmeasurable by the
  historical replayer today — a live draft with real researched intel
  populates them; the backtest simply can't see them yet.
- **Weather and Vegas signals fire rarely** — most weeks, most games, most
  players are unaffected. Don't expect them to move every recommendation.
- **`denial_weight`** (holding a player to keep a rival from getting them)
  **has no backtest evidence either way** — it's a judgment call, not a
  validated one, live from level 2 up. Every denial/hand-off gain is priced
  against the wire's next-best-available player at that position (see
  `ffbot/denial.py`'s fungibility note) and capped at `denial_row_limit`
  (default 1) — a streamable position (K/DEF by default) can never produce
  a denial claim at all, since the wire refills it every week regardless of
  who holds today's best option.
- **`opponent_correlation_weight`** (discounting a same-team stack with your
  live head-to-head opponent's actual starters; rewarding your DEF facing
  their skill players) **has no backtest evidence either way** either — a
  live opponent's actual lineup is a live-league concept with no
  historical-replay equivalent, the same basis `denial_weight` is judgment-
  set on. 0.0 is an exact no-op and skips the live fetch entirely; see
  `ffbot/week.py`'s `opponent_overlap`.
- **Kalshi-market-derived signals are entirely ungraded** — Kalshi's NFL
  markets postdate this project's entire 2021–2024 backtest window, so
  there's no historical data to check them against. They only activate at
  `spice_level: 4`. A forward-logging hook now records a weekly market
  snapshot so a future season can finally grade this signal — see
  docs/dev/SPICE.md.
- **The tool has no write access to Sleeper at all.** Sleeper's public API
  is read-only — there is no lineup-setting endpoint, no waiver-claim
  endpoint, no draft-pick endpoint. Every recommendation is executed by a
  human, always. This isn't a safety feature bolted on top; it's a hard
  platform limit, and it means nothing here can act on your league without
  you.

See [docs/REFERENCE.md](../REFERENCE.md#data-sources) for the full
inventory of every data source behind a recommendation, and
[docs/dev/BACKTEST.md](BACKTEST.md) for the backtesting methodology itself
(data sources, statistics protocol, leakage protections) if you want to run
or extend it yourself.
