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

One brief per run — `scripts/week_report.py` on the terminal, or the GUI's
Weekly Manager page, which auto-loads the current week and folds the same
sections into three groupings: **Recommendations** (start/sit, add/drop,
waiver claims), **My team** (starters/bench/IR, the whole roster — not just
starters), and **Weekly intel** (researched notes/matchups, read-only). Both
front ends build from whichever of these apply:

- **LINEUP** — the exact moves to make (or "no changes," which is a real, common
  answer), each with the point margin.
- **ROSTER status** — capacity, and the derived CORE/STREAM split (which bench
  spots are real depth vs. disposable).
- **STREAMING** (`--stream K DEF`) — ranked free-agent K/DEF by this week's
  matchup rather than season-long value.
- **WAIVERS** (`--waivers`) — ranked free-agent adds, each paired with who to
  drop and a rolling-priority claim verdict, gated by the same drop
  guardrails `ffbot/policy.py` enforces.
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
sleeper API ─┴─> ffbot.models.Player ──┐ ├─> WAIVERS   (ranked adds + priority claim + drop pairing)
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
| **Ownership%** | live from Sleeper's ownership-research endpoint under `roster_source: sleeper`; `None` (inert) on the manual `roster.yml` route | activates `drops.protect_pct_owned`, a permanent no-op without it |

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

**The lineup BASELINE follows the same split.** Under `roster_source: "file"`,
`weekly/lineup_state.yml` remembers each player's slot from the last run, so
the move list reads as real week-over-week changes rather than "everyone
moves off the bench" every time (`scripts/week_report.py` writes it after
every run unless `--no-save-state`; the GUI's older "Commit this lineup"
button did the same and is gone now — see below). Under `roster_source:
"sleeper"`, that file is skipped entirely: `sleeper_roster.starters_slot_map`
reads which slot each of your players ACTUALLY occupies in the Sleeper app
right now (via the roster's `starters`/`reserve` arrays, zipped against the
league's own ordered `roster_positions`), and that live lineup becomes the
baseline directly. A recommended move under `roster_source: sleeper` always
means "make this change in the Sleeper app" — never "the tool remembers you
already made it."

## Running it

### `/gameday` (Claude Code command) — the full weekly cycle

Researches `weekly/week-NN.yml` (schedule, status, weather, Vegas, venue) and
then produces the brief. See `.claude/commands/gameday.md` for the exact
procedure; the short version is "ask Claude Code to run `/gameday`" and it
handles research, writing the file, and running the report.

### Terminal, once `weekly/week-NN.yml` exists

```bash
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers --priority 6
```

`--priority` is optional under `roster_source: sleeper` — your real rolling
waiver position is fetched live from Sleeper and used automatically when you
leave it off; pass it explicitly only to override.

### Web GUI

```bash
.venv/Scripts/python scripts/gui.py
```

Opens at `http://127.0.0.1:8321/weekly` — a **read-only assistant landing
page**, not a form. There's nothing to fill in and nothing to edit: it opens
already loaded, pulling the exact current state of the league.

- **Auto-loads the current week.** Under `roster_source: sleeper`, the week
  comes straight from `SleeperClient.nfl_state()`; otherwise from
  `league.yml`'s own `week:` field. The header strip's prev/next arrows let
  you look at a different week on demand (this sets an explicit week for
  that click only — it doesn't change what "current" means next time you
  open the page).
- **Refresh** bypasses every Sleeper cache for one re-run — league state,
  rosters, the players dump, live projections, and auto-fetched
  weather/odds all refetch regardless of their normal TTL. Use it when
  something changed in Sleeper (a claim processed, a status update landed)
  and you want the page to reflect it immediately rather than waiting out
  the cache.
- **Recommendations**, grouped: start/sit moves, waiver claims (candidates
  whose priority cost is worth spending), and add/drop (everything else —
  hold-priority free agents, K/DEF streaming, IR-stash adds, denial holds).
- **My team** shows the WHOLE roster — starters with slot/opponent/kickoff,
  bench (with why each bench player is benched, when there's a specific
  reason), and IR — not just the starting lineup.
- **Weekly intel** is read-only: researched player notes for your own roster
  at the top, then a matchup table (kickoff, wind, precipitation, Vegas
  totals, venue, and a per-game note) — written by `/gameday`, never
  hand-edited from the browser. A "no intel for week N — run /gameday" hint
  appears when nothing's been researched yet.
- Header badges show where every number came from: `projection_source`,
  `roster_source`, the lineup-baseline `slots_source`, your live waiver
  priority, and how many of the league's rosters are loaded (and whether
  that came from a live fetch or the `league_rosters.yml` snapshot).

There is no roster editor and no weekly-intel editor in the GUI anymore —
`roster.yml`'s flags (`undroppable`/`keeper_round`/`note`/`blocking`) are a
plain text file, hand-edited same as `weekly/week-NN.yml`; `/gameday` is the
one thing that writes the latter. Nothing here writes a lineup baseline
either (the old "Commit this lineup" button is gone) — under
`roster_source: sleeper` the live Sleeper lineup already IS the baseline;
under the file route, `scripts/week_report.py` (without `--no-save-state`)
is still what persists `weekly/lineup_state.yml`.

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

## Automation — built, local, kickoff-aware

`scripts/autorun.py` is a one-shot trigger brain: run it every ~15 minutes from
Windows Task Scheduler and it decides whether anything is due right now, using
the real NFL schedule rather than a fixed clock time. Two trigger types, both
recomputed fresh from `ffbot.live.schedule` each poll:

- **Pre-kickoff** — one trigger per DISTINCT kickoff slot this week (Thursday
  night, Sunday early/late/night, Monday night are typically five separate
  slots, not one), firing `--lead-minutes` (default 120 = 2h) before each.
- **Pre-waiver** — one trigger per week, on a configurable weekday/hour
  (`--waiver-weekday`/`--waiver-hour`) ahead of this league's rolling-priority
  waiver processing — **adjust the default (Tuesday 8pm) to match this
  league's actual processing time.**

Idempotent via a small JSON state file (`data/autorun_state.json`, gitignored)
keyed on a stable trigger id, so a 15-minute poll never double-fires. A window
missed entirely (machine asleep/off) fires LATE within a grace period rather
than never; past that window it's simply skipped — a start/sit brief for a
game that already kicked off helps nobody. Each fired trigger runs the exact
`scripts/week_report.py run_report` pipeline with `--no-save-state` (an
unattended check can never poison next week's real lineup baseline) and
`--refresh` (the whole point of firing at a specific moment ahead of kickoff
or waivers is wanting the LATEST information right then, not whatever was
already cached from an earlier poll) — and writes
`reports/YYYY-wNN-<trigger>.md`, viewable in the web GUI's `/reports` page or
opened directly. `--waivers` defaults ON (pass `--no-waivers` to skip); it
was opt-in before this default flipped.

    python scripts/autorun.py --dry-run       # print this week's real trigger schedule, fire nothing, show the notify channel
    python scripts/autorun.py --priority 6    # forwarded to week_report.py for every fired run; --stream defaults to season.stream_positions

Registering the actual Windows Task Scheduler entry is a manual, one-time step
(a system-settings change, done by you, not by an agent) — see the exact
`schtasks` invocation in the session that built this.

Notes on `/gameday`'s weather/Vegas research specifically: a scheduled
`autorun.py` run has no research step of its own (a cron job can't read injury
news), so it relies on `game_conditions:`'s auto-fetched weather (Open-Meteo
forecast) and odds (Kalshi public markets) to fill `weekly/week-NN.yml`'s
`games:` block when nobody has run `/gameday` first — see `ffbot/live/`. A
human-run `/gameday` always wins outright over the auto-fetched numbers.

### Notifications — knowing when a fired trigger found something

Nobody watches a terminal for a Task-Scheduler poll, so a fired trigger that
produces something ACTIONABLE (a real lineup move, or a waiver candidate
flagged `CLAIM` — never a mere `HOLD PRIORITY`) sends a push notification
(`ffbot/notify.py`; `scripts/autorun.py`'s `actionable_summary` decides what
counts). `config.yml`'s `notify:` block:

- `channel: "ntfy"` (recommended) — a free, no-signup push to your phone via
  [ntfy.sh](https://ntfy.sh). Install the ntfy app, pick a private random
  topic name (it doubles as the secret, since the server is public — never
  commit a real one to `config.yml`), subscribe to it in the app, then set
  `notify.ntfy_topic` in **`config.local.yml`**. A self-hosted server works
  too — set `notify.ntfy_server`.
- `channel: "toast"` — a local Windows notification instead, no phone, no
  external service, only seen if you're at the machine when it fires.
- `channel: "both"` fans out to each; `"off"` (the default) is an exact
  no-op — `ffbot.notify.send` never even builds a request.
- `notify.min_waiver_net` — a `CLAIM`-worthy candidate only notifies when its
  `net` (season points) clears this; a week where nothing clears the bar to
  be worth a real claim stays quiet rather than buzzing for a marginal one.

A delivery failure prints to stderr and never un-marks the trigger as
fired — the written report is the artifact of record; a missed push is a
lesser problem than re-running (and re-notifying for) an already-completed
check.
