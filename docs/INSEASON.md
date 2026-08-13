# In-season: preparing and running the weekly manager

The draft assistant answers "who should I pick." This answers "who should I
start, who should I add, who should I drop" — every week. Same philosophy as the
draft assistant: **one ranked brief**, factual signals baked into the math,
speculative ones left as notes.

## The decision contract

Identical rule to the draft's intel layer, because it's the right rule twice:

> **What's verifiable moves the number. What's speculative stays a note.**

An official injury designation, a confirmed inactive, real weather at kickoff —
these multiply into the projection, because they're facts. A beat reporter's read
on a game plan, a "trending toward playing" — these are notes in the WHY column.
You see both, but the ranking itself can't be wrong because a hot take was wrong.

## What it produces today

One brief per run (`scripts/week_report.py`, or the GUI's Weekly Manager page),
built from whichever of these apply:

- **LINEUP** — the exact moves to make (or "no changes," which is a real, common
  answer), each with the point margin.
- **ROSTER status** — capacity, and the derived CORE/STREAM split (which bench
  spots are real depth vs. disposable).
- **STREAMING** (`--stream K DEF`) — ranked free-agent K/DEF by this week's
  matchup rather than season-long value.
- **WAIVERS** (`--waivers`) — ranked free-agent adds, each paired with who to
  drop and what to bid/claim, gated by the same drop/FAAB guardrails
  `ffbot/policy.py` enforces.
- **IR STASH** — researched IR-eligible free agents addable at zero bench cost.
- **DENIAL HOLDS** — free agents worth claiming purely to keep a rival from
  getting them (see [Tactical denial](#tactical-denial-weekly-opponent--standings-proximity) below); only appears when configured.

**Not yet built:** a WATCH-style section for pending decisions that resolve as
late news lands (e.g. "if X is inactive, start Y instead"), and a NEXT-WEEK
look-ahead beyond the bye-collision alerts the draft assistant already surfaces.
Both were part of the original design sketch; neither has a real implementation
today — this document used to claim otherwise. Don't rely on either existing.

## Architecture

```
roster.yml ──┐                          ┌─> LINEUP    (moves, margins)
sleeper API ─┴─> ffbot.models.Player ──┐ ├─> WAIVERS   (ranked adds + FAAB/claim + drop pairing)
              (roster_source: sleeper) │ ├─> STREAMING, IR STASH, DENIAL HOLDS
weekly + ROS projections ───────────────┤     (ros_gain/hold_margin/drop_cost real
  (projection_source: sleeper)          │      under projection_source: sleeper)
weather + vegas + status + notes ───────┴─> ffbot/week.py ─> lineup.optimize() ─> the brief
   (weekly/week-NN.yml, researched)
```

`ffbot/week.py` is the in-season analog of `ffbot/edge.py`: it takes your roster
and researched weekly intel, produces an *adjusted* set of `Player`s (status
overridden where intel says so, `projected_points` scaled by weather/Vegas/venue/
spice), and hands that straight to the same `lineup.optimize()` the draft
assistant already proved exact and deterministic. No new optimizer, no new
correctness risk — the only new code is *what feeds it*.

### The weekly signals, and why each is "outside standard rankings"

| Signal | Source | Why standard rankings mostly miss it |
|---|---|---|
| **Status overrides** | official designations — live from Sleeper (`roster_source: sleeper`), or researched into `weekly/week-NN.yml` as a base/override either way | a hand-researched entry always wins over the live value, so a Saturday beat report still beats a stale API field |
| **Weather** | forecast (wind/precip), matched to `data/stadiums.yml` | consensus rankings are largely weather-blind; 20mph wind at a dome-less stadium is a real, quantifiable discount most tools ignore until Sunday morning |
| **Vegas tilt** | implied team totals, researched | a 45-point shootout vs. a 34-point defensive slog changes every skill player's ceiling; standard rankings update slowly on this |
| **Venue/international** | researched, flagged in `weekly/week-NN.yml` | a neutral-site or international game means the usual home-stadium weather lookup is wrong, and (optionally, off by default) a small, evidence-weak "not a typical NFL setting" discount — see `season.venue_disruption_weight` in [SETUP.md](SETUP.md) |
| **Streaming K/DEF** | opponent implied total + matchup, researched | cheap, high-yield, and exactly the kind of grinding lookup a bot should do so you don't have to |
| **Waiver value** | rest-of-season VOR — a genuine live total under `projection_source: sleeper` (real weekly numbers summed forward, never counting an already-played week), the frozen board's own VOR otherwise — blended with this week's number | separates "hot right now" from "actually good the rest of the way" |
| **Ownership%** | live from Sleeper's ownership-research endpoint under `roster_source: sleeper`; `None` (inert) on the manual `roster.yml` route | activates `drops.protect_pct_owned`/`faab.min_pct_owned_to_bid`, both permanent no-ops without it |

### Kickoff times and venue are researched every week — never assumed

**A fixed Thursday/Sunday/Monday schedule is wrong often enough to matter.** From
mid-December on, Saturday games are routine. International games (London,
Munich, São Paulo) kick off as early as ~9:30am ET, sometimes at a stadium
neither team calls home. `/gameday`'s research step gets the real published
schedule and records `kickoff_et`, and — when applicable — `venue`/
`international`, in `weekly/week-NN.yml`'s `games:` section (see
`weekly/week-NN.example.yml`).

**What this data is used for today:** the venue override feeds
`week.is_dome_game` directly (so weather/dome lookups check the real building
for a neutral-site game, not whichever team is nominally "home"), and
`international` optionally feeds `venue_disruption_weight`. **What it does not
yet do:** `kickoff_et` and `config.yml`'s `lock_window_minutes` are recorded and
loaded but nothing currently reads them to decide "is this player still
changeable" — that lock-timing logic doesn't exist yet. Recording the real
kickoff time now means it's ready the day that logic is built; don't assume it's
already enforced.

## Roster sources — two routes, same engine downstream

Set `roster_source.source` in `config.yml` — everything downstream is unaffected
by which one you pick, because `week.py` was built against `models.Player`, the
same shape either route produces.

**1. `roster.yml` (`source: "file"`, the default) — works with no Sleeper account
at all.** Just player names (see `roster.example.yml`); the optimizer decides
slots, so you maintain a name list, not a lineup. Names are validated against the
board at load — a typo surfaces as an error immediately, not as a
silently-vanished player three weeks later.

**2. Live Sleeper roster (`source: "sleeper"`) — real names, live injury status,
live ownership%, one call, no auth.** `ffbot/sleeper_roster.py` fetches your
roster (`sleeper.league_id`/`roster_id`) and joins it against Sleeper's players
dump for identity. `roster.yml` stays relevant even here: it's read as an
optional per-player FLAG overlay (`undroppable`/`keeper_round`/`note`/
`blocking` — human judgment no platform can supply), merged onto the live roster
by name, never as the identity list itself. A hand-researched
`weekly/week-NN.yml` status entry still wins over the live value — see the
signals table above. A failed live fetch falls back to `"file"` for that run,
surfaced as an alert, never a crash.

## Running it

### `/gameday` (Claude Code command) — the full weekly cycle

Researches `weekly/week-NN.yml` (schedule, status, weather, Vegas, venue) and
then produces the brief. See `.claude/commands/gameday.md` for the exact
procedure; the short version is "ask Claude Code to run `/gameday`" and it
handles research, writing the file, and running the report.

### Terminal, once `weekly/week-NN.yml` exists

```bash
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers --faab 45
```

Rolling-priority leagues (not FAAB — check `league.yml`'s `waiver_type`) use
`--priority <N>` instead of `--faab`.

### Web GUI

```bash
.venv/Scripts/python scripts/gui.py
```

Opens at `http://127.0.0.1:8321/weekly`. Runs the identical report as the
terminal command (week number, stream positions, waivers, FAAB/priority as form
fields), and additionally provides:

- A **roster editor** — add/remove players, set `undroppable`/`keeper_round`/
  `acquired`/`note`/`blocking` flags, save straight to `roster.yml`.
- A **weekly-intel editor**, matchup-centric: one row per game (not per team) for
  kickoff time, wind, precipitation, Vegas totals, and venue/international flags
  — the underlying file stores each game twice, mirrored onto both teams, and
  the editor writes both sides from a single row so they can't drift apart.
  Player notes (status, note, risk, upside, volatility) get their own table.

Running the report is a what-if by default — nothing is written until you
explicitly click **Commit this lineup**, which persists the lineup state the
same way the terminal command does unless run with `--no-save-state`.

### Demo environment — rehearsing the GUI before Week 1

`roster.yml` and `weekly/week-NN.yml` are both gitignored and don't exist until
you have a real team, so there's normally no way to click through the Weekly
Manager page before your actual draft. `scripts/demo_season.py` builds a
throwaway, self-contained past season (a real drafted team, real rival
rosters, real weather/injuries/Vegas from nflverse) under `demo/<season>/`,
with a clock you can move to any date:

```bash
.venv/Scripts/python scripts/demo_season.py build --season 2025
.venv/Scripts/python scripts/demo_season.py goto 2025-10-12
.venv/Scripts/python scripts/demo_season.py serve --port 8322
```

Opens at `http://127.0.0.1:8322/weekly`, running against `demo/2025/` instead
of your real files — nothing under `demo/` ever touches the real
`roster.yml`/`weekly/`/`league.yml`. `goto <date>` resolves the date into
both a week number and a Wednesday/Friday/Sunday view of that week's injury
report (see `demo/<season>/README.md`, written by `build`, for exactly what
is and isn't faithful about the reconstruction). Rebuild any time with
`build` again; the whole directory is disposable.

## Tactical denial (weekly opponent + standings proximity)

Holding — or claiming — a player purely to deny a contending rival, never
because you'd start him yourself. Needs two optional inputs, both of which
degrade to an exact no-op if missing: `league_rosters.yml`
(`scripts/import_league_rosters.py` — every rival's actual roster) and
`league.yml`'s `teams:` standings section.

Three layered, individually-optional signals, all documented in
[SETUP.md](SETUP.md)'s configuration reference:

- **`denial_weight`** — the base signal: a rival's threat scales with how close
  their seed is to the playoff bubble.
- **`denial_opponent_boost`** — an extra boost for whoever `league.yml`'s
  `my_opponent` names as your head-to-head opponent this week. A specific,
  known threat to your own matchup outweighs an arbitrary rival's bubble
  distance.
- **`denial_seed_window`** — once the season reaches its playoff push (the
  final few weeks of `regular_season_weeks`), a further boost for any rival
  within this many seeds of your own (`my_team`'s seed) — a team fighting you
  directly for a seed is dangerous regardless of its distance from the bubble.

A rival threatening to grab an ordinary add first folds straight into that
add's WAIVERS `net` (an everyday reason to move now). A free agent worth
claiming *purely* to deny — never to start — shows up in its own separately
flagged DENIAL HOLDS section instead, since denial is an inference about other
humans' behavior, not a verifiable fact.

## Automation — the plan, not yet built

The eventual vehicle is a Claude Code scheduled routine running `/gameday` (most
of a weekly run is research — injury news, weather — which a bare cron job can't
do), on an illustrative cadence: a Tuesday-evening full waiver review, a
Thursday-game check when you have exposure, the big gameday run ~90 minutes
before your earliest kickoff, and later-window checks only when they matter.
None of this scheduling exists yet — every run today is manual, on demand.

## Milestones

- **M1 — manual baseline. Built.** `ffbot/week.py`, `ffbot/roster_source.py`,
  `scripts/week_report.py`, `/gameday`, and the web GUI's Weekly Manager page.
  Runs on demand, no Sleeper account needed, no standing schedule.
- **M2 — automation.** Wraps `/gameday` (and `/intel-refresh`) in a scheduled
  cadence. Not started — set up close to the season, once a real roster exists.
- **M3 — Sleeper API, read access. Built.** Live roster/status/ownership%
  (`roster_source: sleeper`), live weekly + rest-of-season projections
  (`projection_source: sleeper`), live rival rosters
  (`scripts/import_league_rosters.py --live`) — no approval process, unlike the
  Yahoo API this was originally scoped against; `roster.yml` is now genuinely
  optional, not just planned to be.
- **M4 — write access: not possible, permanently.** Sleeper's public API has no
  write capability at all — no lineup setting, no waiver claims, no draft picks.
  This isn't a gap to close later; it's a platform limit. The tool stays
  advisory: every action a human executes by hand in the Sleeper app. (The
  original Yahoo-scoped design had a real M4 plan here — reusing
  `LineupPlan.as_lineup_write()`, policy-gated waiver claims — kept in the code
  as documentation of the shape a write path would need, on the chance a future
  platform or a Sleeper write API someday makes it relevant again.)

## Trades — future work

Not designed or implemented. When it is, it should follow the existing
`ffbot/policy.py` pattern (a `Verdict`-returning function with the reason
surfaced, not a silent allow/deny) and value both sides of a proposed trade with
`draft.need` against each roster, the same marginal-value machinery waivers
already use. No timeline for this.
