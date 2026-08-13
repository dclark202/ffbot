---
description: Re-run the player intel research and rewrite draft/intel.yml (draft mode, or weekly in-season)
---

Re-run the intel gathering process end to end. Mode: `$ARGUMENTS` (empty or
"draft" = pre-draft board research; "week N" = in-season weekly research).

## Procedure

1. **Snapshot first**: `.venv/Scripts/python scripts/intel_refresh.py --archive`

2. **Build the target list** (who to research):
   - Draft mode: `.venv/Scripts/python scripts/intel_check.py --missing` for the
     top-200 by ADP, plus every player already in `draft/intel.yml`.
   - Week mode: my roster (from the draft log or as pasted), plus notable free
     agents — same file, same format, notes about THIS week (matchup, weather,
     injury designations).

3. **Research via web search/fetch.** Source priorities:
   - Injury/availability facts: official designations, PUP/IR moves,
     suspensions, holdouts. These get `risk` scores (0-100).
   - Camp/role reports: beat writers, team-site depth charts, rookie buzz.
   - Vegas: implied team totals and movement vs. ADP.
   - Market: ADP movers, cross-site disagreement.
   Never trust prior-season knowledge for rosters or roles — verify against
   current sources; player-team truth comes from the board CSVs.

4. **Rewrite `draft/intel.yml`** completely (never append-only):
   - `generated:` today's date; `source_notes:` what was consulted.
   - **The factual/speculative line** (this is the contract):
     `risk` = verifiable availability only — suspension, PUP, no-timetable
     injury, holdout. `upside` = researched breakout case. `note` = everything
     speculative — rumors, opinions, vegas takes — short enough for a table row.
     Opinion must never get a `risk` score.
   - Prune entries whose situation resolved; keep notes current, not archival.

5. **Verify and report**:
   - `.venv/Scripts/python scripts/intel_refresh.py --diff` — summarize the
     changes for the user (added/removed/changed).
   - `.venv/Scripts/python scripts/intel_refresh.py` — must load with zero
     unmatched-name warnings and regenerates the draft/ exports.
   - Remind the user to re-paste `draft/board.txt` into Sleeper's pre-draft
     rankings (draft mode).

Report back: coverage numbers, the diff summary, and the 3-5 most
decision-relevant changes in plain English.
