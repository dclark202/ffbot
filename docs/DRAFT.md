# Draft: preparing the board and running the assistant

Everything here works offline by default. Live Sleeper draft sync (see
[Live draft sync](#live-draft-sync) below) needs no credentials and no approval —
see [SETUP.md](SETUP.md) for the one-command league discovery, but nothing in the
core workflow below depends on it.

## Keeping the board honest as draft day approaches

Everything the assistant knows comes from files you refresh by hand: five CSVs
(projections + ADP), one researched intel file, and the export pasted into
Sleeper's pre-draft rankings. Each has a different shelf life. ADP and injury news move fastest — they're the
reason the big refresh happens **the day before the draft**, not a week out.
`draft/intel.yml` records its own `generated:` date at the top; more than ~4 days
old on draft day means you're drafting on stale opinions.

### T-minus 1 week — light pass (~10 minutes)

Catch big moves early (trades, season-ending injuries, suspensions) while there's
still time to think about them. Ask Claude Code:

> Refresh the draft intel. Focus on: injury designations changing, camp
> battles that have resolved, suspensions/holdouts, and anything flagged
> `verify draft week` or `conflicting-reports` in draft/intel.yml.

It re-runs `/intel-refresh`'s research, rewrites `draft/intel.yml`, and reports
what changed. Then sanity-check coverage:

```bash
.venv/Scripts/python scripts/intel_check.py
```

No CSV re-download needed yet unless your last one is more than two weeks old.

### T-minus 1 day — the full refresh (~30 minutes)

This is the one that matters. Do all of it, in order.

**1. Re-download all five CSVs** into `draft/`, same filenames, same settings
(PPR on the first two; the flex page is the one with the position column):

| Save as | Page |
|---|---|
| `draft/proj_flex.csv` | fantasypros.com/nfl/projections/flex.php?week=draft&scoring=PPR |
| `draft/proj_qb.csv` | fantasypros.com/nfl/projections/qb.php?week=draft |
| `draft/proj_k.csv` | fantasypros.com/nfl/projections/k.php?week=draft |
| `draft/proj_dst.csv` | fantasypros.com/nfl/projections/dst.php?week=draft |
| `draft/adp.csv` | fantasypros.com/nfl/adp/ppr-overall.php |

**2. Full intel refresh — one command in Claude Code:**

```
/intel-refresh
```

Snapshots the current file (`scripts/intel_refresh.py --archive`), re-researches
against the new board, rewrites `draft/intel.yml`, and reports a diff of exactly
what changed (`--diff`). The same command serves in-season: `/intel-refresh week
6` researches your roster and the waiver wire instead of the draft board.

**3. Rebuild and verify:**

```bash
.venv/Scripts/python scripts/intel_refresh.py
```

Validates the intel file, loads the board (must print **no unmatched-intel
warnings**), reports coverage, and regenerates every `draft/` export.

**4. Re-paste `draft/board.txt` into Sleeper's pre-draft rankings.** The old paste
reflects the old board. This is the autopick safety net — if you disconnect on
draft day, this list is what drafts for you. There is no API for this step; it's
a manual paste into Sleeper's web/app UI. `draft/board_by_pos.txt` is the same
list split by position, which is easier to enter by hand.

**5. Two-round dry run** so nothing about the interface is a surprise on the day:

```bash
.venv/Scripts/python scripts/draft.py --slot 1
```

Type a few names, `u` to undo them all, `q` to quit, then either delete
`draft_log.jsonl` or run `reset yes` so draft day starts clean.

### Draft day, ~1 hour before — news sweep only (~5 minutes)

Do **not** re-run the pipeline this close — a half-refreshed board is worse than
yesterday's complete one. Ask Claude Code:

> Quick pre-draft news sweep: any breaking injury, inactive, or suspension
> news in the last 24 hours affecting players in the top 150 of
> draft/intel.yml's board? Just tell me — don't rewrite anything.

For anything that broke, hand-edit the player's entry in `draft/intel.yml` (a
one-line `note:` is enough) — or just remember it. Sleeper reveals your slot
shortly before the draft (or check `draft_order`/`slot_to_roster_id` on the
draft object — `whoami.py`-adjacent, not currently automated into a flag):

```bash
.venv/Scripts/python scripts/draft.py --slot <N>
```

### What each artifact is for (so skipping steps is an informed choice)

| Artifact | Feeds | Goes stale because |
|---|---|---|
| `proj_*.csv` | every projection, VOR, tiers | camp performance, injuries |
| `adp.csv` | survival odds, arbitrage, market disagreement | the market reprices daily near draft season |
| `draft/intel.yml` | upside boosts + WHY column notes | camp battles resolve, designations land |
| `draft/board.txt` in Sleeper | autopick if you disconnect | it's a paste of the old board |
| `draft_log.jsonl` | `--resume` | leftover practice picks corrupt a real draft |

---

## Running the assistant

### Terminal

Sleeper randomizes draft position and tells you shortly before the draft. Once
you know it:

```bash
.venv/Scripts/python scripts/draft.py --slot 4
```

Put the terminal and Sleeper's draft room side by side. If the slot was wrong or
it changes, type `me 7` — no restart needed.

### Web GUI

```bash
.venv/Scripts/python scripts/gui.py --slot 4
```

Opens at `http://127.0.0.1:8321/draft` — the same recommendation engine and the
same `draft_log.jsonl`, so a session started in one interface resumes cleanly in
the other. Click a recommendation row to record it (auto-infers whose pick it is,
same as typing a bare name), or use the same command syntax in the text box.
Reset/save/load are buttons instead of typed commands (see below).

### If it crashes or closes by accident, nothing is lost

```bash
.venv/Scripts/python scripts/draft.py --slot 4 --resume
```

Every command you type is written to `draft_log.jsonl` as it happens, and
`--resume` replays it — in either the terminal or the GUI (`scripts/gui.py
--resume`), regardless of which interface originally recorded the picks.

### The one rule that matters

**Record every pick, including everyone else's.**

The assistant figures out whose turn it is by counting picks. Skip one and the
count drifts — from then on it thinks your picks belong to opponents and vice
versa, so your roster goes wrong and so does every recommendation.

If someone picks and you don't catch who it was, type `x` (or click nothing and
move on in the GUI — there's no click-equivalent for "unknown," type `x` in the
command box). That advances the count without claiming to know the player, which
keeps everything aligned.

### Commands

| Type this | What it does |
|---|---|
| `jefferson` | Records a pick — infers whose it is from the pick number |
| `*jefferson` | Explicitly **my** pick |
| `-jefferson` | Explicitly an **opponent's** pick |
| `x` | A pick happened, I don't know who — keeps the count right |
| `u` | Undo the last entry |
| `?jefferson` | Look a player up without drafting them |
| `s` | Cycle the sort: value → vor → adp → urgency → upside → edge |
| `p RB` | Show only RBs. `p` on its own clears the filter |
| `me 7` | Fix your draft slot |
| `1` `2` `3`… | Choose from the numbered list when a name is ambiguous |
| `reset` / `reset yes` | Archive the current draft and start fresh (confirms first) |
| `save <name>` / `load <name>` | Snapshot the current draft, or restore a saved one |
| `q` | Quit |

You only need partial names — `jeffer` is enough. If several players match
equally well you'll get a numbered menu; type the number, or click the row in the
GUI.

### Reading the table

```
#  PLAYER              POS TM  BYE     PROJ    VOR   NEED    VAL   ADP  SURV  WHY
1  Ja'Marr Chase       WR  CIN 6      336.1  146.1  146.1  197.2     3   73%  fills a need (+146.1)
```

| Column | Meaning |
|---|---|
| **PROJ** | Projected fantasy points for the whole season |
| **VOR** | Value over replacement — points above a freely-available player at that position. **This is the real measure of worth, not PROJ.** A 300-point QB can be worth less than a 250-point RB |
| **NEED** | What this player adds *to your specific roster right now*. Starts equal to VOR and falls toward zero as you fill a position |
| **VAL** | What the assistant actually ranks on: NEED plus a bonus for bench depth |
| **ADP** | Where this player typically goes. Much later than their rank = a bargain |
| **SURV** | Chance they're still available at your next pick. Low = take them now or lose them |
| **WHY** | Plain-English reason, including tier warnings and bye conflicts |

**Alerts** below the table flag positional runs, tier cliffs (a big point drop
coming), and bye-week collisions on your roster.

The short version: **take the player at the top of the list.** Deviate when the
WHY column or an alert gives you a reason to.

### Two things worth knowing before the clock is running

**Ambiguous names open a numbered menu — that's normal.** Unless a name matches
exactly one player, you get a list (with position, team, projection, and ADP) and
pick by number. `*chase` lists Chase Brown, Chase McLaughlin, *and* Ja'Marr Chase
— type `3`. Your `*`/`-` intent carries through to the number you pick. Only a
truly unambiguous query (one match, or an exact full name) records instantly.

**SURV shows 100% while you're on the clock.** That's correct, not a bug — it's
the chance a player survives until *your next pick*, and when that's the pick
you're making right now, the answer is trivially "yes." The column becomes
meaningful again as soon as someone else is picking.

---

## Settings

`draft.num_teams`, `draft.my_slot`, `draft.rounds`, `draft.order` (`snake` or
`linear`), `draft.position_caps`, `draft.position_targets`, and the full
`roster_positions` shape all live in `config.yml` (see [SETUP.md](SETUP.md)'s
configuration reference) and can be overridden per run:

```bash
.venv/Scripts/python scripts/draft.py --slot 4 --teams 10 --order linear --rounds 16
```

The GUI's Settings page (`http://127.0.0.1:8321/settings`) edits the same values
and writes them to `config.local.yml`, an overlay that never touches `config.yml`'s
own comments. Changes to team count, draft order, or roster shape are refused
once a draft has picks recorded — `reset` first (or a fresh draft that hasn't
started yet).

**Reset, save, load** work identically in the terminal (`reset`/`reset yes`,
`save <name>`, `load <name>`) and the GUI (buttons). `reset` archives the current
log to a timestamped file rather than deleting it. `save`/`load` snapshot to and
restore from `draft/saves/<name>.jsonl`, independent of the live log — useful for
a practice run you want to come back to, or comparing two draft strategies.

---

## Live draft sync

Works today, no credentials, no approval process — Sleeper's read API needs
neither. Two steps:

```bash
# 1. Reconcile the board against Sleeper's live players dump, building the id map
.venv/Scripts/python scripts/draft_export.py --board rankings.csv --reconcile

# 2. Run with sync on (needs sleeper.league_id set in config.yml -- see SETUP.md)
.venv/Scripts/python scripts/draft.py --slot 4 --sync
```

`--sync` resolves the current draft from `sleeper.league_id` automatically (or
pass `--draft-id` explicitly for a mock draft or a league with more than one
draft). Manual entry still wins over anything sync reports, so you can keep
typing and let sync fill the gaps. If setup fails for any reason — network,
config, an unresolvable draft — it prints a warning and continues offline; a
broken sync never takes down a working draft.

**Before draft day, prove it against something real, not just this doc's word
for it.** Run a Sleeper mock draft (or point `--sync` at a completed public
draft) and watch picks actually land in the terminal. `DraftSync` polls
Sleeper's `last_picked` timestamp and only re-fetches the full pick list when it
changes — cheap enough to poll every few seconds — but propagation latency in a
genuinely live draft room is the one thing worth confirming yourself rather than
trusting blind.

Even with sync fully working, typing picks in by hand is always fine as a
fallback: you have 11 opponent picks between your turns and roughly a minute
each to enter them.
