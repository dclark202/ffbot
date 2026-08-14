# Information sources behind draft and weekly recommendations

Every piece of data that ends up in a draft recommendation or a weekly brief — live
network fetches, hand-maintained files, and human research — collected in one place.
Where a section is documented in more depth elsewhere, that doc is linked.

The "Toggle" / "On today" columns refer to `config.yml` on the branch this doc was
written against; re-check them if you've changed `config.yml` since.

## A. Live network sources (free, unauthenticated, no API key)

Every one of these degrades independently: a failed fetch falls back to the
frozen/offline route and surfaces an alert to the user, never crashes and never
silently succeeds (see CLAUDE.md's live-seam contract).

| # | Source | Endpoint | Feeds | Toggle | On today |
|---|---|---|---|---|---|
| 1 | **Sleeper — league state** | `api.sleeper.app/v1` (state, league, rosters, users, matchups, transactions, traded picks) | roster identity, scoring settings, slot layout, standings, waiver budget | `sleeper:` block | yes |
| 2 | **Sleeper — weekly projections** | `api.sleeper.app/projections/nfl/<season>/<week>` | this week's per-player points, re-scored under `league.yml`'s rules; summed across remaining weeks into a real rest-of-season total | `projection_source.source` | `sleeper` |
| 3 | **Sleeper — season projections + ADP** | `api.sleeper.com/projections/nfl/<season>` (undocumented) | points overlay on the draft board (ADP/bye/cross-site spread still come from the FantasyPros CSVs) | `draft.board_points_source` | `sleeper` |
| 4 | **Sleeper — ownership research** | `api.sleeper.com/players/nfl/research/regular/<season>/<week>` (undocumented) | `percent_owned` / `started_pct` — drives drop protections and FAAB bid floors | `roster_source.source` | `sleeper` |
| 5 | **Sleeper — players dump** | `api.sleeper.app/v1/players/nfl` | player identity, team, injury status, DEF keys; also the pre-draft ID reconciliation (`draft_export.py --reconcile`) | used by 1 and 4 | yes |
| 6 | **Sleeper — live draft feed** | `/v1/draft/<id>` + `/draft/<id>/picks` (never cached) | live pick sync during a real Sleeper draft | `scripts/draft.py --sync` | on demand |
| 7 | **Sleeper — trending add/drop** | `/v1/players/nfl/trending/{add,drop}` | *nothing yet* — the client method exists (`SleeperClient.trending`), but no recommendation path reads it | — | unwired |
| 8 | **nflverse schedule** | `games.csv` GitHub release, refetched every run (never cache-trusted like the historical path) | opponent, home/away, kickoff time, roof/dome state for the current week | required whenever weather or odds is on | yes |
| 9 | **Open-Meteo forecast** | `api.open-meteo.com/v1/forecast` | wind, gusts, precip %, temp per outdoor stadium, at the kickoff hour | `game_conditions.weather_source` | `open_meteo` |
| 10 | **Kalshi — game totals/spread** | `api.elections.kalshi.com/trade-api/v2`, series `KXNFLTOTAL`/`KXNFLSPREAD` | market-implied team totals → `GameInfo.team_total`/`opp_total` (the Vegas tilt), live at every spice level | `game_conditions.odds_source` + `kalshi_odds_series` | `kalshi` |
| 11 | **Kalshi — per-player props** | same host, single yes/no markets ("Player X: 1+ TD") | a 0–100 per-player signal on both the weekly and draft paths | `season.spice_level` / `draft.spice_level` **= 4 only** | no (level 3) |

## B. Local files — market data and league facts

| Source | Path | Feeds |
|---|---|---|
| FantasyPros projections export | `draft/proj_flex.csv`, `proj_qb.csv`, `proj_k.csv`, `proj_dst.csv` | the frozen board's baseline points and stat lines (hand-downloaded from FantasyPros) |
| FantasyPros ADP export | `draft/adp.csv` | ADP, cross-site ADP stdev (the volatility/boom-bust proxy), bye weeks |
| League rules | `league.yml` | actual scoring rules (every projection is re-scored under these), waiver type, `regular_season_weeks`, `my_opponent`, standings |
| Rival rosters | `league_rosters.yml` | tactical denial — who else needs a candidate player (`scripts/import_league_rosters.py --live` fills it live from Sleeper) |
| Stadiums | `data/stadiums.yml` | dome gate + lat/lon for the weather lookup, including international/neutral-site venues |
| Roster flags | `roster.yml` | under `roster_source: sleeper`, a flag overlay only (`undroppable`/`keeper_round`/`note`/`blocking`); under `roster_source: file`, also the roster's identity |
| Behavior config | `config.yml` + `config.local.yml` | every tunable weight/threshold; the weekly and draft spice ladders resolve from `spice_level` |
| Draft log | `draft_log.jsonl` | the pick history the draft state machine replays |
| Lineup state | lineup-state file (via `roster_source`) | last week's slot assignments, so the optimizer prefers the minimal-move equivalent lineup |

## C. Human research (the LLM-in-the-loop sources)

Both are held to the same factual/speculative contract: what's verifiable moves a
number, what's speculative stays a `note:` a human reads — never a `status`/`risk` field.

| Source | Written to | Feeds |
|---|---|---|
| **`/gameday`** — real schedule/venue for the week, official injury designations, near-kickoff weather, Vegas totals, beat-writer color | `weekly/week-NN.yml` (`players:` + `games:`) | status overrides, weather/Vegas numbers, plain-English notes in the brief. **Always wins outright over the auto-fetched sources 8/9/10**, merged whole-entry per team (see `ffbot/live/conditions.py`'s `merge_conditions`) |
| **`/intel-refresh`** — pre-draft (or in-season weekly) player research: availability facts, camp/role reports, Vegas, ADP movers | `draft/intel.yml` | `upside` (researched breakout case) and `risk` (verifiable availability only) score the board; `note` lands verbatim in the recommendation WHY column |

`weekly/week-NN.yml` can also carry hand-typed `volatility`, `upside`, `usage_trend`,
`momentum`, and `divergence` fields per player — the same trend signals a backtest
`SignalProvider` (section E) can populate automatically in historical replay.

## D. Derived, not sourced

Numbers computed from the sources above rather than fetched or typed anywhere:

- **Replacement level / VOR / tiers** — derived by running the lineup optimizer over
  the whole player pool, never assumed (`ffbot/board.py`).
- **ADP survival probability** — `Normal(adp, sigma)` from the ADP export, conditioned
  on surviving to the current pick (`ffbot/draft.py`).
- **`demand_ahead` / denial threat** — computed from `league_rosters.yml` + `league.yml`
  standings (`ffbot/denial.py`).
- **Decision scale** — the point spread across the current player pool; every spice
  weight is a fraction of it, never an absolute point total.

## E. Backtest-only sources (never touch a live recommendation)

Reachable only through `ffbot/history/` + `ffbot/backtest/`, behind
`ffbot.history.index.as_of()`'s leakage boundary — see [docs/BACKTEST.md](BACKTEST.md)
for the full leakage register.

- **nflverse** — `stats_player_week`, `stats_team_week`, `games.csv`,
  `injuries_<season>.csv`, `roster_weekly_<season>.csv` (ground truth + point-in-time
  intel for replay).
- **DynastyProcess archive** — the free FantasyPros ECR scrape history (point-in-time
  rankings, preseason cheatsheets).
- **Fantasy Football Calculator** — `fantasyfootballcalculator.com/api/v1` historical
  ADP.
- **Open-Meteo archive** — `archive-api.open-meteo.com` observed historical weather.

## See also

- [docs/SETUP.md](SETUP.md) — Sleeper discovery and the full `config.yml`/`league.yml`
  reference for every toggle above.
- [docs/DRAFT.md](DRAFT.md) — preparing the board and researched intel.
- [docs/INSEASON.md](INSEASON.md) — the weekly research/report cycle.
- [docs/BACKTEST.md](BACKTEST.md) — the historical data stack (section E) in full.
