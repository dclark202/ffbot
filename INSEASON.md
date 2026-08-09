# The in-season manager

This is the second half of what `CLAUDE.md` calls "not yet built": the runner that
ties fetch → optimize → policy → write together during the season, and the schedule
that runs it automatically. The draft assistant answers "who should I pick"; this
answers "who should I start, who should I drop, who should I claim" — every week,
without you having to remember to ask.

Same philosophy as the draft assistant: **one ranked brief**, factual signals baked
into the math, speculative ones left as notes, spice where the market is wrong and
consensus (proven wrong) where it isn't. You said you'll likely just take what it
offers, so the bar is that the top recommendation should be right often enough that
doing exactly that is a good season-long strategy.

---

## What it produces

One brief per run, built from whichever of these apply that week:

- **LINEUP** — the exact moves to make (or "no changes," which is a real, common
  answer), each with the point margin and why.
- **WAIVERS** — ranked free-agent adds, each paired with who to drop for them and
  what to bid, gated by the same drop/FAAB guardrails `ffbot/policy.py` already
  enforces.
- **WATCH** — pending decisions where the answer depends on news not in yet: "if
  Waddle is inactive, start Shaheed instead." These resolve automatically as the
  actual inactive/status reports land closer to kickoff.
- **NEXT WEEK** — bye holes and early adds worth grabbing before everyone else
  notices, using the same lookahead machinery as the draft's bye-collision alerts.

## The decision contract

Identical rule to the draft's intel layer, because it's the right rule twice:

> **What's verifiable moves the number. What's speculative stays a note.**

An official injury designation, a confirmed inactive, real weather at kickoff — these
multiply into the projection, because they're facts. A beat reporter's read on a
game plan, a "trending toward playing" — these are notes in the WHY column. You see
both, but the ranking itself can't be wrong because a hot take was wrong.

---

## Architecture

```
roster.yml ──┐                          ┌─> LINEUP    (moves, margins, WHY)
chrome sync ─┼─> ffbot.models.Player ──┐ ├─> WAIVERS   (ranked adds + FAAB + drop pairing)
yahoo API ───┘        (M3)             │ ├─> WATCH     (pending inactives, weather branches)
                                        │ └─> NEXT WEEK (bye holes, early adds)
weekly projections ─────────────────────┤
weather + vegas + status + notes ───────┴─> ffbot/week.py ─> lineup.optimize() ─> the brief
   (weekly/week-NN.yml, researched)
```

`ffbot/week.py` is the in-season analog of `ffbot/edge.py`: it takes your roster and
researched weekly intel, produces an *adjusted* set of `Player`s (status overridden
where intel says so, `projected_points` scaled by weather/vegas/spice), and hands
that straight to the same `lineup.optimize()` the draft assistant already proved
exact and deterministic. No new optimizer, no new correctness risk — the only new
code is *what feeds it*.

### The weekly signals, and why each is "outside standard rankings"

| Signal | Source | Why Yahoo/ESPN mostly miss it |
|---|---|---|
| **Status overrides** | official designations, researched | Yahoo has this too, but only once the API works — for now, researched |
| **Weather** | free keyless weather API (wind/precip), matched to `data/stadiums.yml` | consensus rankings are largely weather-blind; 20mph wind at a dome-less stadium is a real, quantifiable discount on deep passing and kicking that most tools ignore until Sunday morning |
| **Vegas tilt** | implied team totals, researched | a 45-point Thursday shootout vs. a 34-point defensive slog changes every skill player's ceiling; standard rankings update slowly on this |
| **Streaming K/DEF** | opponent implied total + matchup, researched | cheap, high-yield, and exactly the kind of grinding lookup a bot should do so you don't have to |
| **Waiver value** | rest-of-season projection VOR (same board machinery as the draft) blended with this week's number | separates "hot right now" from "actually good the rest of the way" |

### Kickoff times are fetched fresh every week — never assumed

Caught mid-build, and worth stating plainly: **a fixed Thursday/Sunday/Monday
schedule is wrong often enough to matter.** From mid-December on, Saturday games are
routine. International games (London, Munich/Berlin, São Paulo) kick off as early as
**~9:30am ET**. A system that assumes "Sunday games start at 1pm" will watch the
wrong clock and give you a lineup lock warning after the game already started.

So the weekly intel file carries a `games:` section — every relevant matchup's real
kickoff datetime in ET, sourced from that week's actual published schedule during
research, never guessed from day-of-week. `week.py`'s "is this player still
changeable" logic reads real kickoff times from this section, not a weekday
assumption. The scheduling cadence below is the illustrative default case; the actual
mechanism (M2) is a weekly look-ahead run that reads the real schedule and derives
that week's specific alert windows from it.

---

## Roster sources — three routes, same engine downstream

**1. `roster.yml` — the baseline. Works today, no Yahoo, no browser.**
Just player names (see `roster.example.yml`); the optimizer decides slots, so you
don't maintain a lineup, just a 15-name list. About two minutes of upkeep after a
trade or waiver move. Names are validated against the board at load — a typo
surfaces as an error immediately, not as a silently-vanished player three weeks
later.

**2. Chrome-assisted sync — optional, interactive-only.**
"Read my Yahoo roster page and update roster.yml" works in a live session where
you're driving the browser. It **cannot** be the backbone of the automated schedule:
a cloud-scheduled run has no browser session to read. Chrome stays a convenience for
keeping `roster.yml` in sync, not a dependency the automation needs.

**3. Yahoo API — once the Fantasy Sports scope is approved (M3).**
`ffbot/auth.py`'s `YahooSession` already exists and needs no changes. Once wired,
roster, status, FAAB balance, and free agents all arrive live — `roster.yml` becomes
unnecessary, but nothing downstream changes, because `week.py` was built against
`models.Player`, the same shape either path produces.

---

## Automation — Claude scheduled routines, not bare GitHub Actions

The original repo plan (see `CLAUDE.md`'s Project status) pointed at GitHub Actions
for the scheduled tick. That's still right for the eventual pure-write step (M4), but
**most of a weekly run is research** — injury news, weather, matchup notes — which is
LLM work a cron job can't do. So the vehicle is a Claude Code scheduled routine
running `/gameday`, which researches *and* computes in one pass.

Illustrative cadence (real per-week timing is schedule-derived, see above):

| When (ET) | Run | Does |
|---|---|---|
| Tue evening | Waiver run | full week review, ranked claims + FAAB bids + drop pairings, next-week bye lookahead, `/intel-refresh week N` |
| Thu evening | Thursday-game check | only if you have exposure in that game |
| **Game morning, ~90 min before your earliest kickoff** | **Gameday** | the big one — official inactives land close to kickoff; final weather; lineup locked for the early window; WATCH branches set for later games |
| Later kickoff windows | Window checks | only fire if you have exposure in that window |

Every run ends in a delivered brief — a notification, loud only when a change is
actually recommended. `config.yml`'s existing `lock_window_minutes: 45` stops the
system from flip-flopping a call right before kickoff. Until M4, every run is
recommendations-only, matching `dry_run: true` semantics — nothing touches Yahoo.

---

## Milestones

- **M1 — manual baseline.** Built now: `ffbot/week.py`, `ffbot/roster_source.py`,
  `scripts/week_report.py`, `/gameday`. Runs on demand, no Yahoo, no standing
  schedule. This is what makes the rest of this document real rather than aspirational.
- **M2 — automation.** Wraps `/gameday` (and `/intel-refresh`) in the scheduled
  cadence above. Set up with you present, close to the season, not now — a routine
  running against a roster that doesn't exist yet has nothing to do.
- **M2.5 — Chrome roster-sync assist** (optional, whenever it'd help).
- **M3 — Yahoo API, read access.** On approval: live roster/status/free-agents/FAAB
  through `ffbot/fetch.py`, same engine, `roster.yml` becomes optional.
- **M4 — Yahoo API, write access.** The original autonomous vision. Two weeks of
  `dry_run: true` logs you've read and agreed with, then a deliberate flip. Lineup
  writes reuse `LineupPlan.as_yahoo_changes()`, already shaped for exactly this;
  waiver claims stay policy-gated the same way drops always have been. Any new
  irreversible action gets the same `Verdict`-returning-function treatment as drops —
  per the invariant in `CLAUDE.md`, never a silent allow.

## What's genuinely new vs. reused

Reused untouched: `lineup.optimize()`, `LineupPlan.as_yahoo_changes()`,
`policy.can_drop/droppable/max_faab_bid/can_bid_on`, `config.dry_run` and
`lock_window_minutes`, the `/intel-refresh` skill (already `week N`-aware), the
board's VOR machinery for rest-of-season waiver value, `names.py` matching,
`auth.YahooSession` untouched for M3.

New for this iteration: `ffbot/week.py` (weather/vegas/spice adjustments, streaming,
waiver ranking), `ffbot/roster_source.py` (`roster.yml` loading + validation),
`data/stadiums.yml` (roof type, for the weather gate), the `games:`-aware kickoff
logic, `scripts/week_report.py`, `/gameday`.
