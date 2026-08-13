# Setup: Sleeper discovery and configuration

Two independent things live here: discovering your Sleeper league (no credentials,
no approval process, no waiting — a single lookup), and the full configuration
reference for `config.yml`/`league.yml`. Do the Sleeper step whenever you like; the
config reference applies regardless.

---

## Part 1 — Finding your Sleeper league

Sleeper's public API needs no auth at all — no app registration, no OAuth, no
review process, nothing gitignored to protect. This whole section is one command.

```
scripts/whoami.py --username <you>  ──►  league_id + roster_id  ──►  config.yml
```

### Step 1 — Run whoami

```bash
.venv/Scripts/python scripts/whoami.py --username <your-sleeper-username>
```

This lists every league your account is in for the current season (or pass
`--season 2025` for a different year), and for each one prints:

- `league_id` — the league's identity
- your `roster_id` within it, resolved by matching your username against the
  league's rosters
- the league's real `roster_positions` and `scoring_settings`, straight from
  Sleeper, for reference

### Step 2 — Paste into `config.yml`

```yaml
sleeper:
  league_id: "1048499258691981312"
  username: "yourname"
  roster_id: 4
```

`roster_id` is optional — leave it `null` and it's resolved from `username`
automatically each run (one extra cached lookup, not worth avoiding unless you'd
rather not depend on username resolution staying stable). `username` is only
needed for that resolution and for `scripts/import_league_rosters.py`'s live
import; day-to-day runs with `roster_id` set don't touch it.

### Step 3 — Verify

```bash
.venv/Scripts/python scripts/week_report.py --week 1 --source sleeper
```

A real weekly brief with live numbers means the connection works. If you also set
`roster_source: sleeper` (see Part 2), the roster section should show your actual
Sleeper roster — a good end-to-end check before trusting it during the season.

### What's built and tested, on this front

League discovery, live draft pick sync (`scripts/draft.py --sync`), live roster
identity + injury status + ownership% (`roster_source: sleeper`), live weekly and
rest-of-season projections (`projection_source: sleeper`), and a live points
overlay on the draft board (`draft.board_points_source: sleeper`) — all working,
all unauthenticated. **Sleeper's public API has no write capability at all** — no
lineup setting, no waiver claims, no draft picks. This tool is advisory only, by
design of the platform, not a gap in this codebase: every action still gets
executed by a human, in the Sleeper app.

---

## Part 2 — Configuration reference

Everything the agent uses to make a judgment call lives in `config.yml` (behavior)
and `league.yml` (your league's actual scoring rules), so tuning strategy is a
config edit, never a code change. Both are heavily commented in place — this
section is a map of what's where, not a restatement of every comment.

### `config.yml` — behavior

- **Top level** — `sleeper:` (`league_id`/`username`/`roster_id`, see Part 1),
  `dry_run` (meaningful only for a hypothetical future write path — Sleeper has
  none today, so this stays on with nothing to disable), `roster_positions` (your
  league's starting-slot layout; `whoami.py` prints this from Sleeper once
  connected, and it's also the draft assistant's default before that).
- **`projection:`** — how a *missing* weekly projection gets estimated, and how
  injury status discounts one that exists. Not scoring rules, and not the same
  question as `projection_source:` below (which picks WHERE the real numbers come
  from in the first place).
- **`projection_source:`** — `"board"` (default: the frozen preseason board
  rescaled to a per-week rate, no network), `"sleeper"` (real live weekly numbers
  AND a genuine rest-of-season total, free and unauthenticated), or `"csv"` (the
  `--proj` flag, hand-fed FantasyPros weekly exports). A failed `"sleeper"` fetch
  always falls back to `"board"` for that run and prints why — never a crash.
- **`roster_source:`** — `"file"` (default: `roster.yml`'s hand-maintained name
  list) or `"sleeper"` (your live Sleeper roster — real names, live injury status,
  live ownership%). `roster.yml` stays relevant under `"sleeper"` too: it's read as
  an optional per-player flag overlay (`undroppable`/`keeper_round`/`note`/
  `blocking`), never as the identity list itself, once this is set. A failed fetch
  falls back to `"file"` for that run.
- **`standings_source:`** — `"file"` (default: whatever `league.yml`'s `teams:`/
  `my_team`/`my_opponent`/`week` block says by hand) or `"sleeper"` (derived live
  from Sleeper's rosters/users/matchups endpoints — see
  `ffbot/sleeper_standings.py` — and merged UNDER `league.yml`, so a hand-curated
  entry there always wins). `season.denial_weight`/`denial_opponent_boost`/
  `denial_seed_window` and `matchup_variance_weight` are all exact no-ops without
  this populated, regardless of their own weight.
- **`game_conditions:`** — auto-fetched weather (Open-Meteo forecast) and game
  totals/spread (Kalshi public markets), merged UNDER `weekly/week-NN.yml` so a
  human's `/gameday` research always wins. `weather_source`/`odds_source` are
  independent `"off"`/live switches; exists so a scheduled run with nobody at the
  keyboard (see `scripts/autorun.py`) still feeds real numbers to
  `season.weather_weight`/`vegas_weight` instead of sitting at a no-op 1.0x.
- **`drops:` / `faab:`** — the guardrails on the one irreversible in-season action
  (dropping a player) and on FAAB bid sizing. `drops.protect_pct_owned` and
  `faab.min_pct_owned_to_bid` are both driven by `percent_owned`, which is real,
  live data under `roster_source: sleeper` and `None` (both guardrails inert) on
  the manual `roster.yml` route.
- **`draft:`** — everything the draft assistant needs: league shape (`num_teams`,
  `my_slot`, `rounds`, `order` — `snake` or `linear`), the FantasyPros CSV sources
  (`board_csv`), `board_points_source` (`"csv"` default, or `"sleeper"` to overlay
  live Sleeper season points onto the board while FantasyPros' CSVs still supply
  ADP/bye/cross-site spread — Sleeper's endpoint doesn't carry those),
  replacement-level/tiering/ADP-survival tuning, `position_caps` (hard ceilings)
  and `position_targets` (soft roster-shape targets), and `spice_level` (1–5,
  same meaning as `season.spice_level` below) which resolves
  `DRAFT_SPICE_PRESETS` via `DraftConfig.from_spice_level` and sets the "how
  contrarian" edge-layer weights (`upside_weight`, `risk_weight`,
  `volatility_weight`, `stack_bonus`, `scoring_arbitrage_weight`,
  `risk_ramp_start`/`risk_ramp_full`) all at once. **Watch the override trap**:
  `_draft_from_dict` lets any of those same keys, if still present elsewhere in
  the `draft:` block, win over the preset field-by-field — config.yml comments
  them out once `spice_level` is set, for exactly this reason. `arbitrage_weight`
  stays excluded from every level (retired, confirmed harm — see its own
  docstring in `ffbot/config.py`) regardless of `spice_level`. No `spice_level`
  key at all falls straight through to the bare 0.0 defaults (`DraftConfig`'s
  dataclass defaults), unlike `season:` below, which defaults to level 3 even
  with the key absent — the two blocks' fallback behavior genuinely differs.
- **`season:`** — the weekly manager's dial. `spice_level` (1–5: Chalk through
  Chaos, defaults to 3 even when the key is omitted entirely) sets every derived
  weight at once (weather/Vegas/volatility/upside-lean/streaming) via
  `SeasonConfig.from_spice_level`; hand-edit any one signal afterward to
  override just it without losing the rest of the level's shape.
  Also here: `ros_blend` (season-long vs. this-week value in waiver ranking —
  a real rest-of-season number under `projection_source: sleeper`, the frozen
  board's rescaled estimate otherwise), `min_stream_spots`, `blocking_hold_bonus`,
  `denial_weight` + `denial_opponent_boost` + `denial_seed_window` (tactical
  denial — see below), `denial_priority_floor`, `priority_value`, and
  `venue_disruption_weight` (playing outside a typical NFL setting — deliberately
  **not** part of any spice level, since unlike weather/Vegas there's no real
  evidence base for how much an international game moves output; it stays a
  hand-set 0.0 unless you turn it on yourself).

### `league.yml` — your league's actual rules

Copy `league.example.yml` to `league.yml` and fill in Sleeper's League Settings
values (`whoami.py` prints your league's raw `scoring_settings` for reference) —
passing/rushing/receiving/misc/bonuses/kicking/defense scoring, including
distance-tiered field goals and a points-allowed ladder if your league uses one.
Run `scripts/scoring_check.py` after editing it — it prints exactly what's exact,
what's estimated, and what no FantasyPros export column can express, before
anything downstream touches it. No `league.yml` at all is a no-op: every board
recomputation falls back to FantasyPros' own consensus PPR scoring, unchanged.

Also here (all optional, all no-ops until set):

- **`playoff_teams`, `waiver_type` (`faab`/`rolling`), `lock_eliminated_teams`** —
  structural league facts valuation code needs.
- **`week`, `my_team`, `my_opponent`, `teams:`** — curated weekly standings.
  `teams:` entries (`name`, `record`, `seed`, `waiver_priority`, `eliminated`) feed
  tactical denial: `season.denial_weight` on its own weighs a rival by seed
  distance from the playoff bubble; `my_opponent` (who you're playing *this* week)
  adds `denial_opponent_boost` on top for that specific rival; `my_team` + `season.
  denial_seed_window` add a second boost for anyone within that many seeds of your
  own once the season reaches its playoff push (the final few weeks of
  `regular_season_weeks`). All three layer additively and are individually
  no-ops — set only the ones you want. Denial itself needs `league_rosters.yml`
  imported too, or every denial function stays an exact no-op regardless of these
  weights — `scripts/import_league_rosters.py --live` fetches it straight from
  Sleeper (no auth, exact `player_id` join, no fuzzy name matching needed); `--paste`
  is the manual fallback.

### `config.local.yml` — the GUI's settings overlay

`config.yml` is hand-narrated with comments a YAML round-trip would destroy, so
the web GUI's Settings page never writes there. Instead, `Config.load` merges an
optional sibling `config.local.yml` on top (deep-merged, not replaced wholesale —
a local override of just `draft: {num_teams: 10}` doesn't blank out the rest of
that block). Gitignored; missing entirely is the common case. Safe to hand-edit
too, or delete to fall back to whatever `config.yml` says.
