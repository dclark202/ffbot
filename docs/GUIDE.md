# Using ffbot: the web GUI, day to day

This assumes you've done step 1 in the [README](../README.md) — `ffbot` knows
your Sleeper league. Everything below happens in the browser at
`http://127.0.0.1:8321/` (`python scripts/gui.py`). Terminal commands and the
manual `roster.yml` route exist as backups — see [REFERENCE.md](REFERENCE.md)
— but the GUI is the intended way to use this tool.

**In season, you want the weekly pages** — [the weekly
manager](#the-weekly-page) and [hands-off
mode](#hands-off-mode-the-scheduled-task). [Draft day](#draft-day) and [Try it
before the season](#try-it-before-the-season) are a once-a-year read; they stay
here for next August.

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
  - A brief opponent strip — your live head-to-head opponent's name, both
    teams' projected scores on Sleeper's scale (so they match the app, with
    finished games counted at their real score), our adjusted total for you
    beside it, and their actual started lineup.
  - **Start/sit**: one card per swap (Start / Sit / Reason), computed on the
    roster AFTER the recommended free-agent adds below; a free agent you
    should grab and play reads **Add & start**, and names who to drop when
    your roster is full. A swap whose gain is inside the noise floor stays
    listed but is labelled **Toss-up** — it's the optimizer's pick, not a
    call worth agonizing over.
  - **Waiver claims**: players on waivers worth spending priority on, each
    with an "if it clears: …" line — what actually changes in your lineup if
    the claim is awarded.
  - **Add/drop**: only rows actually worth making, `<Position>: Add X —
    Drop Y (reason)`, each marked **ADD NOW** (a free agent — no priority
    spent) or **WAIT** (on waivers until it clears — from his kickoff until
    the league's weekly run, or for a couple of days after a drop), with a
    line saying where the waiver cycle stands. A row that displaced other
    candidates for the same slot carries them as backups, in order — one
    move per slot, never four defenses for one drop. Streaming a K/DEF need or denying a
    rival a player are *reasons* on an ordinary row, not separate
    categories. A move too small to mean anything — under the
    per-position noise floor, in points per week — isn't recommended; a
    note below says what was left out and by how much, so nothing is hidden
    silently.
  - A player whose game has already kicked off is locked in Sleeper, so the
    page never suggests dropping, benching or starting him.
  - Every row has a **Metrics** toggle holding the numbers the call was
    actually made on — both players' projections for this week, shown as
    "Sleeper 18.9 · ours 15.2 (weather: wind 40 mph −3.9)" so you can check
    it against the app and see exactly what we changed; the live or final
    score once his game has started; rest-of-season value, points scored
    so far, VOR, tier, ADP, ownership,
    researched upside/injury risk — plus a breakdown of *how the number was
    built*: how much of a claim's value is rest-of-season versus this week,
    what the drop cost, what waiver priority cost. That split is the useful
    part: a claim that's all this-week gain is a one-week rental, one that's
    all rest-of-season is a real roster upgrade, and they deserve different
    answers on whether to spend priority.
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

## Reviewing a week after the fact

Every run of the weekly page (and every scheduled check) writes a JSON
record to `weekly/reports/`, named `<season>-w<NN>-<source>.json`. It holds
exactly what the Recommendations panel showed and every metric behind each
row, plus both lineups, which live source produced each number, and the
config dials in force.

This exists because a week is genuinely unrecoverable once it passes. Live
projections move, the wire turns over, injury designations resolve — so
"why did it bench him in week 6" cannot be answered by re-running anything
later. The record is the answer.

One file per week per surface, overwritten: the browser page re-syncs every
five minutes, and a timestamped name would bury you in files. Scheduled
checks pass their own trigger name instead, so each pre-kickoff and
pre-waiver snapshot is kept separately — those are different decisions and
worth keeping apart. Pass `--no-week-log` to either entry point to turn it
off.

The terminal `week_report.py` prints the same metrics inline under each
recommendation; `--brief` gives you the old one-line-per-row output.

## The weekly rhythm

Run **`/gameday`** in Claude Code once a week, ahead of your lineup lock.
It researches the real schedule, injury designations, weather, and Vegas
lines for the week, writes them to `weekly/week-NN.yml`, and produces a
report — then that same research is what the GUI's weekly page reads for
its Weekly Intel panel and its weather/Vegas-adjusted numbers. Hit
**Refresh** on `/weekly` (or wait for the 5-minute soft sync) and the brief
you just researched shows up there. You read it, then set your own lineup
and waiver claims in the Sleeper app — the tool never does this for you.

If you've registered the scheduled task (next section) and turned on
`research:`, this happens automatically — research included — ahead of every
kickoff slot, before Tuesday's waiver check, and after Friday's final injury
designations, with nobody at the keyboard. Without research the checks still
run, on auto-fetched weather and odds alone.

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
schedule and fires whichever of these checks are due. Each has a purpose,
and its message is shaped by it:

- **Pre-kickoff** — one check per distinct kickoff window this week; kickoffs
  within half an hour of each other share a check, timed off the earlier one
  (a normal Sunday afternoon's 15:05 and 15:25 windows are one ping, while a
  London 09:30 or a December Saturday game keeps its own). (Thursday
  night, Sunday early/late/night, Monday night are typically five separate
  slots), starting about 80 minutes before each by default
  (`--lead-minutes`) — just after NFL inactives post, 90 minutes before
  kickoff — so its message lands about an hour out. Kickoff times come from
  the schedule in US Eastern and are converted to the machine's local time.
  Lineup first: the moves to make before that slot locks.
- **Waiver claims** — Tuesday evening by default (`autorun.waiver_weekday`
  and `waiver_hour` in `config.yml`; `--waiver-weekday`/`--waiver-hour`
  override them). After Monday night every player who played is on waivers
  until your league's weekly run, so this is the night to decide what to
  spend rolling priority on. The message is your priority, each claim worth
  making with its ordered backups (the other candidates for the same slot,
  queued behind it — Sleeper fails a later claim once the drop is spent),
  and what to leave for free agency. Never a lineup line: the first game is
  days away. Nothing worth a claim is said out loud, not left silent.
- **Free agents** — Wednesday morning by default (`autorun.post_waiver_*`),
  after the run: what Sleeper did with your claims, and which players not
  worth a claim are worth a free pickup now, each with where he starts.
- **Injury-report research** — Friday 5 PM local, research only (see
  "Research, unattended" below).
- **Projection grade** — Tuesday morning (`grade:` in `config.yml`): last
  week's projections against real points, adjustment by adjustment. It
  proposes a dial; it never moves one.

Each fired check writes a report to `reports/` (viewable in the GUI's
`/reports` page) and, if it found something actionable — a real lineup
move, a claim worth making, a free agent worth adding — sends a push
notification. A check that finds nothing still sends a short "all clear"
(see below), so a quiet phone never has to be read as good news. To
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

**The all-clear message.** With notifications on, every check that finds
nothing to do still pushes one message. A pre-kickoff check, about an hour
before the slot: which of your starters lock at that kickoff, your lineup's
projected total, the closest call it looked at and declined (say, a DEF swap
worth +0.2 points that isn't worth a waiver claim), and whether every live
data source answered. The waiver-claims check: no claim is worth your
priority, the closest call, and what to leave for free agency. The
free-agent check: what happened to your claims and that nothing is worth
adding. It exists so that silence means something: if a check's time comes
and goes with no message at all, the check didn't run — the machine was
asleep, or the task broke — and that's worth a look. Turn it off with
`notify.heartbeat: false`.

**Research, unattended.** Turn on `research.enabled` in `config.local.yml`
and each check researches before it reports: a headless Claude Code run of
`/research-week` that writes `weekly/week-NN.yml`, the same file `/gameday`
writes by hand.

| When | Pass | Why then |
|---|---|---|
| Before Tuesday's waiver check | Full: injuries, IR moves, role changes, the free agents being weighed | It feeds your waiver claims |
| Friday, 5 PM local | Full, research only | Final injury designations for the weekend post Friday afternoon |
| Each kickoff slot, ~80 min out | Quick: that slot's players — inactives, game-time calls, weather, lines | Inactives post 90 minutes before kickoff |

A slot pass is skipped when none of your players or candidates play in it.
Every message says whether research ran, and a failed pass is always
reported, even from a check that would otherwise stay quiet.

It needs the command-line Claude Code logged in once — run `claude` in a
terminal and `/login` — and it uses Claude usage on every run, about seven in
a normal week. What it may do is fenced in code, not just asked for in the
prompt: it can only search the web, read files, and write that one week file;
a `status` (which overrides Sleeper's) sticks only when its `source:` is an
nfl.com or official team-site URL, and is otherwise kept as a note; a write
that won't load is rolled back; and a slot pass never deletes another slot's
research. Running `/gameday` yourself still works and edits the same file.

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

The **P** column, next to Δ, answers the question a column of raw point
gaps doesn't: is there a standout here, or is this a toss-up? It reads each
row's Val as a noisy estimate and shows the chance that row is truly the
best pick, drawn as a bar against a faint tick marking a perfect
twenty-way split. Every bar sitting on the tick means it genuinely doesn't
matter much who you take; one bar towering over it means it does. The pill
above the table says the same thing in words ("1.1 live options of 20" in
round 1, "20.0" by round 8, on a real draft).

`draft.pick_confidence_scale` in `config.yml` (default 8.0, season points)
sets how much of a Val gap it takes to separate two picks — lower
concentrates the bars, higher flattens them, and 0.0 turns the column off.
It's a display calibration only: the number is a straight transform of Val,
so it can change how confident the ranking *looks* and nothing about the
ranking itself. Each pick's confidence is also stamped into the draft's
JSON tuning record, alongside how much probability the player you actually
took was holding — a sharper record of disagreement than a raw point gap,
since it's already scaled by how much was at stake.

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
- **You changed `my_slot`/`rounds` in Settings and nothing happened**:
  those need a server restart too. Tuning dials are the exception — a
  slider you save reaches an open draft room on its next poll, with your
  recorded picks intact; team count, draft order, and roster shape rebuild
  the draft state and are refused outright once a draft has picks.
- **You pasted a new league ID in Settings and draft sync didn't start**:
  sync is also set up once at startup — restart the server after changing
  connection settings.
- For anything else — config keys, error messages, the manual/CLI routes —
  see [REFERENCE.md](REFERENCE.md).

## Practising against bots (no Sleeper)

```bash
.venv/Scripts/python scripts/gui.py --mock --slot 4
```

The same draft room, with bots taking the other seats and picking instantly
— no Sleeper mock to join and nothing to wait for. Their picks appear in the
Draft Log and their rosters in the Opponents panel exactly as a synced
draft's would, and it writes the same `draft_log.jsonl` and
`draft/reports/*.json`, so `scripts/draft_report.py` and
`scripts/draft_counterfactual.py` work on a mock unchanged.

`--bot-spice N` sets how well the bots draft (default 1, VOR-chalk).
`--bot-window N` is how much they vary between runs (1 = deterministic).
`--seed N` reproduces a run exactly.

For bulk data rather than practice, `scripts/mock_draft.py --auto --runs 5`
drafts your seat too and produces five complete drafts, and their reports,
in seconds.
