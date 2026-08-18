# Training packs: human feedback at volume

Every tuning question this repo has hit was answered from a sample of one — a
single mock draft where a recommendation looked wrong. The
[single-draft-evidence](../../CLAUDE.md) lesson from that era is blunt: every
change proposed on one draft measured neutral-to-negative once actually
backtested. What was missing was a second, cheap source of judgment at
volume: a human reviewer with no stake in the outcome, looking at many
situations rather than the one that happened to feel off.

A **training pack** is a set of synthetic draft situations — each one the
exact recommendation table, partial roster, and opponent state the live
draft room would have shown, frozen at one of "my" picks in a simulated
mock draft — exported as a single standalone HTML file a reviewer opens in
any browser, no server and no Python required. They rank their own top 3,
say whether they agree with the engine, and download a small JSON of
answers to send back. A report script grades those answers against the
frozen table and prints where the two systematically disagree.

**A finding here is a hypothesis, never a change by itself.** Anything this
report surfaces has to clear `scripts/backtest_draft.py` before a weight
moves — the exact discipline the single-draft-evidence lesson exists to
enforce. This tool multiplies the number of human judgments feeding
hypotheses; it does not skip the step that turns a hypothesis into a change.

## Workflow

```bash
# 1. Generate a pack -- a set of full mock drafts with bots in EVERY seat
#    (including "mine", at a different spice level than the tool's own
#    config, so the roster isn't a replay of the engine's own choices).
.venv/Scripts/python scripts/make_training_pack.py \
    --count 30 --drafts 6 --seed 7 \
    --out training/packs/dad-01.json \
    --export training/dad-01.html \
    --label "Round 1"

# 2. Send training/dad-01.html to the reviewer -- email, a shared drive,
#    a USB stick. It needs nothing else: no server, no Python, no network.

# 3. They click through it, rank up to 3 players per situation, mark
#    agree/close/disagree/none, add notes if they want, and click
#    "Download responses". They send that file back to you.

# 4. Grade it.
.venv/Scripts/python scripts/training_report.py \
    --pack training/packs/dad-01.json \
    --responses training/responses/dad-01-dad.json
```

`training/` is gitignored end to end — packs, exports, responses, and
graded feedback are all generated and personal (a reviewer's name and notes
live in there). `web/train.html` and `web/draft_render.js`, the two source
files behind the reviewer page, are tracked normally.

## How a pack is built

`scripts/make_training_pack.py` runs `--drafts` full mock drafts
(`ffbot.draft.DraftState` + `scripts/mock_draft.py`'s `_bot_pick`, exactly
the machinery `scripts/mock_draft.py`/`scripts/gui.py --mock` already use).
Every seat is a bot, including mine — at `--my-spice` (default 2,
deliberately different from the config's own level), so the roster a
reviewer sees is a plausible human's roster instead of a replay of the
engine's own top pick every time. That distinction matters: reviewing "did
the engine agree with itself" would be worthless.

At each of my picks, the tool freezes `webapi.draft_state_json()` — the
identical JSON the GUI's draft room renders from, computed under the
**real**, tool-configured engine (`cfg`, not the bot's spice) — via
`ffbot.training.build_scenario`. `ffbot.training.stratify` then selects
`--count` of those candidate situations, spread across round (early/mid/
late/endgame) and the engine's own top-recommended position, so a 30-
situation pack doesn't read as "twelve WR situations in a row" just because
that's what the source drafts happened to generate.

## The reviewer page

`web/train.html` (templated) plus `web/draft_render.js` (the rendering
functions shared with the live draft room in `web/draft.html`, so a
situation looks exactly like it would have looked live) are inlined by
`ffbot/training_export.py` into one self-contained `.html` file with no
external references — no `/style.css`, no `/common.js`, nothing fetched
over the network. Answers autosave to the browser's `localStorage`, keyed
by the pack's id, so closing the tab mid-pack loses nothing and reopening
the same file resumes where they left off.

`--blind` hides the Val/Δ/P columns and the Why text until the reviewer
ranks at least one player, then reveals them next to their answer — useful
when you want a read that isn't anchored to the engine's own stated
reasoning. Off by default: the normal mode reviews the recommendations as
given, since that's what the tool actually shows in a live draft.

Below the (much shorter) recommendation table sits a **Player Board**: the
full draftable universe for the pack, filterable by position and sortable by
clicking Proj or ADP (defaulting to Proj, highest first), with already-taken
players grayed out and inert. It's built once per pack
(`ffbot.training.build_pack`'s `player_board`) rather than once per
scenario, since the board itself doesn't change across a generation run —
only who's taken does (`scenario["taken_keys"]`). Clicking
an available row ranks that player exactly like clicking a recommendation
row does (same `toggleChoice`), so a reviewer who'd rather have someone
outside the top-N recommendations can name them directly instead of only
describing them in a note.

"None of these — I'd take someone else" stays a first-class verdict for
whatever the Player Board still can't cover (a reviewer who doesn't want to
name anyone specific, or a keeper/edge case outside the frozen board), and
grades to `rank_in_table: None` — the same thing a real draft report
records when a human's actual pick wasn't in the engine's own table at all
(see `ffbot/draft_report.py`).

## Grading

`ffbot.training.grade_response` reuses `ffbot.draft_report.taken_block` —
the exact function a real draft pick is graded with — so a training verdict
and a live draft-report entry always describe a disagreement in the same
units: `rank_in_table`, `was_top_recommendation`, `p_best_of_taken`,
`value_gap_to_top`. `tests/test_training.py`'s
`TestGradeResponse::test_grade_response_reuses_taken_block_shape` asserts
this structurally, the same anti-drift discipline `rec_row` and
`draft_report`/`week_log` already follow.

`scripts/training_report.py` merges a returned responses file back onto its
pack (`ffbot.training.merge_responses`), writes the graded records to
`training/feedback/<pack_id>-<reviewer>.jsonl` (append-only, one record per
answer), and prints:

- **Overall** — agree rate, how often the reviewer's #1 *is* the engine's
  #1, the mean rank and value gap of their #1 pick.
- **By round** — where the disagreement actually concentrates.
- **By confidence band**, split on the scenario's own
  `confidence.effective_options` (standout / a few options / toss-up).
  Disagreement on toss-ups is expected noise; disagreement on picks the
  engine was *confident* about is the signal worth a second look.
- **Positional bias** — a matrix of the engine's top-recommended position
  vs. the reviewer's own #1 position. This is the table that would say "he
  takes QB two rounds earlier than we do," if that pattern is real.
- **Biggest disagreements** — the largest `value_gap_to_top` gaps, with
  notes, as concrete starting points to look at.

## What to do with a finding

Nothing, directly. A pattern in the report — "disagreement clusters at
confident RB picks in rounds 3-5" — is a hypothesis about where the engine
might be wrong, not evidence that it is. Turn it into a specific,
falsifiable change (a weight, a threshold) and run it through
`scripts/backtest_draft.py` against real NFL seasons before it touches
`config.yml`. See [BACKTEST.md](BACKTEST.md) for that harness and its
leakage guarantees, and the single-draft-evidence memory for exactly what
goes wrong when this step gets skipped.
