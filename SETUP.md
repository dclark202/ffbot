# What I need from Yahoo to get started

> **Revised after seeing the actual form.** Yahoo has removed Fantasy Sports from the self-serve API permissions list. The only route to API access is now an application that Yahoo reviews by hand. This gates **read as well as write** — until approved, there is no API access at all.

Do Step 1 and Step 2 today. Everything else waits on Yahoo.

---

## Step 1 — Create the app anyway (to get an App ID)

Go to **https://developer.yahoo.com/apps/create/** and fill it in exactly as you had it:

| Field | Value |
|---|---|
| Application Name | `ff-automaton` |
| Description | `fantasy football bot` |
| Homepage URL | *(blank)* |
| Redirect URI(s) | `https://localhost:8080` |
| OAuth Client Type | **Confidential Client** ✓ *(correct — we hold a secret in GitHub Actions)* |
| API Permissions | **Leave both unchecked.** Neither OpenID Connect nor TW Auction is relevant. |

> **Sign in with the Yahoo account that owns your fantasy team.** The token is scoped to whoever authorizes. A different Yahoo address will authorize cleanly and then find zero leagues — it looks like a code bug and isn't.

Click **Create App**. Record three things: **App ID**, **Client ID (Consumer Key)**, **Client Secret (Consumer Secret)**.

The app is useless on its own right now — it has no Fantasy Sports scope. The point is the **App ID**, which the access application asks for. Yahoo attaches the Fantasy Sports permission to this existing app if they approve you.

*(Correction to my earlier draft: I said to pick "Installed Application" and check "Fantasy Sports." Neither option exists on the current form. Confidential Client is right.)*

---

## Step 2 — Apply for Fantasy Sports API access ← the real gate

Go to **https://sports.yahoo.com/developer/access/**

| Field | What to put |
|---|---|
| Name / Email | Yours |
| Organization | Your name, or "Independent developer" — there's no personal/hobby option |
| App ID | The App ID from Step 1 |
| Expected Users | **Small (< 1,000 users)** |
| App Description | See below |
| Notes | See below — **this is where write access has to be requested** |

The form states access is **read-only by default** and that read/write requires explaining why. So the Notes field is doing the real work here.

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

No turnaround time is published. Apply today — it's free, and it's the long pole.

---

## Step 3 — Credentials (do now, harmless)

**Don't paste the Client Secret into this chat.** It's password-equivalent for your fantasy account, and chat history is a bad place for it.

Create `.env` in this directory:

```
YAHOO_CLIENT_ID=your_client_id_here
YAHOO_CLIENT_SECRET=your_client_secret_here
YAHOO_LEAGUE_ID=123456
```

I'll gitignore it before any git history exists.

---

## Step 4 — Your league ID (do now)

Open your league on Yahoo Fantasy. The URL:

```
https://football.fantasysports.yahoo.com/f1/123456
```

`123456` is the league ID. Confirm it's your **2026** league, not last season's.

---

## Blocked until Yahoo approves

- One-time OAuth authorization (needs the Fantasy Sports scope to exist)
- `whoami` / any read from the API
- Lineup writes, waiver claims
- GitHub Actions wiring, PAT for token write-back

---

## What to tell me

1. **App created** — App ID recorded
2. **Application submitted** — and the date, so we can chase it
3. Credentials in `.env`, league ID confirmed
4. Anything Yahoo says back — approval, denial, or a request for more detail
