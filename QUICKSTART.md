# Draft day quickstart

Everything here works offline. No Yahoo API access is needed.

---

## Week-of checklist

Data goes stale — projections and ADP move all through camp, and the intel file
records when it was last researched. **[REFRESH.md](REFRESH.md) is the full
step-by-step plan**: a light pass a week out, the complete refresh the day
before (new CSVs → intel re-research → re-export → re-paste into Yahoo → dry
run), and a news-sweep-only rule for draft day itself.

---

## The week before

**1. Refresh the board.** Re-download the five FantasyPros exports into `draft/` if
they're more than a few days old — projections move a lot through camp. Same filenames,
same PPR setting. (Sources and settings are listed in the `draft.board_csv` comment in
[config.yml](config.yml).)

**2. Regenerate the exports.**

```bash
.venv/Scripts/python scripts/draft_export.py
```

**3. Paste `draft/board.txt` into Yahoo's pre-draft custom rankings.** This is your
safety net and the single highest-value thing you can do before draft day. There is no
API for it — it's a manual step in Yahoo's web UI — but it means if you get disconnected,
lose power, or step away, **Yahoo's autopick follows your board instead of its own.**
`draft/board_by_pos.txt` is the same list split by position, which is easier to enter.

**4. Print or open `draft/cheatsheet.txt`** — players grouped by position and tier, as a
fallback if the laptop dies entirely.

---

## Draft day

Yahoo randomizes draft position and tells you shortly before the draft. Once you know it:

```bash
.venv/Scripts/python scripts/draft.py --slot 4
```

Put the terminal and Yahoo's draft room side by side. If you got the slot wrong or it
changes, type `me 7` — no restart needed.

If it crashes or you close it by accident, **nothing is lost**:

```bash
.venv/Scripts/python scripts/draft.py --slot 4 --resume
```

Every command you type is written to `draft_log.jsonl` as it happens, and `--resume`
replays it.

---

## The one rule that matters

**Record every pick, including everyone else's.**

The assistant figures out whose turn it is by counting picks. If you skip one, the count
drifts, and from then on it thinks your picks belong to opponents and vice versa — your
roster goes wrong and so does every recommendation.

If someone picks and you don't catch who it was, type `x`. That advances the count
without claiming to know the player, which keeps everything aligned.

---

## Commands

| Type this | What it does |
|---|---|
| `jefferson` | Records a pick — infers whose it is from the pick number |
| `*jefferson` | Explicitly **my** pick |
| `-jefferson` | Explicitly an **opponent's** pick |
| `x` | A pick happened, I don't know who — keeps the count right |
| `u` | Undo the last entry |
| `?jefferson` | Look a player up without drafting them |
| `s` | Cycle the sort: value → vor → adp → urgency |
| `p RB` | Show only RBs. `p` on its own clears the filter |
| `me 7` | Fix your draft slot |
| `1` `2` `3`… | Choose from the numbered list when a name is ambiguous |
| `q` | Quit |

You only need partial names — `jeffer` is enough. If several players match equally well
you'll get a numbered menu; type the number.

---

## Reading the table

```
#  PLAYER              POS TM  BYE     PROJ    VOR   NEED    VAL   ADP  SURV  WHY
1  Ja'Marr Chase       WR  CIN 6      336.1  146.1  146.1  197.2     3   73%  fills a need (+146.1)
```

| Column | Meaning |
|---|---|
| **PROJ** | Projected fantasy points for the whole season |
| **VOR** | Value over replacement — points above a freely-available player at that position. **This is the real measure of worth, not PROJ.** A 300-point QB can be worth less than a 250-point RB |
| **NEED** | What this player adds *to your specific roster right now*. Starts equal to VOR and falls toward zero as you fill a position |
| **VAL** | What the assistant actually ranks on: NEED plus a bonus for bench depth |
| **ADP** | Where this player typically goes. Much later than their rank = a bargain |
| **SURV** | Chance they're still available at your next pick. Low = take them now or lose them |
| **WHY** | Plain-English reason, including tier warnings and bye conflicts |

**Alerts** below the table flag positional runs, tier cliffs (a big point drop coming),
and bye-week collisions on your roster.

The short version: **take the player at the top of the list.** Deviate when the WHY
column or an alert gives you a reason to.

---

## Two things worth knowing before the clock is running

**Ambiguous names open a numbered menu — that's normal.** Unless a name matches
exactly one player, you get a list (with position, team, projection, and ADP) and pick
by number. `*chase` lists Chase Brown, Chase McLaughlin, *and* Ja'Marr Chase — type
`3`. Your `*`/`-` intent carries through to the number you pick. Only a truly
unambiguous query (one match, or an exact full name) records instantly.

**SURV shows 100% while you're on the clock.** That's correct, not a bug — it's the
chance a player survives until *your next pick*, and when that's the pick you're making
right now, the answer is trivially "yes." The column becomes meaningful again as soon as
someone else is picking.

---

## If Yahoo approves API access before your draft

The live-sync path is already built and needs no new code. `draft_results()` does return
picks mid-draft, so it genuinely works. Two steps:

```bash
# 1. Dump your league's players to JSON, then build the id map
.venv/Scripts/python scripts/draft_export.py --yahoo-players yahoo.json

# 2. Run with sync on
.venv/Scripts/python scripts/draft.py --slot 4 --sync
```

Manual entry still wins over anything sync reports, so you can keep typing and let sync
fill the gaps. If setup fails for any reason it prints a warning and continues offline —
a broken sync will never take down a working draft.

Until then, typing picks in is the whole workflow, and it's genuinely fine: you have
11 opponent picks between your turns and roughly a minute each to enter them.
