---
description: Research this week's news/weather/matchups and produce the start/sit + waivers brief
---

Run the full weekly cycle: research `weekly/week-NN.yml`, then produce the brief via
`scripts/week_report.py`. Week: `$ARGUMENTS` (a number, e.g. "3"; if omitted, work out
the current NFL week from today's date and ask if ambiguous — never guess silently on
this one, since every downstream number depends on it).

See `docs/GUIDE.md` for the full day-to-day design and `roster.example.yml` /
`weekly/week-NN.example.yml` for the file shapes referenced below.

## Procedure

1. **Confirm `roster.yml` exists, unless `config.yml`'s `roster_source.source` is
   `"sleeper"`.** Under the file route (the default), if `roster.yml` is missing, stop
   and tell the user to copy `roster.example.yml` to `roster.yml` and fill in their
   real roster — don't invent one. Under the live route, the roster comes straight
   from Sleeper each run; `roster.yml` is optional there and, if present, only
   contributes per-player flags (`undroppable`/`keeper_round`/`note`/`blocking`), never
   the roster's identity.

2. **Get the REAL schedule for this week — never assume Thu/Sun/Mon.** Search for the
   actual NFL schedule for this week: every game's real day and kickoff time in ET.
   This is not optional or approximate: Saturday games are routine from mid-December
   on, and international games (London, Munich, São Paulo) kick off as early as
   ~9:30am ET. A wrong assumption here means the lineup-lock timing is wrong, not just
   imprecise. Only bother recording games involving a team on the user's roster or a
   plausible waiver/streaming target.

   **Also confirm the actual venue for each of those games.** Most weeks every game is
   at the home team's usual stadium and nothing needs recording. But check for a
   neutral-site or international game (London, Munich, Frankfurt, Madrid, Mexico City,
   São Paulo, or a relocated game) — if one applies, set `venue:` to the matching key in
   `data/stadiums.yml` (e.g. `LONDON_TOT`) so weather/dome lookups check the real
   building instead of guessing from home/away, and set `international: true` so the
   (off-by-default, evidence-weak) `venue_disruption_weight` knob has something to act
   on if the user has turned it on. Add a stadium row to `data/stadiums.yml` first if
   the venue isn't already listed there.

3. **Research per player** (roster names from `roster.yml`, plus notable free agents if
   `--waivers`/`--stream` will be used):
   - **Official status/availability** — questionable/doubtful/out designations, PUP,
     suspensions, holdouts. These are facts: write them to `status:` in the weekly
     file, which moves the math. Under `roster_source: sleeper`, a live status is
     already pulled from Sleeper as the baseline — this step's job is to VERIFY it and
     write an entry only when your research disagrees or adds something Sleeper's feed
     doesn't have yet (a `weekly/week-NN.yml` entry always wins over the live value, so
     a fresher beat report can override a stale API field).
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
   weather/Vegas numbers. A game is written twice, once per team (mirrored) — carry
   `venue:`/`international:` on BOTH sides identically when step 2 found either applies,
   or the two teams will disagree about where they're playing. An optional one-line
   `note:` per game (also mirrored on both sides) is for genuinely game-level color
   worth surfacing on the GUI's matchup row — a shootout script, a short week, a
   backup QB starting — never a risk-bearing field.

5. **Run the report.** Waivers are always rolling-priority (no FAAB):
   ```bash
   .venv/Scripts/python scripts/week_report.py --week N --stream K DEF --waivers
   ```
   `--priority <N>` is optional — under `roster_source: sleeper` your real rolling
   waiver position is fetched live and used automatically; pass `--priority`
   explicitly only if you need to override it or the live fetch failed (a stderr
   note says which happened). Without either, it falls back to the cheapest/
   least-urgent assumption, which understates the real cost of spending a good
   priority position — ask the user for their priority if that fallback fired
   and it matters for the call at hand.

   If any roster names come back unmatched, that's a loud warning already printed to
   stderr — surface it to the user prominently, don't bury it.

6. **Present the brief.** Lead with the lineup section verbatim (or "no changes
   needed" when that's genuinely the answer — don't manufacture busywork). Then
   waivers/streaming if requested. Keep notes attached to the players they're about,
   exactly as the report shows them — don't paraphrase away the specific reasoning.

Report back: the lineup call, any WATCH-style contingencies worth flagging (a
questionable player whose status might still change), and the top 1-2 waiver/streaming
recommendations if requested. Mention that the brief is now live on the GUI's weekly
page too (`http://127.0.0.1:8321/weekly` reads the same `weekly/week-NN.yml` — hit
Refresh, or wait for the 5-minute soft sync).
