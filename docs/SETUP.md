# Setup: Yahoo access and configuration

Two independent things live here: getting Yahoo API access (optional — everything in
this repo works offline without it), and the full configuration reference for
`config.yml`/`league.yml`. Do the Yahoo steps whenever you like; the config
reference applies regardless of whether Yahoo access ever lands.

---

## Part 1 — Yahoo API access

### The blocker (read this first)

**Yahoo removed Fantasy Sports from the self-serve API permissions list.** The only
route to access — read *or* write — is an application Yahoo reviews by hand at
https://sports.yahoo.com/developer/access/. Until that's approved, nothing in this
section past Step 2 will work; every call fails with 401/403.

Apply as early as possible — no turnaround time is published, and it's the long
pole. Everything else here works fine while you wait.

```
Yahoo app (Client ID + Secret)
        │
        ▼
scripts/authorize.py  ──►  refresh token  (long-lived, the season-long key)
        │
        ▼
scripts/whoami.py     ──►  league_id + team_key  ──►  config.yml
        │
        ▼
GitHub secrets        ──►  Actions runs unattended
```

### Step 1 — Create the app (to get an App ID)

Go to **https://developer.yahoo.com/apps/create/**:

| Field | Value |
|---|---|
| Application Name | your choice, e.g. `ff-automaton` |
| Description | `fantasy football bot` |
| Homepage URL | *(blank)* |
| Redirect URI(s) | `https://localhost:8080` |
| OAuth Client Type | **Confidential Client** ✓ (correct — the secret lives in a `.env` file / GitHub secret, not exposed client-side) |
| API Permissions | leave both unchecked — neither OpenID Connect nor TW Auction is relevant |

**Sign in with the Yahoo account that owns your fantasy team.** The token is scoped
to whoever authorizes; a different account authorizes cleanly and then finds zero
leagues, which reads like a bug and isn't.

Click **Create App** and record three things: **App ID**, **Client ID (Consumer
Key)**, **Client Secret (Consumer Secret)**. The app has no Fantasy Sports scope
yet — the point of this step is just the App ID, which Step 2's application asks
for. Yahoo attaches the scope to this existing app if they approve you.

### Step 2 — Apply for Fantasy Sports API access ← the real gate

Go to **https://sports.yahoo.com/developer/access/**:

| Field | What to put |
|---|---|
| Name / Email | Yours |
| Organization | Your name, or "Independent developer" — there's no personal/hobby option |
| App ID | The App ID from Step 1 |
| Expected Users | **Small (< 1,000 users)** |
| App Description | See below |
| Notes | See below — **this is where write access has to be requested** |

Access is read-only by default; read/write requires explaining why, so the Notes
field is doing the real work.

**App Description:**

> A personal tool that manages a single fantasy football team in one private
> league. It reads my roster and player status, then sets my starting lineup
> and submits waiver claims on my behalf so that injured or inactive players
> are never left in my starting lineup.

**Notes:**

> Requesting read/write scope. Single user (me), one team, one private league —
> not a public or commercial product, no redistribution of Yahoo data, and no
> multi-account usage. Write access is required for two endpoints specifically:
> roster position changes (PUT team/roster) and add/drop transactions
> (POST league/transactions). Expected request volume is a few dozen calls per
> week during the NFL season. Read-only access would not serve the use case,
> since the entire purpose is automating lineup changes I would otherwise make
> by hand in the Yahoo app.

### Step 3 — Credentials into `.env`

Create `.env` in the project root — already gitignored:

```
YAHOO_CLIENT_ID=<Client ID / Consumer Key>
YAHOO_CLIENT_SECRET=<Client Secret / Consumer Secret>
```

**Don't paste the Client Secret into chat** — it's password-equivalent for your
fantasy account.

If you registered a Redirect URI other than `https://localhost:8080`, also add
`YAHOO_REDIRECT_URI=<whatever you registered>` — it must match **character for
character**, or the token exchange fails.

Your league ID does **not** go in `.env` — it goes in `config.yml` (Step 6 below
gets it for you automatically). `.env` holds only app-level credentials, which are
the same across every league you're in; `config.yml` holds this specific league.

### Step 4 — Your league ID (optional, works either way)

You can get this from Step 6 below once Yahoo access lands, or read it straight off
your league's URL right now:

```
https://football.fantasysports.yahoo.com/f1/123456
```

`123456` is the league ID. Confirm it's the current season's league, not last
year's, then put it in `config.yml`:

```yaml
league_id: "123456"
```

### Step 5 — Authorize once (needs Step 2 approved)

```bash
.venv/Scripts/python scripts/authorize.py
```

The script prints a Yahoo URL. Open it:

1. **Sign in as the Yahoo account that owns your fantasy team** (see Step 1's
   note — this is the single most common way to lose an afternoon here).
2. Approve the consent screen.
3. The browser will fail to load `https://localhost:8080/?code=…`. **That's
   expected** — nothing is running there. Copy the whole address bar.
4. Paste it back into the script.

The refresh token is written to `.env` and never printed, so it stays out of
terminal scrollback and screen shares. It's the season-long key; access tokens
last an hour and are minted from it as needed.

### Step 6 — Verify the connection

```bash
.venv/Scripts/python scripts/whoami.py
```

This exercises the entire read path — refresh, league discovery, your team key,
roster, FAAB balance. A printed roster means the connection genuinely works, not
just that the token parsed. It prints the two values `config.yml` needs:

```yaml
league_id: "123456"
team_key: "461.l.123456.t.7"
```

### Step 7 — Hand credentials to GitHub Actions (for unattended runs)

Three secrets, so a runner can authenticate without your laptop:

```bash
gh secret set YAHOO_CLIENT_ID
gh secret set YAHOO_CLIENT_SECRET
gh secret set YAHOO_REFRESH_TOKEN
```

Each prompts for the value — nothing hits shell history.

**The PAT, and why it exists.** Yahoo may hand back a **new** refresh token on any
refresh, and its docs are explicit that the client must store it and discard the
old one. Miss one and the agent is locked out for the rest of the season, until
Step 5 is re-run by hand — which an unattended job cannot do at 3am on a
Wednesday. So each run writes a rotated token straight back to the repo secret,
before doing anything else — see `ffbot/auth.py`'s "refresh-token persistence must
happen before any other work" invariant. The built-in Actions token **cannot
write secrets**, so this needs a fine-grained PAT:

1. https://github.com/settings/personal-access-tokens/new
2. Repository access → **Only select repositories** → this repo
3. Permissions → Repository permissions → **Secrets: Read and write**
4. Create, copy, then `gh secret set GH_PAT`

### Step 8 — Confirm it works unattended

Once the scheduled workflows exist, run one manually with `dry_run: true` and read
the log. A successful dry run that lists your real roster is what "connected"
means.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401`/`403` listing leagues | App lacks the Fantasy Sports scope | Step 2 — nothing else will help |
| "No leagues found" | Authorized as the wrong Yahoo account | Re-run Step 5, sign in as the team owner |
| `invalid_grant` on exchange | Code already used, or expired | Codes are one-shot and short-lived — restart Step 5 |
| `redirect_uri` mismatch | `.env` value ≠ what's registered on the app | Make them identical, including scheme and port |
| `invalid_client` | Wrong Client Secret | Re-copy from the Yahoo app page |
| Worked for weeks, now `401` | Rotated refresh token wasn't stored | Check the PAT (Step 7), then re-run Step 5 |
| Browser warns about the certificate | Expected — nothing is served on `localhost:8080` | Ignore it and copy the address bar |

**A note on the token endpoint:** Yahoo documents the client credentials as
**body parameters**, but some apps require **HTTP Basic**. Which one yours wants
can't be determined without live credentials, so [ffbot/auth.py](../ffbot/auth.py)
tries the documented form and falls back to Basic on a 400/401 automatically. If
you see `invalid_client`, it's a wrong secret, not the wrong auth style.

### What's already built and tested, on this front

Token refresh, rotation detection, and write-back (`ffbot/auth.py`); the `sc` shim
`yahoo_fantasy_api` needs (no `yahoo_oauth` dependency). Blocked on Step 2: the
tick runner, the scheduled workflows, and waiver claims.

---

## Part 2 — Configuration reference

Everything the agent uses to make a judgment call lives in `config.yml` (behavior)
and `league.yml` (your league's actual scoring rules), so tuning strategy is a
config edit, never a code change. Both are heavily commented in place — this
section is a map of what's where, not a restatement of every comment.

### `config.yml` — behavior

- **Top level** — `league_id`/`team_key` (Setup Steps 4/6), `dry_run` (compute and
  log everything, never write to Yahoo — leave on until you've read a few weeks of
  logs), `roster_positions` (your league's starting-slot layout; `whoami.py` prints
  this from Yahoo once connected, and it's also the draft assistant's default
  before that).
- **`projection:`** — how a *missing* weekly projection gets estimated, and how
  injury status discounts one that exists. Not scoring rules.
- **`drops:` / `faab:`** — the guardrails on the one irreversible in-season action
  (dropping a player) and on FAAB bid sizing.
- **`draft:`** — everything the draft assistant needs: league shape (`num_teams`,
  `my_slot`, `rounds`, `order` — `snake` or `linear`), the FantasyPros CSV sources,
  replacement-level/tiering/ADP-survival tuning, `position_caps` (hard ceilings)
  and `position_targets` (soft roster-shape targets), and the "how contrarian"
  edge-layer weights (`upside_weight`, `risk_weight`, `volatility_weight`,
  `stack_bonus`, `arbitrage_weight`, `scoring_arbitrage_weight`) — every one
  defaults to 0.0 in code, so this block is the entire spice dial for the draft.
- **`season:`** — the weekly manager's dial. `spice_level` (1–5: Chalk through
  Chaos) sets every derived weight at once (weather/Vegas/volatility/upside-lean/
  streaming) via `SeasonConfig.from_spice_level`; hand-edit any one signal
  afterward to override just it without losing the rest of the level's shape.
  Also here: `ros_blend` (season-long vs. this-week value in waiver ranking),
  `min_stream_spots`, `blocking_hold_bonus`, `denial_weight` +
  `denial_opponent_boost` + `denial_seed_window` (tactical denial — see below),
  `denial_priority_floor`, `priority_value`, and `venue_disruption_weight`
  (playing outside a typical NFL setting — deliberately **not** part of any spice
  level, since unlike weather/Vegas there's no real evidence base for how much an
  international game moves output; it stays a hand-set 0.0 unless you turn it on
  yourself).

### `league.yml` — your league's actual rules

Copy `league.example.yml` to `league.yml` and fill in Yahoo's Settings page
values — passing/rushing/receiving/misc/bonuses/kicking/defense scoring, including
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
  no-ops — set only the ones you want. Denial itself needs
  `league_rosters.yml` (`scripts/import_league_rosters.py`) imported too, or every
  denial function stays an exact no-op regardless of these weights.

### `config.local.yml` — the GUI's settings overlay

`config.yml` is hand-narrated with comments a YAML round-trip would destroy, so
the web GUI's Settings page never writes there. Instead, `Config.load` merges an
optional sibling `config.local.yml` on top (deep-merged, not replaced wholesale —
a local override of just `draft: {num_teams: 10}` doesn't blank out the rest of
that block). Gitignored; missing entirely is the common case. Safe to hand-edit
too, or delete to fall back to whatever `config.yml` says.
