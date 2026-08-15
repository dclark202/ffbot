# ffbot

A local web GUI, live-synced to your Sleeper fantasy football league: a
draft room with real-time pick sync, and a weekly manager that opens
already showing your current roster, lineup, and waiver picture — no
setup step to run each week, no form to fill in. It's read-only against
Sleeper's public API (no write endpoint exists there at all), so it
recommends and you act, in the Sleeper app. Terminal tools and a manual
`roster.yml` file exist as last-resort backups — see
[docs/REFERENCE.md](docs/REFERENCE.md) — but the GUI, live-synced, is the
way this is meant to be used.

## Use this as a template

This repo is meant to be forked, not run in place — click **"Use this
template"** above (or `gh repo create --template <this-repo>`) to get a
clean copy with no history, one repo per season or per league. That keeps
each copy's draft log, board CSVs, and weekly reports naturally separate
from anyone else's.

## Setup — three steps

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on macOS/Linux
pytest                                            # optional: run the full test suite
```

### 1. Connect to your league

```bash
.venv/Scripts/python scripts/init_league.py --username <your-sleeper-username>
```

No credentials, no app registration, no approval wait — Sleeper's API is
public. This finds your league (or lists them, if you're in more than
one — pass `--league-id` to pick) and writes `config.local.yml` (your
`league_id`/`roster_id`) and `league.yml` (your league's real scoring
rules, translated from Sleeper) automatically. Then:

```bash
.venv/Scripts/python scripts/scoring_check.py   # verify the generated league.yml
.venv/Scripts/python scripts/gui.py             # http://127.0.0.1:8321/
```

Open the GUI and your live team is already there.

### 2. (Recommended) Download the FantasyPros CSVs

Five free exports, same filenames, into `draft/`:

| Save as | Page |
|---|---|
| `draft/proj_flex.csv` | fantasypros.com/nfl/projections/flex.php?week=draft&scoring=PPR |
| `draft/proj_qb.csv` | fantasypros.com/nfl/projections/qb.php?week=draft |
| `draft/proj_k.csv` | fantasypros.com/nfl/projections/k.php?week=draft |
| `draft/proj_dst.csv` | fantasypros.com/nfl/projections/dst.php?week=draft |
| `draft/adp.csv` | fantasypros.com/nfl/adp/ppr-overall.php |

These unlock the draft room (value-over-replacement rankings and tiers),
waiver-add valuation on the weekly page, and live draft-pick sync (re-run
`init_league.py`, or `scripts/draft_export.py --reconcile`, once they're
downloaded to build the id map sync needs). Without them, the weekly page
still runs start/sit fully from live Sleeper projections — this step is
what the draft side and waiver adds need, not a prerequisite for the
whole tool. Refresh these the day before your draft; see
[docs/GUIDE.md](docs/GUIDE.md#before-the-draft) for the full cadence.

### 3. Make it a habit

```bash
.venv/Scripts/python scripts/schedule_autorun.py register
```

Registers a recurring check (Windows Task Scheduler, or prints the
`crontab` equivalent elsewhere) that fires the weekly report ahead of each
kickoff slot and ahead of waivers, with nobody at the keyboard. Add phone
push notifications by setting `notify.channel`/`ntfy_topic` in
`config.local.yml`. And once a week, ahead of your lineup lock, ask Claude
Code to run **`/gameday`** — it researches the real schedule, injury
designations, weather, and Vegas lines, and that research shows up
directly on the GUI's weekly page. `/intel-refresh` is the same idea for
pre-draft player research. See [docs/GUIDE.md](docs/GUIDE.md) for the full
day-to-day rhythm.

## What's behind the recommendations

- Live weekly and rest-of-season projections from Sleeper, real component
  stat lines re-scored under your league's actual rules — not a frozen
  preseason estimate rescaled down
- Your live Sleeper roster (name, team, injury status, ownership%),
  merged with any per-player flags (`undroppable`/`keeper_round`/`note`/
  `blocking`) you keep in `roster.yml`
- Your league's actual scoring rules — auto-imported from Sleeper, or
  transcribed by hand into `league.yml` (points-allowed ladders,
  distance-tiered field goals, TE premium, whatever differs from generic
  PPR)
- FantasyPros projections, ADP, and rankings for the draft board, with
  Sleeper optionally overlaying live points on top while FantasyPros still
  supplies ADP, bye weeks, and cross-site ADP spread
- Researched player intel and weekly weather/Vegas/venue context that a
  Claude Code research pass (`/gameday`, `/intel-refresh`) writes, always
  winning over auto-fetched numbers when both exist
- Other teams' rosters and league standings, live from Sleeper, for
  waiver-pool exclusion and tactical denial (holding a player purely to
  keep a contending rival from getting them)

## Backups and manual routes

Everything above has an offline fallback: a hand-maintained `roster.yml`
name list instead of a live Sleeper roster, terminal tools
(`scripts/draft.py`, `scripts/week_report.py`) that share the exact same
engine and draft log as the GUI, and a frozen preseason board instead of
live projections. These exist so you're never stuck if something's
unreachable — see [docs/REFERENCE.md](docs/REFERENCE.md) for all of them.

## Docs

- **[docs/GUIDE.md](docs/GUIDE.md)** — using the GUI day to day: the
  weekly page, the weekly rhythm, the scheduled task, and draft day.
- **[docs/REFERENCE.md](docs/REFERENCE.md)** — the full `config.yml`/
  `league.yml` reference, the spice-level dial, manual/terminal routes,
  every data source, and troubleshooting.
- **[docs/dev/](docs/dev/)** — architecture history and the backtest
  evidence behind the spice ladder, for the curious; not needed to use
  the tool.
- **[CLAUDE.md](CLAUDE.md)** — architecture and design invariants, for
  anyone (human or Claude Code) working on the code itself.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how issues and PRs are handled.

## Status

Built and tested against the live Sleeper API: league discovery, live
draft-pick sync, live roster identity/status/ownership%, live weekly and
rest-of-season projections, live standings and rival rosters — all
unauthenticated, no approval needed. The lineup optimizer and drop/
rolling-waiver policy guardrails, the full draft assistant, the weekly
manager, and the web GUI are all backed by a full backtesting suite (see
[docs/dev/BACKTEST.md](docs/dev/BACKTEST.md)) that replays the weekly
lineup, draft, and waiver/streaming paths against real NFL seasons.

Sleeper's public API has no write capability at all, so the tool stays
advisory permanently — every lineup change, waiver claim, and draft pick
is executed by a human in the Sleeper app. Trade support has no design
yet.

## License and disclaimer

MIT-licensed — see [LICENSE](LICENSE). Use it, fork it, modify it, run it
for your own league; no attribution required beyond keeping the license
file.

A few things worth being explicit about:

- This produces *recommendations from public projections*, not
  guarantees. The tool has no write access to Sleeper at all — every
  lineup call, waiver claim, and draft pick is a decision you make and
  execute yourself. The author is not responsible for outcomes from using
  it.
- Not affiliated with or endorsed by Sleeper, FantasyPros, nflverse,
  Kalshi, or the NFL. It reads public, unauthenticated endpoints — respect
  their terms and rate limits if you extend it.
- Not gambling advice. `game_conditions.odds_source: kalshi` reads a
  public prediction market purely as a projection input (an implied team
  total), the same way a Vegas line would be used — it is not a betting
  recommendation.
