# Connecting the agent to Yahoo Fantasy

The credential chain, end to end. [SETUP.md](SETUP.md) covers getting *access*;
this covers using it once you have it.

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

---

## Step 0 — The blocker (do this first)

**You cannot connect anything until Yahoo grants your app the Fantasy Sports
scope.** It was removed from the self-serve app form, so the application at
https://sports.yahoo.com/developer/access/ is the only route. Steps 1–5 below
will all fail with 401/403 until that lands.

Wording for the application is in [SETUP.md](SETUP.md).

---

## Step 1 — Credentials into `.env`

Create `.env` in the project root:

```
YAHOO_CLIENT_ID=<Client ID / Consumer Key>
YAHOO_CLIENT_SECRET=<Client Secret / Consumer Secret>
```

Already gitignored. **Don't paste the secret into chat** — it is
password-equivalent for your fantasy account.

If you registered a Redirect URI other than `https://localhost:8080`, add:

```
YAHOO_REDIRECT_URI=<whatever you registered>
```

It has to match **character for character**, or the token exchange fails.

---

## Step 2 — Authorize once

```bash
.venv/bin/python scripts/authorize.py
```

The script prints a Yahoo URL. Open it, and:

1. **Sign in as the Yahoo account that owns your fantasy team.** This is the
   single most common way to waste an afternoon here — any other Yahoo account
   authorizes perfectly and then finds zero leagues, which reads like a bug in
   the code and isn't.
2. Approve the consent screen.
3. The browser will fail to load `https://localhost:8080/?code=…`. **That is
   expected** — nothing is running there, and nothing needs to be. Copy the
   whole address bar.
4. Paste it back into the script.

The refresh token is written to `.env` and deliberately never printed, so it
stays out of terminal scrollback and screen shares.

That token is the season-long key. Everything after this reuses it; access
tokens last an hour and are minted as needed.

---

## Step 3 — Verify the connection

```bash
.venv/bin/python scripts/whoami.py
```

This exercises the entire read path — refresh, league discovery, your team key,
roster, FAAB balance. Getting a roster printed here means the connection is
genuinely working, not just that the token parsed.

It prints the two values to copy into `config.yml`:

```yaml
league_id: "123456"
team_key: "461.l.123456.t.7"
```

---

## Step 4 — Hand the credentials to GitHub Actions

Three secrets, so the runner can authenticate without your laptop:

```bash
gh secret set YAHOO_CLIENT_ID
gh secret set YAHOO_CLIENT_SECRET
gh secret set YAHOO_REFRESH_TOKEN
```

Each prompts for the value — nothing hits your shell history.

### Step 4b — The PAT, and why it exists

Yahoo may hand back a **new** refresh token on any refresh, and its docs are
explicit that the client must store it and discard the old one. Miss one and
the agent is locked out for the rest of the season, recoverable only by
re-running Step 2 by hand — which it cannot do at 3am on a Wednesday.

So each run writes a rotated token straight back to the repo secret, before
doing anything else. The built-in Actions token **cannot write secrets**, so
this needs a fine-grained PAT:

1. https://github.com/settings/personal-access-tokens/new
2. Repository access → **Only select repositories** → this repo
3. Permissions → Repository permissions → **Secrets: Read and write**
4. Create, copy, then:

```bash
gh secret set GH_PAT
```

---

## Step 5 — Confirm it works unattended

Once the workflows exist, run one manually with `dry_run` on and read the log.
A successful dry run that lists your real roster is what "connected" means.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401`/`403` listing leagues | App lacks the Fantasy Sports scope | Step 0 — nothing else will help |
| "No leagues found" | Authorized as the wrong Yahoo account | Re-run Step 2, sign in as the team owner |
| `invalid_grant` on exchange | Code already used, or expired | Codes are one-shot and short-lived — restart Step 2 |
| `redirect_uri` mismatch | `.env` value ≠ what's registered on the app | Make them identical, including scheme and port |
| `invalid_client` | Wrong Client Secret | Re-copy from the Yahoo app page |
| Worked for weeks, now `401` | Rotated refresh token wasn't stored | Check the PAT (Step 4b), then re-run Step 2 |
| Browser warns about the certificate | Expected — nothing is served on `localhost:8080` | Ignore it and copy the address bar |

### A note on the token endpoint

Yahoo documents the client credentials as **body parameters** but some apps
require **HTTP Basic**. Which one yours wants can't be determined without live
credentials, so [ffbot/auth.py](ffbot/auth.py) tries the documented form and
falls back to Basic on a 400/401 automatically. If you see `invalid_client`,
it's a wrong secret, not the wrong auth style.

---

## What is already built and tested

- Token refresh, rotation detection, and write-back — `ffbot/auth.py`
- The `sc` shim `yahoo_fantasy_api` needs — no `yahoo_oauth` dependency
- Lineup optimizer and drop/FAAB guardrails — `ffbot/lineup.py`, `ffbot/policy.py`
- 71 tests, including rotation handling and an optimality cross-check

Blocked on Step 0: the tick runner, the workflows, and waiver claims.
