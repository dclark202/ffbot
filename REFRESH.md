# Refresh plan — keeping the board honest as draft day approaches

Everything the assistant knows comes from files you refresh by hand: five CSVs
(projections + ADP), one researched intel file, and the exports pasted into
Yahoo. Each has a different shelf life. ADP and injury news move fastest —
they're the reason the big refresh happens **the day before the draft**, not a
week out.

`draft/intel.yml` records its own `generated:` date at the top. If that date is
more than ~4 days old on draft day, you're drafting on stale opinions.

---

## T-minus 1 week — light pass (~10 minutes)

Purpose: catch big moves early (trades, season-ending injuries, suspensions)
while there's still time to think about them.

1. Ask Claude Code:

   > Refresh the draft intel. Focus on: injury designations changing, camp
   > battles that have resolved, suspensions/holdouts, and anything flagged
   > `verify draft week` or `conflicting-reports` in draft/intel.yml.

   It re-runs the research, rewrites `draft/intel.yml`, and reports what changed.

2. Sanity-check coverage:

   ```bash
   .venv/Scripts/python scripts/intel_check.py
   ```

No CSV re-download needed yet unless your last one is >2 weeks old.

---

## T-minus 1 day — the full refresh (~30 minutes)

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

   This runs the whole researched pass: snapshots the current file
   (`scripts/intel_refresh.py --archive`), re-researches against the new board,
   rewrites `draft/intel.yml`, and reports a diff of exactly what changed
   (`--diff`). The same command serves in-season: `/intel-refresh week 6`
   researches your roster and the waiver wire instead of the draft board.

**3. Rebuild and verify:**

   ```bash
   .venv/Scripts/python scripts/intel_refresh.py
   ```

   One command: validates the intel file, loads the board (must print **no
   unmatched-intel warnings**), reports coverage, and regenerates every
   `draft/` export.

**4. Re-paste `draft/board.txt` into Yahoo's custom pre-draft rankings.**
   The old paste reflects the old board. This is the autopick safety net —
   if you disconnect on draft day, this list is what drafts for you.

**5. Two-round dry run** so nothing about the interface is a surprise:

   ```bash
   .venv/Scripts/python scripts/draft.py --slot 1
   ```

   Type a few names, `u` to undo them all, `q` to quit, then delete
   `draft_log.jsonl` so draft day starts clean.

---

## Draft day, ~1 hour before — news sweep only (~5 minutes)

Do **not** re-run the pipeline this close — a half-refreshed board is worse
than yesterday's complete one.

1. Ask Claude Code:

   > Quick pre-draft news sweep: any breaking injury, inactive, or suspension
   > news in the last 24 hours affecting players in the top 150 of
   > draft/intel.yml's board? Just tell me — don't rewrite anything.

2. For anything that broke, hand-edit the player's entry in
   `draft/intel.yml` (a one-line `note:` is enough) — or just remember it.
3. Yahoo reveals your slot shortly before the draft:

   ```bash
   .venv/Scripts/python scripts/draft.py --slot <N>
   ```

---

## What each artifact is for (so skipping steps is an informed choice)

| Artifact | Feeds | Goes stale because |
|---|---|---|
| `proj_*.csv` | every projection, VOR, tiers | camp performance, injuries |
| `adp.csv` | survival odds, arbitrage, market disagreement | the market reprices daily near draft season |
| `draft/intel.yml` | upside boosts + WHY column notes | camp battles resolve, designations land |
| `draft/board.txt` in Yahoo | autopick if you disconnect | it's a paste of the old board |
| `draft_log.jsonl` | `--resume` | leftover practice picks corrupt a real draft |
