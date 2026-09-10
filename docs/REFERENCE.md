# Reference: configuration, fallbacks, troubleshooting

The GUI is the intended way to use `ffbot` — see the [README](../README.md)
for setup and [GUIDE.md](GUIDE.md) for day-to-day use. This page is
everything else: what each config key does, the manual/terminal routes that
exist as backups, and what to do when something looks wrong.

## How configuration works

Two files. `config.yml` — behavior: scoring weights, drop protections,
draft valuation, weekly tuning. It's hand-narrated with comments a YAML
round-trip would destroy, so nothing in this repo ever rewrites it —
including the GUI's Settings page, which instead writes to a gitignored
sibling, `config.local.yml`, deep-merged on top at load time (a local
override of just `draft: {num_teams: 10}` doesn't blank out the rest of
that block). Missing entirely is the common case; safe to hand-edit too, or
delete to fall back to whatever `config.yml` says.

`league.yml` — your league's actual scoring rules, separate from `config.yml`
because it describes your league, not this tool's behavior. Copy
`league.example.yml` and fill in Sleeper's League Settings values, or let
`scripts/init_league.py` generate it from Sleeper's live `scoring_settings`
(see the README). Run `scripts/scoring_check.py` after editing it by hand —
it prints exactly what's exact, what's estimated, and what no FantasyPros
export column can express. No `league.yml` at all is a clean no-op: every
board falls back to FantasyPros' own consensus PPR scoring.

## config.yml reference

Below, "code default" means what `ffbot/config.py`'s dataclasses fall back
to if a key is deleted entirely — **the shipped `config.yml` already sets
the live/Sleeper option for nearly every one of these**, so you shouldn't
need to touch most of this section on a normal setup.

- **Top level** — `sleeper:` (`league_id`/`username`/`roster_id`, filled by
  `init_league.py` — see the README), `roster_positions` (your league's
  starting-slot layout; `whoami.py` prints this from Sleeper, and it's also
  the draft assistant's default before you connect).
- **`projection:`** — how a *missing* weekly projection gets estimated, and
  how injury status discounts one that exists. Not scoring rules, and not
  the same question as `projection_source:` below (which picks WHERE the
  real numbers come from).
- **`projection_source:`** — `"sleeper"` (shipped default: real live weekly
  numbers and a genuine rest-of-season total, free and unauthenticated),
  `"board"` (code default if you remove the key: the frozen preseason board
  rescaled to a per-week rate, no network), or `"csv"` (the `--proj` CLI
  flag, hand-fed FantasyPros weekly exports — not reachable from the GUI).
  A failed `"sleeper"` fetch falls back to `"board"` for that run and
  surfaces why, never a crash.
- **`roster_source:`** — `"sleeper"` (shipped default: your live Sleeper
  roster — real names, live injury status, live ownership%) or `"file"`
  (code default if removed: `roster.yml`'s hand-maintained name list).
  `roster.yml` stays relevant even under `"sleeper"`: it's read as an
  optional per-player flag overlay (`undroppable`/`keeper_round`/`note`/
  `blocking`), never as identity, once this is set. A failed fetch falls
  back to `"file"` for that run.
- **`standings_source:`** — `"sleeper"` (shipped default: derived live from
  Sleeper's rosters/users/matchups endpoints, merged UNDER `league.yml` so
  a hand-curated entry there always wins) or `"file"` (code default if
  removed: whatever `league.yml`'s `teams:`/`my_team`/`my_opponent`/`week`
  block says by hand). `season.denial_weight`/`denial_opponent_boost`/
  `denial_seed_window` and `matchup_variance_weight` are all exact no-ops
  without this populated, regardless of their own weight — and without a
  real `league.yml` at all, this source is skipped entirely.
- **`league_rosters_source:`** — WHERE the other 11 teams' rosters come
  from, the free-agent-pool exclusion set every add/drop and waiver-claim
  recommendation needs. `"sleeper"` (shipped default: fetched fresh, an
  exact `player_id` join, on every run — no staleness possible, needs only
  `sleeper.league_id`) or `"file"` (code default if removed:
  `league_rosters.yml`, a snapshot `scripts/import_league_rosters.py --live`
  writes on demand — goes stale the moment a waiver claim processes unless
  something re-runs the script). A failed live fetch falls back to the file
  with a surfaced alert.
- **`game_conditions:`** — auto-fetched weather (Open-Meteo forecast) and
  game totals/spread (Kalshi public markets), merged UNDER
  `weekly/week-NN.yml` so a human's `/gameday` research always wins.
  `weather_source`/`odds_source` are independent switches (shipped on) so a
  scheduled run with nobody at the keyboard still feeds real numbers to
  `season.weather_weight`/`vegas_weight` instead of a no-op 1.0x.
- **`drops:`** — guardrails on the one irreversible in-season action
  (dropping a player). `drops.protect_pct_owned` is driven by
  `percent_owned`, which is real, live data under `roster_source: sleeper`
  and `None` (the guardrail inert) on the manual `roster.yml` route.
- **`draft:`** — everything the draft assistant needs: league shape
  (`num_teams`, `my_slot`, `rounds`, `order` — `snake` or `linear`), the
  FantasyPros CSV sources (`board_csv`), `board_points_source` (`"sleeper"`
  overlays live Sleeper season points onto the board while the CSVs still
  supply ADP/bye/cross-site spread, which Sleeper's endpoint doesn't
  carry), replacement-level/tiering/ADP-survival tuning, `position_caps`
  (hard ceilings), `position_targets` (soft roster-shape targets),
  `scarcity_weight`, and the thirteen [tuning dials](#tuning-dials) the
  Settings page's Draft panel moves. `scarcity_weight` is the one
  valuation dial denominated in real season points rather than a fraction
  of the pick's decision scale: it subtracts what a position is expected to
  still be worth at your *next* pick, so a position about to evaporate
  outranks an equally-valued one that will still be there. It is
  deliberately not a tuning dial and gets no slider (structural roster
  construction) — without it, a full-PPR board where WR and RB
  have nearly the same replacement level drafts seven receivers and one
  running back. Backtest-confirmed positive; see
  [BACKTEST.md's B8 section](dev/BACKTEST.md).
  Every tuning dial defaults to `DRAFT_BASELINE` in `ffbot/config.py`;
  setting one here pins that dial and stops it tracking the baseline, which
  is why `config.yml` ships them all commented out. Precedence, lowest to
  highest: baseline → a key here → `config.local.yml` (what the sliders
  write). `use_untested_features` gates `kalshi_weight` on this path.
- **`season:`** — the weekly manager's tuning. The eighteen
  [tuning dials](#tuning-dials) the Settings page's Weekly panel moves
  (weather/Vegas/trend/volatility/upside-lean/streaming, plus the
  structural denial/blocking/priority dials), each defaulting to
  `SEASON_BASELINE` and each overridable on its own without disturbing the
  rest. `use_untested_features` gates `kalshi_weight`,
  `venue_disruption_weight` (playing outside a typical NFL setting —
  inconclusive evidence) and `matchup_variance_weight`. Also here, and not
  tuning dials: `ros_blend` (season-long vs. this-week value in waiver
  ranking), `stream_ros_blend` (the same for a streaming position only, lower
  because you never actually acquire a streamed player's rest-of-season value),
  `noise_floor_weight` (minimum gain worth recommending at all, scaled by how
  much of a position's projected spread historically survives — ships at 0.0,
  awaiting evidence), `min_stream_spots`, `denial_row_limit`, and
  `stream_positions`
  (which positions the weekly manager scans for a streaming upgrade —
  `[K, DEF]` by default; the GUI folds these straight into its
  recommendations with no per-run input).
- **`notify:`** — push notifications for the [scheduled task](GUIDE.md#hands-off-mode-the-scheduled-task).
  `channel`: `"off"` (shipped default, exact no-op), `"ntfy"` (a free phone
  push via [ntfy.sh](https://ntfy.sh) — set `ntfy_topic` in
  **`config.local.yml`**, never here, since the topic name is the secret),
  `"toast"` (a local Windows notification), or `"both"`. `min_waiver_net` —
  a claim-worthy waiver candidate only notifies once its net season-point
  value clears this.

### league.yml

Also here (all optional, all no-ops until set):

- **`playoff_teams`, `lock_eliminated_teams`** — structural league facts
  valuation code needs. Waivers are always modeled as rolling-priority —
  there is no FAAB support.
- **`week`, `my_team`, `my_opponent`, `teams:`** — curated weekly
  standings, live-fetched by default under `standings_source: sleeper`
  above. `teams:` entries feed [tactical denial](#tactical-denial).

## Tuning dials

Everything the tool leans on beyond plain top-projected consensus — weather,
Vegas totals, usage trend, tactical blocking, draft-time upside and risk — is
a numbered weight, and each one has a slider on the **Settings** page.

There is no level to pick. The shipped defaults are the evidence-backed
baseline: on the weekly side, the one configuration in this project's
backtesting ever validated on a genuinely held-out season (train 2021–2023
+0.392 pts, 95% CI [+0.11, +0.68]; held-out 2024 +0.487, CI [+0.11, +0.88]);
on the draft side, the assembled shape that measured +123 season points over
following the market's blind ADP order, 95% CI excluding zero. If you change
nothing, that is what you get.

**Two panels of sliders** — Weekly tuning and Draft tuning — cover every dial,
grouped by what they do (game conditions, player trend, variance lean, waivers
and streaming, blocking and denial; and on the draft side player valuation,
roster construction, risk ramp). Most are *fractions of the decision at hand* —
the projection gap between the real options in front of you — not point totals,
so 0.45 means "this signal is worth up to 45% of that gap." Each slider has a
↺ to restore its default.

**One checkbox per panel: "use untested features."** This turns on the dials
this repo has no backtest evidence for, in either direction:

- **Weekly** — per-player Kalshi prop markets (those markets launched in
  September 2025, with zero overlap with the 2021–2024 backtest window),
  venue disruption for international games (inconclusive — no train/test
  season has ever isolated it), and matchup-conditioned variance (structurally
  unmeasurable by the lineup-only replayer).
- **Draft** — Kalshi prediction markets, for the same reason.

It switches *features* on, never *intensity*: ticking it does not touch
`weather_weight` or any other measured dial. Turning one of those up is what
the sliders are for. Left off, the gated dials are held at a hard zero no
matter what any config file says.

Retired weights stay retired and get no slider: `arbitrage_weight`,
`game_script_weight`, and `scarcity_covered_damping` all measured actively
harmful and are excluded rather than merely defaulted to zero.

Saving applies immediately — including to a draft room that is already open,
with its recorded picks intact. Team count, draft order, and roster shape are
still refused mid-draft; reset the draft first.

**If you have an old `spice_level` in a config file**, it still loads: it now
selects a named baseline (1–4, the old presets) and the untested checkbox
applies on top of it, so `spice_level: 4` with the box unticked means "level 4
intensity, untested features off." It is deprecated and warns, and the Settings
page removes it from `config.local.yml` the next time you save. The presets
themselves live on internally — `scripts/backtest_*.py` still address them by
level, and the evidence behind every dial's shipped value is in
[docs/dev/SPICE.md](dev/SPICE.md) for anyone who wants to check the work.

## Connecting manually

If you'd rather not run the bootstrap wizard, or want more control:

```bash
python scripts/whoami.py --username <your-sleeper-username>
```

Lists every league your account is in for the current season (or pass
`--season 2025`) and *prints* — never writes — `league_id`, your
`roster_id`, and the league's real `roster_positions`/`scoring_settings`,
for you to paste in by hand:

```yaml
# config.local.yml
sleeper:
  league_id: "1048499258691981312"
  username: "yourname"
  roster_id: 4
```

`roster_id` is optional — leave it `null` and it resolves from `username`
automatically each run. Transcribe `scoring_settings` into `league.yml` by
hand using `league.example.yml` as a guide (what `init_league.py` does for
you automatically). Verify the connection with:

```bash
python scripts/week_report.py --week 1 --source sleeper
```

A real weekly brief with live numbers means it worked.

## The manual roster.yml route

Set `roster_source.source: file` to run entirely from a hand-maintained
name list instead of a live Sleeper roster — no Sleeper account needed at
all for this part. `roster.yml` holds just player names (see
`roster.example.yml`); the optimizer decides slots, so you maintain a name
list, not a lineup. Names are validated against the board at load — a typo
surfaces as an error immediately, not as a silently-vanished player three
weeks later.

The lineup **baseline** follows the same split: under `roster_source: file`,
`weekly/lineup_state.yml` remembers each player's slot from the last run,
so the move list reads as real week-over-week changes rather than
"everyone moves off the bench" every time (`scripts/week_report.py` writes
it after every run unless `--no-save-state`). Under `roster_source:
sleeper`, that file is skipped entirely — your actual live lineup in the
Sleeper app is the baseline directly, so a recommended move always means
"make this change in the app," never "the tool remembers you already made
it."

## Terminal draft assistant

```bash
python scripts/draft.py --slot 4
```

Sleeper randomizes draft position and tells you shortly before the draft;
`me 7` fixes it if it changes. Put the terminal and Sleeper's draft room
side by side.

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

If it crashes or closes by accident, nothing is lost: `--resume` replays
every command from `draft_log.jsonl`, in either interface, regardless of
which one originally recorded the picks.

**Live draft sync** (on by default in both interfaces) needs
`draft/sleeper_ids.json`, built by:

```bash
python scripts/draft_export.py --board rankings.csv --reconcile
```

`init_league.py` builds this automatically once a board is configured;
re-run it (or this command directly) after downloading the FantasyPros
CSVs if sync isn't live yet. Override league shape per run:

```bash
python scripts/draft.py --slot 4 --teams 10 --order linear --rounds 16
```

Changes to team count, draft order, or roster shape are refused once a
draft has picks recorded — `reset` first.

`--draft-id` points sync at a specific Sleeper draft instead of resolving
one from `sleeper.league_id` — most useful for rehearsing against a Sleeper
mock draft before the real thing (see docs/GUIDE.md's "Try it before the
season"). A draft whose `league_id` doesn't match `sleeper.league_id` is
treated as foreign: ownership no longer trusts `sleeper.roster_id`/
`username` (meaningless against a draft outside your league) and falls back
to snake-order inference instead, exact for a mock since there are no
trades.

## Terminal weekly report

```bash
python scripts/week_report.py --week 3 --stream K DEF --waivers --priority 6
```

`--priority` is optional under `roster_source: sleeper` — your real
rolling waiver position is fetched live and used automatically when you
leave it off.

### Scheduling

```bash
python scripts/autorun.py --dry-run           # print this week's trigger schedule, fire nothing
python scripts/schedule_autorun.py register    # register the recurring task (see GUIDE.md)
python scripts/schedule_autorun.py register -- --waiver-weekday wed --waiver-hour 21
python scripts/schedule_autorun.py status
python scripts/schedule_autorun.py remove
```

`schedule_autorun.py register` accepts `--every MINUTES` (default 15) and
`--python PATH` (default: this venv's own windowless interpreter). Anything
after a literal `--` forwards to every scheduled `autorun.py` invocation.
On non-Windows platforms it prints the equivalent `crontab` line instead of
touching `schtasks`.

## Tactical denial

Holding — or claiming — a player purely to deny a contending rival, never
because you'd start them yourself. Needs two optional inputs, both of
which degrade to an exact no-op if missing: rival rosters
(`league_rosters_source: sleeper`, live by default, or
`scripts/import_league_rosters.py`'s file snapshot) and `league.yml`'s
`teams:` standings section.

Three layered, individually-optional signals:

- **`denial_weight`** — the base signal: a rival's threat scales with how
  close their seed is to the playoff bubble.
- **`denial_opponent_boost`** — an extra boost for whoever `league.yml`'s
  `my_opponent` names as your head-to-head opponent this week.
- **`denial_seed_window`** — once the season reaches its playoff push (the
  final few weeks of `regular_season_weeks`), a further boost for any
  rival within this many seeds of your own.

A rival threatening to grab an ordinary add folds straight into that add's
waiver `net` (an everyday reason to move now). A free agent worth claiming
*purely* to deny — never to start — shows up as its own flagged row
instead, since denial is an inference about other humans' behavior, not a
verifiable fact.

## Data sources

Every piece of data behind a recommendation, in one place. Every live
source degrades independently: a failed fetch falls back to the
frozen/offline route and surfaces an alert, never crashes and never
silently succeeds.

**Live, free, unauthenticated:**

| Source | Feeds | Config toggle |
|---|---|---|
| Sleeper — league state | roster identity, scoring, slot layout, standings, live lineup baseline, other teams' rosters, live opponent's starters | `sleeper:` block |
| Sleeper — weekly projections | this week's per-player points, rescored under `league.yml`; summed forward into a real rest-of-season total | `projection_source.source` |
| Sleeper — season projections | points overlay on the draft board (ADP/bye/spread still from FantasyPros) | `draft.board_points_source` |
| Sleeper — ownership research | `percent_owned` (drives drop protections) and `started_pct` (shown on recommendations) | `roster_source.source` |
| Sleeper — weekly realized stats | each player's ACTUAL points scored so far, rescored under `league.yml`. Shown on every recommendation and logged; **never enters a valuation** — see the note below | `season_stats_source.source` |
| Sleeper — players dump | player identity, team, injury status, DEF keys; pre-draft ID reconciliation | used by the above |
| Sleeper — live draft feed | live pick sync during a real draft | draft sync, on by default |
| nflverse schedule | opponent, home/away, kickoff time, roof/dome state | required whenever weather or odds is on |
| Open-Meteo forecast | wind, gusts, precip, temp per outdoor stadium at kickoff | `game_conditions.weather_source` |
| Kalshi — game totals/spread | market-implied team totals (the Vegas tilt), always live | `game_conditions.odds_source` |
| Kalshi — per-player props | a per-player signal on both weekly and draft paths | `use_untested_features` |

Season points-to-date is the one live source that is deliberately
**descriptive only**. It is attached to every recommendation and written
into the weekly log so a call can be reviewed against what a player had
really been doing, but nothing reads it back into a ranking. Two reasons:
a realized-outcome number feeding the valuation would be a scoring change
no backtest has graded, and it would double-count, since live
rest-of-season projections already price in past production.
`tests/test_gameplan.py::TestSeasonPointsToDateIsDescriptiveOnly` pins
that as an invariant rather than a promise.

**Local files:** the five FantasyPros CSVs under `draft/` (the frozen
board's baseline points, ADP, cross-site ADP stdev), `league.yml` (scoring
rules, standings), `league_rosters.yml` (rival rosters, fallback route),
`data/stadiums.yml` (dome gate + coordinates), `roster.yml` (flag overlay
or identity, depending on `roster_source`), `config.yml`/`config.local.yml`,
`draft_log.jsonl`, and the lineup-state file.

**Human research:** `/gameday` writes `weekly/week-NN.yml` (schedule,
official status, weather, Vegas, plain-English notes) and always wins
outright over the auto-fetched sources above, merged whole-entry per team.
`/intel-refresh` writes `draft/intel.yml` (researched upside/risk, notes
that land verbatim in the recommendation WHY column).

**Backtest-only** (never touch a live recommendation): nflverse box
scores/injuries/rosters, the DynastyProcess FantasyPros-ECR archive,
Fantasy Football Calculator historical ADP, the Open-Meteo archive — see
[docs/dev/BACKTEST.md](dev/BACKTEST.md) for the full leakage register.

**Outbound:** the scheduled task's push notifications (`ntfy`/`toast`) are
the one non-Sleeper, non-inbound seam — see [Hands-off mode](GUIDE.md#hands-off-mode-the-scheduled-task).

## Troubleshooting

- **The GUI won't start, or the draft room says no board is loaded** — the
  five FantasyPros CSVs (README step 2) haven't been downloaded into
  `draft/` yet. The weekly page still works fully without them; download
  the CSVs and restart the server to enable the draft room and waiver-add
  valuation.
- **"ntfy: no ntfy_topic configured"** in a scheduled run's output — you
  set `notify.channel: ntfy` without also setting `notify.ntfy_topic` in
  `config.local.yml`. See [Hands-off mode](GUIDE.md#hands-off-mode-the-scheduled-task).
- **Draft sync says it's not live** — `draft/sleeper_ids.json` doesn't
  exist yet, which needs a board (download the CSVs, then re-run
  `init_league.py` or `draft_export.py --reconcile` directly).
- **The weekly page says the week can't be resolved** — `roster_source:
  sleeper` asks Sleeper for the current NFL week, which only resolves
  during the season; off-season, set `league.yml`'s `week:` field or pass
  `--week` explicitly.
- **A Settings change to `my_slot`/`rounds`, or a newly pasted league ID,
  didn't take effect** — those need a server restart. Tuning dials do not:
  they reach an open draft room on its next poll. See
  [When something looks odd](GUIDE.md#when-something-looks-odd).
- **`league_id`/`username` are empty** — every live Sleeper source falls
  back to its offline route until you run `scripts/init_league.py` (README
  step 1) or fill them in by hand (see [Connecting manually](#connecting-manually)).
