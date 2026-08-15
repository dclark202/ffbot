# Using ffbot: the web GUI, day to day

This assumes you've done step 1 in the [README](../README.md) — `ffbot` knows
your Sleeper league. Everything below happens in the browser at
`http://127.0.0.1:8321/` (`python scripts/gui.py`). Terminal commands and the
manual `roster.yml` route exist as backups — see [REFERENCE.md](REFERENCE.md)
— but the GUI is the intended way to use this tool.

One rule underlies everything it tells you:

> **What's verifiable moves the number. What's speculative stays a note.**

An official injury designation, a confirmed inactive, real weather at
kickoff — these multiply into the projection, because they're facts. A beat
reporter's read on a game plan, a "trending toward playing" — these are
notes you read, not numbers the ranking trusts. The tool also has zero write
access to Sleeper — there's no lineup-setting, waiver-claim, or draft-pick
endpoint on Sleeper's public API. Every recommendation is executed by you,
in the Sleeper app.

## The weekly page

`http://127.0.0.1:8321/weekly` is a **read-only assistant landing page**,
not a form. There's nothing to fill in: it opens already loaded, pulling
the exact current state of your league.

- **Auto-loads the current week.** The week comes straight from Sleeper's
  own clock (or `league.yml`'s `week:` field on the manual route). The
  header strip's prev/next arrows let you look at a different week on
  demand, for that click only.
- **Refresh** bypasses every cache for one re-run — league state, rosters,
  the players dump, live projections, and auto-fetched weather/odds all
  refetch regardless of their normal TTL. Use it right after something
  changed in Sleeper (a claim processed, a status update landed) and you
  want the page to reflect it immediately.
- The page also **soft-syncs itself** every 5 minutes and whenever the
  browser tab regains focus, honoring Sleeper's normal cache TTLs (cheap,
  no forced refetch) — a "last synced" timestamp sits next to the Refresh
  button.
- **Recommendations** is first on the page, one coherent plan rather than
  separate lists:
  - A brief opponent strip — your live head-to-head opponent's name, this
    week's projected score, and their actual started lineup.
  - **Start/sit**: one line per swap (`K: Start Jim Bologna (CHI) — Bench
    Karl Marx (DEN)`), computed on the roster AFTER the recommended
    free-agent adds below.
  - **Waiver claims**: candidates worth spending priority on, each with an
    "if it clears: …" line — what actually changes in your lineup if the
    claim is awarded.
  - **Add/drop**: only rows actually worth making, `<Position>: Add X —
    Drop Y (reason)`. Streaming a K/DEF need or denying a rival a player
    are *reasons* on an ordinary row, not separate categories — they show
    up only when they clear the bar, never as a flat dump regardless of
    whether a pickup is warranted.
- **Alerts** sits just below Recommendations.
- **My team** shows the whole current roster — starters (with
  slot/opponent/kickoff), bench (with why each player is benched, when
  there's a specific reason), and IR. This is a read of the roster AS IT
  IS RIGHT NOW; it can legitimately disagree with Recommendations' post-
  pickup lines above (a bye-week kicker still shows as starting here while
  a streamer is recommended above it) — that's "what's true right now" vs.
  "what to do about it," not a bug.
- **Weekly intel** is read-only: researched player notes for your roster,
  then a matchup table (kickoff, wind, precipitation, Vegas totals, venue,
  a per-game note) — written by `/gameday`, never hand-edited in the
  browser. A "no intel for week N — run /gameday" hint appears when
  nothing's been researched yet.
- Header badges show where every number came from — projection source,
  roster source, the lineup baseline, your live waiver priority, and how
  many of the league's rosters are loaded.

## The weekly rhythm

Run **`/gameday`** in Claude Code once a week, ahead of your lineup lock.
It researches the real schedule, injury designations, weather, and Vegas
lines for the week, writes them to `weekly/week-NN.yml`, and produces a
report — then that same research is what the GUI's weekly page reads for
its Weekly Intel panel and its weather/Vegas-adjusted numbers. Hit
**Refresh** on `/weekly` (or wait for the 5-minute soft sync) and the brief
you just researched shows up there. You read it, then set your own lineup
and waiver claims in the Sleeper app — the tool never does this for you.

If you've registered the scheduled task (next section), this happens
automatically ahead of each kickoff slot even with nobody at the keyboard,
using auto-fetched weather/odds in place of a human research pass — a
human-run `/gameday` always wins outright over those when both exist.

## Hands-off mode: the scheduled task

`scripts/autorun.py` is a one-shot check: "is anything due right now?" It
needs something to actually call it on a schedule. `scripts/schedule_autorun.py`
sets that up:

```bash
python scripts/schedule_autorun.py register              # every 15 min, by default
python scripts/schedule_autorun.py status                 # is it registered, when did it last run
python scripts/schedule_autorun.py remove                  # tear it down
```

On Windows this registers a Task Scheduler entry; on macOS/Linux it prints
the equivalent `crontab` line instead (there's no Windows-only tool to wrap
there). Pass `--dry-run` to see the exact command before it runs anything.

Once registered, every ~15 minutes `autorun.py` checks the real NFL
schedule and fires two kinds of check when they're due:

- **Pre-kickoff** — one check per distinct kickoff slot this week (Thursday
  night, Sunday early/late/night, Monday night are typically five separate
  slots), firing 2 hours before each by default (`--lead-minutes`).
- **Pre-waiver** — one check per week, on a configurable weekday/hour
  (`--waiver-weekday`/`--waiver-hour`) ahead of your league's rolling-
  priority waiver processing — **adjust this to match your league's actual
  processing time**, e.g. `schedule_autorun.py register -- --waiver-weekday
  wed --waiver-hour 21`.

Each fired check writes a report to `reports/` (viewable in the GUI's
`/reports` page) and, if it found something actionable — a real lineup
move, or a waiver candidate worth a claim — sends a push notification. To
turn that on, install the free [ntfy](https://ntfy.sh) app, pick a private
random topic name (it doubles as the secret), subscribe to it in the app,
then add both lines to `config.local.yml` (never `config.yml` — this repo
is a public template, and a real topic name should never be committed):

```yaml
notify:
  channel: ntfy
  ntfy_topic: ffbot-a1b2c3d4-waivers
```

`channel: toast` fires a local Windows notification instead (no phone, only
seen if you're at the machine); `channel: both` does both. The machine
running the scheduled task needs to actually be on and awake for a check to
fire — this isn't a cloud service.

A scheduled run has no research step of its own (a scheduled task can't
read injury news), so it relies on auto-fetched weather (Open-Meteo) and
odds (Kalshi) to fill in what `/gameday` would otherwise research by hand.
Run `/gameday` yourself when you can — it always wins over the auto-fetched
numbers.

## Draft day

### Before the draft

The board goes stale fastest right before the draft, so the big refresh
happens the day before, not a week out:

- **T-minus 1 week** (~10 min): ask Claude Code to refresh the draft
  intel, focused on injury designations, resolved camp battles, and
  anything flagged `verify draft week` — this reruns `/intel-refresh`.
- **T-minus 1 day** (~30 min): re-download the five FantasyPros CSVs (see
  the README's step 2), run `/intel-refresh` again for a full pass, then
  `python scripts/intel_refresh.py` to rebuild every `draft/` export and
  confirm zero unmatched-intel warnings. **Re-paste `draft/board.txt` into
  Sleeper's pre-draft rankings** — this is your autopick safety net if you
  disconnect on draft day; there's no API for this step, it's a manual
  paste into Sleeper's own UI.
- **Draft day, ~1 hour before**: a quick news sweep only — ask Claude Code
  for any breaking injury/inactive/suspension news in the last 24 hours.
  Don't re-run the full pipeline this close; a half-refreshed board is
  worse than yesterday's complete one.

### In the draft room

```bash
python scripts/gui.py --slot 4
```

Opens at `http://127.0.0.1:8321/draft`. Live pick sync from your real
Sleeper draft is **on by default** (no credentials, no approval needed) —
once `draft/sleeper_ids.json` exists (built automatically once you have a
board; see the README's step 2), picks made in Sleeper appear here within
about 10 seconds, no typing required. A status pill shows whether sync is
live; the page keeps itself current on its own while it's open, and a
**Refresh** button next to the pill pulls immediately if you don't want to
wait. Manual entry always wins over sync, so you can keep typing and let
sync fill any gaps — the page holds off polling while you're actively typing
in the search box, so it won't yank focus or close the dropdown mid-name;
Refresh is there for that moment too. Polling does **not** stop when the
window is in the background, which matters if you keep ffbot and Sleeper
side by side — but browsers clamp background timers to roughly once a
minute no matter what the interval says, so a window that's been buried
catches up the moment it's in front again rather than on the dot. Changing
`draft.gui_poll_seconds` in `config.yml` (default 10) sets the interval. Pass `--draft-id` to sync a different
draft than `sleeper.league_id`'s own — the way to [rehearse against a
Sleeper mock draft](#rehearse-with-a-sleeper-mock-draft) before the real
thing.

Click a recommendation row to record a pick (auto-infers whose turn it is),
or type a name in the search box — partial names work (`jeffer` is
enough). If someone picks and sync doesn't catch it, type `x` to keep the
pick count aligned without claiming to know who it was; a drifted count is
the one thing that makes every later recommendation wrong, since the tool
figures out whose turn it is by counting.

The table's columns: **PROJ** (season points), **VOR** (value over
replacement — the real measure of worth, not PROJ), **NEED** (what this
player adds to *your* roster right now), **VAL** (what the assistant
actually ranks on), **ADP**, **SURV** (chance they survive to your next
pick), **WHY** (plain-English reason). Alerts above the table are `RUN`
(opponents are running a position), `WAIT` (what passing on a position
until your next turn costs, in points), and `BYE` (a bye-week hole your
own roster would have). The short version: take the player at the top of
the list; deviate when WHY or an alert gives you a reason to.

Reset/save/load are buttons on the page. `reset` archives the current draft
and starts fresh; `save`/`load` snapshot to and restore a named draft,
useful for a practice run. If the browser tab closes or crashes, nothing is
lost — every pick is logged as it happens and replayed on reopen.

## Try it before the season

`roster.yml` and `weekly/week-NN.yml` don't exist until you have a real
team, so there's normally no way to click through the weekly page before
your actual draft. `scripts/demo_season.py` builds a throwaway, self-
contained past season (a real drafted team, real rival rosters, real
weather/injuries/Vegas from historical NFL data) you can move a clock
through:

```bash
python scripts/demo_season.py build --season 2025
python scripts/demo_season.py goto 2025-10-12
python scripts/demo_season.py serve --port 8322
```

Opens at `http://127.0.0.1:8322/weekly`, running entirely against
`demo/2025/` — nothing here touches your real files. This is a good way to
see what the GUI looks like, but it's a **replay with every live switch
turned off** (frozen projections, no live Sleeper fetch), the opposite of
how the real thing runs — don't take its numbers as a preview of live
behavior, just the layout.

### Rehearse with a Sleeper mock draft

The draft room has no equivalent frozen replay, but it doesn't need one —
Sleeper's mock drafts are real, unauthenticated drafts with a real (if fast)
clock, and `--draft-id` already lets sync follow any draft, not just
`sleeper.league_id`'s own. Create a mock in Sleeper matching your league's
team count and rounds, copy the draft id out of its URL
(`sleeper.com/draft/nfl/<draft_id>`), then:

```bash
python scripts/gui.py --draft-id <mock_draft_id> --slot 7 --log draft/mock_log.jsonl
```

A separate `--log` keeps the rehearsal out of your real `draft_log.jsonl`.
The assistant notices the mock isn't your real league (its picks carry no
relationship to `sleeper.roster_id`) and switches ownership inference to
plain pick-number math instead — exact for a mock, since there are no
trades to throw it off. The sync pill explains this and, if you left off
`--slot`, resolves it from the mock's own draft order — pass `--slot`
explicitly if it can't (no `sleeper.username` configured, or you're not in
the mock's draft order yet).

Mock drafts often run a 30-second clock — fast, but real practice for
reading the recommendation panel under actual time pressure, not just
clicking through it at your own pace.

## When something looks odd

- **The draft room says no board is loaded**: you haven't downloaded the
  five FantasyPros CSVs yet (README step 2) — the weekly page still works
  fully without them. Download them, then **restart the server**; the GUI
  builds its draft state once at startup and won't pick up new files
  without a restart.
- **You changed `my_slot`/`rounds`/`spice_level` in Settings and nothing
  happened**: those need a server restart too — only team count, draft
  order, and roster shape rebuild the draft state live.
- **You pasted a new league ID in Settings and draft sync didn't start**:
  sync is also set up once at startup — restart the server after changing
  connection settings.
- For anything else — config keys, error messages, the manual/CLI routes —
  see [REFERENCE.md](REFERENCE.md).
