---
description: Research this week's news/weather/matchups and produce the start/sit + waivers brief
---

Run the full weekly cycle: research `weekly/week-NN.yml`, then produce the brief via
`scripts/week_report.py`. Week: `$ARGUMENTS` (a number, e.g. "3"; if omitted, work out
the current NFL week from today's date and ask if ambiguous — never guess silently on
this one, since every downstream number depends on it).

See `INSEASON.md` for the full design and `roster.example.yml` / `weekly/week-NN.example.yml`
for the file shapes referenced below.

## Procedure

1. **Confirm `roster.yml` exists.** If not, stop and tell the user to copy
   `roster.example.yml` to `roster.yml` and fill in their real roster — don't invent one.

2. **Get the REAL schedule for this week — never assume Thu/Sun/Mon.** Search for the
   actual NFL schedule for this week: every game's real day and kickoff time in ET.
   This is not optional or approximate: Saturday games are routine from mid-December
   on, and international games (London, Munich, São Paulo) kick off as early as
   ~9:30am ET. A wrong assumption here means the lineup-lock timing is wrong, not just
   imprecise. Only bother recording games involving a team on the user's roster or a
   plausible waiver/streaming target.

3. **Research per player** (roster names from `roster.yml`, plus notable free agents if
   `--waivers`/`--stream` will be used):
   - **Official status/availability** — questionable/doubtful/out designations, PUP,
     suspensions, holdouts. These are facts: write them to `status:` (Yahoo-style code)
     in the weekly file, which moves the math.
   - **Weather** — a forecast close to kickoff (not a 5-day-out guess) for every
     *outdoor* stadium (check `data/stadiums.yml` — dome games don't need this).
     Wind mph and precipitation %.
   - **Vegas** — implied team totals for each game.
   - **Speculative color** (beat-writer reads, "trending toward playing," matchup
     narratives) — these become `note:`, and NEVER a `status`/risk-bearing field. Same
     rule as `/intel-refresh`: what's verifiable moves the number, what's speculative
     stays a note the human reads.
   Never trust prior-season knowledge for rosters, depth charts, or teams — verify
   against current sources.

4. **Write `weekly/week-NN.yml`** in the shape `weekly/week-NN.example.yml` shows:
   `week`, `generated`, `source_notes`, a `players:` entry per player with real news,
   a `games:` entry per relevant team with the *real* kickoff time and researched
   weather/Vegas numbers.

5. **Run the report:**
   ```bash
   .venv/Scripts/python scripts/week_report.py --week N --stream K DEF --waivers --faab <budget>
   ```
   (Ask the user for their remaining FAAB budget if `--waivers` is wanted and it's
   unknown — don't guess a number that sizes real bids.) If any roster names come back
   unmatched, that's a loud warning already printed to stderr — surface it to the user
   prominently, don't bury it.

6. **Present the brief.** Lead with the lineup section verbatim (or "no changes
   needed" when that's genuinely the answer — don't manufacture busywork). Then
   waivers/streaming if requested. Keep notes attached to the players they're about,
   exactly as the report shows them — don't paraphrase away the specific reasoning.

Report back: the lineup call, any WATCH-style contingencies worth flagging (a
questionable player whose status might still change), and the top 1-2 waiver/streaming
recommendations if requested.
