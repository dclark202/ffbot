# ffbot

Two tools around one Yahoo fantasy football team, plus a local web GUI for both:

1. **Live snake-draft assistant** — a keyboard-driven, fully offline recommendation
   engine that ranks every remaining player by value-over-replacement, need, and a
   set of optional "edge" signals (researched upside/risk, ADP disagreement, stacking,
   arbitrage), with real-time alerts for positional runs, tier cliffs, and bye-week
   collisions.
2. **In-season weekly manager** — a start/sit + waivers + streaming brief, built on
   the exact same optimizer as the draft assistant. Reads a hand-maintained
   `roster.yml` today (no Yahoo access needed); a live Yahoo-fetch route and,
   eventually, unattended writes are the natural extension once API access lands.
3. **Web GUI** (`scripts/gui.py`) — a local, zero-dependency server exposing both of
   the above as pages in your browser: a draft room, a weekly manager, roster and
   weekly-intel editors, and a settings page. Uses the identical compute layer as the
   two CLIs above — same draft log, same lineup logic, same everything — just a
   second front end.

Everything works entirely offline. Yahoo API access unlocks live roster fetching and
(eventually) automated writes, but every recommendation engine here runs against
plain CSV exports and hand-edited YAML with no network calls at all.

## What's incorporated

- FantasyPros projections, ADP, and rankings (CSV exports)
- Your league's actual scoring rules, transcribed once into `league.yml`
  (points-allowed ladders, distance-tiered field goals, TE premium, whatever your
  league does differently from generic PPR)
- Researched player intel (injury status, breakout/risk signals, plain-English notes)
  that a Claude Code research pass writes into `draft/intel.yml` and
  `weekly/week-NN.yml`
- Weekly weather (wind/precipitation vs. each stadium's roof), Vegas implied team
  totals, and neutral-site/international game flags
- Optionally, other teams' rosters and league standings, for waiver-pool exclusion
  and tactical denial (holding a player purely to keep a contending rival from
  getting him)

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on macOS/Linux
pytest                                            # run the full suite
```

## Quick commands

```bash
.venv/Scripts/python scripts/gui.py                                  # web GUI, http://127.0.0.1:8321/
.venv/Scripts/python scripts/draft.py --slot 4                       # live draft assistant (terminal)
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers --faab 45   # weekly brief (terminal)
```

The GUI and the terminal tools share the same draft log and the same lineup state —
start a draft in one, resume it in the other.

## Docs

- **[docs/SETUP.md](docs/SETUP.md)** — Yahoo API access (optional, gated behind
  manual approval), credentials, and the full `config.yml`/`league.yml` tuning
  reference (scoring, spice level, every knob).
- **[docs/DRAFT.md](docs/DRAFT.md)** — preparing the board (CSV refresh, researched
  intel) and running the draft assistant, CLI or GUI.
- **[docs/INSEASON.md](docs/INSEASON.md)** — preparing and running the weekly
  manager: roster setup, researching a week, start/sit, waivers, streaming, denial.
- **[docs/BACKTEST.md](docs/BACKTEST.md)** — validating the optimizer/edge/spice
  weights against real NFL history instead of judgment: data sources, baselines,
  leakage protocol, and the B1–B5 milestone plan.
- **[CLAUDE.md](CLAUDE.md)** — architecture and design invariants, for anyone
  (human or Claude Code) working on the code itself.

## Status

Built and tested: auth (hand-rolled Yahoo OAuth2, rotation-safe), the lineup
optimizer and drop/FAAB policy guardrails, the full draft assistant (board
valuation, live TUI, edge/contrarian layer, export, optional Yahoo sync), the
in-season weekly manager's manual-roster baseline (weather, Vegas, streaming,
waivers, denial), the web GUI, and B1–B4 of the backtest plan
(`ffbot/history/`, `ffbot/backtest/`, see [docs/BACKTEST.md](docs/BACKTEST.md)) —
the weekly lineup, draft, and waiver/streaming paths can all be replayed
against real NFL seasons and graded against a frozen-projection control.
886 tests.

Blocked on Yahoo granting the app Fantasy Sports API scope (a manual review
process — see [docs/SETUP.md](docs/SETUP.md)); until that lands, nothing can call
the Yahoo API, though every path above works fully offline regardless. Not yet
built: the Yahoo-fetch roster source and the scheduled/unattended routines that
would run either path automatically (see [docs/INSEASON.md](docs/INSEASON.md)'s
milestones). Trade support is future work — no design yet, see
[docs/INSEASON.md](docs/INSEASON.md).
