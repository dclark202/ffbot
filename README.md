# ffbot

Two tools around one Sleeper fantasy football team, plus a local web GUI for both:

1. **Live snake-draft assistant** — a keyboard-driven, fully offline recommendation
   engine that ranks every remaining player by value-over-replacement, need, and a
   set of optional "edge" signals (researched upside/risk, ADP disagreement, stacking,
   arbitrage), with real-time alerts for positional runs, tier cliffs, and bye-week
   collisions. Optionally syncs picks live from a real Sleeper draft — no credentials
   needed.
2. **In-season weekly manager** — a start/sit + waivers + streaming brief, built on
   the exact same optimizer as the draft assistant. Reads a hand-maintained
   `roster.yml` by default (no Sleeper account needed at all); a live Sleeper-fetch
   route (real roster, injury status, ownership%, and rest-of-season projections) is
   built and working — see `roster_source`/`projection_source` in `config.yml`.
3. **Web GUI** (`scripts/gui.py`) — a local, zero-dependency server exposing both of
   the above as pages in your browser: a draft room, a read-only weekly manager that
   auto-loads the current week's live Sleeper state and recommendations, and a
   settings page. Uses the identical compute layer as the two CLIs above — same
   draft log, same lineup logic, same everything — just a second front end.

Everything works entirely offline by default. Flip `projection_source`/`roster_source`/
`draft.board_points_source` to `sleeper` in `config.yml` for live data — Sleeper's
public API needs no credentials or approval at all, so this is a config toggle, not a
setup process. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for how a recommendation
actually gets made and what the spice-level dial means.

## Use this as a template

This repo is meant to be forked, not run in place — click **"Use this template"**
above (or `gh repo create --template <this-repo>`) to get a clean copy with no
history, one repo per season or per league. That keeps each copy's draft log, board
CSVs, and weekly reports naturally separate from anyone else's.

## Quickstart (5 minutes)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on macOS/Linux
pytest                                            # optional: run the full test suite

# Pull your league straight from Sleeper — no credentials, no manual transcription:
.venv/Scripts/python scripts/init_league.py --username <your-sleeper-username>
```

That last command writes `config.local.yml` (your `league_id`/`roster_id`) and
`league.yml` (your league's real scoring rules, translated from Sleeper) automatically.
Then:

```bash
.venv/Scripts/python scripts/scoring_check.py   # verify the generated league.yml
# download the 5 FantasyPros CSVs into draft/ -- see docs/DRAFT.md
.venv/Scripts/python scripts/gui.py             # http://127.0.0.1:8321/
```

Prefer to do it by hand, or need finer control? `scripts/whoami.py` prints your
league's IDs and raw scoring settings without writing anything — see
[docs/SETUP.md](docs/SETUP.md).

## What's incorporated

- FantasyPros projections, ADP, and rankings for the draft board (CSV exports) —
  Sleeper can optionally overlay live points on top (`draft.board_points_source:
  sleeper`), while FantasyPros still supplies ADP, bye weeks, and cross-site ADP
  spread, which Sleeper's API doesn't carry
- Live weekly and rest-of-season projections from Sleeper, real component stat lines
  re-scored under your league's actual rules — not a frozen preseason estimate
  rescaled down
- Your live Sleeper roster (name, team, injury status, ownership%) when
  `roster_source: sleeper` is set, merged with any per-player flags
  (`undroppable`/`keeper_round`/`note`/`blocking`) you still keep in `roster.yml`
- Your league's actual scoring rules — auto-imported from Sleeper by
  `scripts/init_league.py`, or transcribed by hand into `league.yml`
  (points-allowed ladders, distance-tiered field goals, TE premium, whatever your
  league does differently from generic PPR)
- Researched player intel (injury status, breakout/risk signals, plain-English notes)
  that a Claude Code research pass writes into `draft/intel.yml` and
  `weekly/week-NN.yml`
- Weekly weather (wind/precipitation vs. each stadium's roof), Vegas implied team
  totals, and neutral-site/international game flags
- Other teams' rosters and league standings, for waiver-pool exclusion and tactical
  denial (holding a player purely to keep a contending rival from getting him) — auto-
  importable live from Sleeper (`scripts/import_league_rosters.py --live`) or pasted by
  hand

## Quick commands

```bash
.venv/Scripts/python scripts/init_league.py --username <you>         # bootstrap config.local.yml + league.yml from Sleeper
.venv/Scripts/python scripts/gui.py                                  # web GUI, http://127.0.0.1:8321/
.venv/Scripts/python scripts/draft.py --slot 4                       # live draft assistant (terminal)
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers             # weekly brief (terminal)
```

The GUI and the terminal tools share the same draft log and the same lineup state —
start a draft in one, resume it in the other.

## Docs

- **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)** — how a recommendation actually gets
  made: the pipeline from projection to lineup, the spice-level ladder explained, normal
  weekly/draft use, and an honest list of what's been backtested vs. shipped on
  judgment.
- **[docs/SPICE.md](docs/SPICE.md)** — the full 1–4 spice ladder reference: what
  every level actually turns on, the evidence class behind every single dial, and
  the backtest run results the current ladder rests on.
- **[docs/SETUP.md](docs/SETUP.md)** — discovering your Sleeper league (no
  credentials needed at all), and the full `config.yml`/`league.yml` tuning reference
  (scoring, spice level, every knob).
- **[docs/SOURCES.md](docs/SOURCES.md)** — every information source behind a draft or
  weekly recommendation (live fetches, local files, human research), one page, with
  what toggles each one on.
- **[docs/DRAFT.md](docs/DRAFT.md)** — preparing the board (CSV refresh, researched
  intel) and running the draft assistant, CLI or GUI, including live Sleeper draft
  sync.
- **[docs/INSEASON.md](docs/INSEASON.md)** — preparing and running the weekly
  manager: roster setup (file or live Sleeper), researching a week, start/sit,
  waivers, streaming, denial.
- **[docs/BACKTEST.md](docs/BACKTEST.md)** — validating the optimizer/edge/spice
  weights against real NFL history instead of judgment: data sources, baselines,
  leakage protocol, and how to run it yourself.
- **[CLAUDE.md](CLAUDE.md)** — architecture and design invariants, for anyone
  (human or Claude Code) working on the code itself.

## Status

Built and tested: the Sleeper client (league discovery, live draft sync, live roster
identity/status/ownership%, live weekly + rest-of-season projections — all
unauthenticated, no API approval needed), the lineup optimizer and drop/rolling-waiver
policy guardrails, the full draft assistant, the in-season weekly manager, the web GUI, and
a full backtesting suite (see [docs/BACKTEST.md](docs/BACKTEST.md)) — the weekly
lineup, draft, and waiver/streaming paths can all be replayed against real NFL
seasons and graded against a frozen-projection control.

Sleeper's public API has no write capability at all, so the tool stays advisory
permanently — every lineup change, waiver claim, and draft pick is executed by a
human in the Sleeper app. The read-only path can run unattended: `scripts/autorun.py`
is a kickoff-aware local trigger (Windows Task Scheduler, no cloud dependency) that
fires the weekly report ahead of each kickoff slot and ahead of waivers — see
[docs/INSEASON.md](docs/INSEASON.md)'s Automation section. Trade support has no
design yet.

## License and disclaimer

MIT-licensed — see [LICENSE](LICENSE). Use it, fork it, modify it, run it for your
own league; no attribution required beyond keeping the license file.

A few things worth being explicit about:

- This produces *recommendations from public projections*, not guarantees. The tool
  has no write access to Sleeper at all — every lineup call, waiver claim, and draft
  pick is a decision you make and execute yourself. The author is not responsible for
  outcomes from using it.
- Not affiliated with or endorsed by Sleeper, FantasyPros, nflverse, Kalshi, or the
  NFL. It reads public, unauthenticated endpoints — respect their terms and rate
  limits if you extend it.
- Not gambling advice. `game_conditions.odds_source: kalshi` reads a public
  prediction market purely as a projection input (an implied team total), the same
  way a Vegas line would be used — it is not a betting recommendation.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how issues and PRs are handled.
