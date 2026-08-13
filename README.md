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
   the above as pages in your browser: a draft room, a weekly manager, roster and
   weekly-intel editors, and a settings page. Uses the identical compute layer as the
   two CLIs above — same draft log, same lineup logic, same everything — just a
   second front end.

Everything works entirely offline by default. Flip `projection_source`/`roster_source`/
`draft.board_points_source` to `sleeper` in `config.yml` for live data — Sleeper's
public API needs no credentials or approval at all, so this is a config toggle, not a
setup process.

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
- Your league's actual scoring rules, transcribed once into `league.yml`
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

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on macOS/Linux
pytest                                            # run the full suite
```

## Quick commands

```bash
.venv/Scripts/python scripts/whoami.py --username <you>              # discover your Sleeper league_id/roster_id
.venv/Scripts/python scripts/gui.py                                  # web GUI, http://127.0.0.1:8321/
.venv/Scripts/python scripts/draft.py --slot 4                       # live draft assistant (terminal)
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers --faab 45   # weekly brief (terminal)
```

The GUI and the terminal tools share the same draft log and the same lineup state —
start a draft in one, resume it in the other.

## Docs

- **[docs/SETUP.md](docs/SETUP.md)** — discovering your Sleeper league (no
  credentials needed at all), and the full `config.yml`/`league.yml` tuning reference
  (scoring, spice level, every knob).
- **[docs/DRAFT.md](docs/DRAFT.md)** — preparing the board (CSV refresh, researched
  intel) and running the draft assistant, CLI or GUI, including live Sleeper draft
  sync.
- **[docs/INSEASON.md](docs/INSEASON.md)** — preparing and running the weekly
  manager: roster setup (file or live Sleeper), researching a week, start/sit,
  waivers, streaming, denial.
- **[docs/BACKTEST.md](docs/BACKTEST.md)** — validating the optimizer/edge/spice
  weights against real NFL history instead of judgment: data sources, baselines,
  leakage protocol, and the B1–B6 milestone plan.
- **[CLAUDE.md](CLAUDE.md)** — architecture and design invariants, for anyone
  (human or Claude Code) working on the code itself.

## Status

Built and tested: the Sleeper client (league discovery, live draft sync, live roster
identity/status/ownership%, live weekly + rest-of-season projections — all
unauthenticated, no API approval needed), the lineup optimizer and drop/FAAB policy
guardrails, the full draft assistant (board valuation, live TUI, edge/contrarian
layer, export, optional live Sleeper sync), the in-season weekly manager (file or
live-Sleeper roster, weather, Vegas, streaming, waivers, denial), the web GUI, and
B1–B6 of the backtest plan (`ffbot/history/`, `ffbot/backtest/`, see
[docs/BACKTEST.md](docs/BACKTEST.md)) — the weekly lineup, draft, and
waiver/streaming paths can all be replayed against real NFL seasons and graded
against a frozen-projection control. The weekly spice ladder (`SPICE_PRESETS`) has
been re-derived along two axes (information vs. deliberate variance-seeking) and
validated on a held-out season; the draft ladder (`DRAFT_SPICE_PRESETS`) found and
retired one confirmed-harmful live weight (`arbitrage_weight`) but remains a first
exploratory pass, not a full re-derivation.

Migrated from an original Yahoo Fantasy design to Sleeper — Yahoo never granted the
app's Fantasy Sports API scope (a manual review that never resolved), while Sleeper's
public API works instantly with no credentials. The one real cost of that trade:
Sleeper has no write API at all, so unattended lineup/waiver writes — a stated Yahoo
goal — are off the table permanently; the tool stays advisory, a human executes every
action. Not yet built: the GitHub Actions workflows / scheduled routines that would
run the read-only path unattended, trade support (no design yet — see
[docs/INSEASON.md](docs/INSEASON.md)), and backtesting Sleeper itself as a historical
data source.
