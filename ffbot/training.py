"""Synthetic draft situations for a human reviewer, and grading their answers.

`ffbot/draft_report.py` exists because reconstructing what the engine said
after the fact is slow and depends on the board being rebuildable exactly.
This module answers a different question the same way: instead of asking
"what did the engine say at a real pick", it freezes what the engine WOULD
say at a synthetic one, hands it to a human with no stake in the outcome,
and grades their answer against the exact table they were shown.

A "scenario" is nothing new: it's `webapi.draft_state_json()`'s output --
the same header, recommendation table, roster, opponents, and draft log the
live draft room renders -- captured at one of MY picks in a mock draft where
every seat (including mine) is a bot, so the partial roster looks like a
plausible human's roster rather than a replay of the engine's own choices,
plus `taken_keys` (who's already off the board at that moment). A "pack"
bundles several scenarios, the full draftable universe once
(`player_board`), and the config/board provenance that produced them,
mirroring `draft_report.build_report`'s header. `grade_response` reuses
`draft_report.taken_block` so a human's answer lands in exactly the shape a
real draft pick does -- see that function's docstring for why a second copy
of those keys would be a bug waiting to happen.

Every finding this produces is a hypothesis, never a change by itself --
see docs/dev/TRAINING.md. `scripts/backtest_draft.py` is the actual gate.

Pure except for `read_pack`/`write_pack`, the same split `draft_report`
and `week_log` already use: builders never do I/O, and the one write
function never raises into a caller (a training run must not die because a
pack file couldn't be written).
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .draft_report import _TUNING_FIELDS, taken_block
from .draft_ui import UiState
from .webapi import _bp_team, draft_state_json

SCHEMA = 1

# Round buckets a scenario's pick number is sorted into for stratification --
# deliberately coarser than exact rounds. Early picks (1-2) are the highest-
# stakes decisions and the ones most worth checking; 10-14 is the endgame
# (K/DEF/QB timing), the other place this repo's own tuning notes say the
# engine has been wrong before (see the draft-scarcity-term memory). A
# scenario past round 14 falls into the last bucket rather than being
# dropped -- stratify() only needs buckets to compare fullness across, not
# an exhaustive taxonomy.
_ROUND_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (1, 2, "R1-2"),
    (3, 5, "R3-5"),
    (6, 9, "R6-9"),
    (10, 999, "R10+"),
)


def _round_bucket(round_: int) -> str:
    for lo, hi, label in _ROUND_BUCKETS:
        if lo <= round_ <= hi:
            return label
    return _ROUND_BUCKETS[-1][2]  # pragma: no cover -- buckets are exhaustive from 1


def build_scenario(
    ui_state: UiState,
    *,
    scenario_id: str,
    source_draft: str,
) -> dict:
    """One frozen situation: `webapi.draft_state_json(ui_state)` plus enough
    identity to place it in a pack and stratify a set of candidates.

    `source_draft` names which simulated draft produced this candidate (e.g.
    "d3") -- `stratify` uses it to cap any single draft's contribution to a
    pack, so 30 situations that all trace back to one bot run never masquerade
    as 30 independent looks at the engine.

    `taken_keys` is every board key already off the board (mine and every
    opponent's) at the moment this was frozen -- the reviewer page's Player
    Board panel grays these out. Derived straight from `DraftState.taken_keys`
    rather than re-parsed from `state["draft_log"]`, which is the same set in
    practice here (no unknown picks happen in a synthetic draft) but is one
    fewer thing the reviewer page has to reconstruct.
    """
    state = draft_state_json(ui_state)
    header = state["header"]
    top_rec_position = state["recommendations"][0]["position"] if state["recommendations"] else None
    return {
        "id": scenario_id,
        "source_draft": source_draft,
        "round": header["round"],
        "pick": header["pick"],
        "my_slot": header["my_slot"],
        "round_bucket": _round_bucket(header["round"]),
        "top_rec_position": top_rec_position,
        "taken_keys": sorted(ui_state.draft.taken_keys()),
        "state": state,
    }


def stratify(candidates: Sequence[dict], count: int, seed: int | None = None) -> list[dict]:
    """Pick `count` of `candidates`, spread across round bucket and the
    engine's top-recommended position, so a pack never reads as "twelve WR
    situations in a row" just because that's what the source drafts happened
    to generate at that point.

    Deterministic given `seed`: candidates are shuffled once (so ties within
    a bucket don't always favor draft order) and buckets are then visited in
    a fixed round-robin, fullest first, so a repeat run with the same seed
    and inputs always returns the same subset. Pure -- takes already-built
    scenarios, returns a subset; no board or config needed, so this is
    unit-testable on hand-built dicts alone.
    """
    if count >= len(candidates):
        return list(candidates)

    rng = random.Random(seed)
    shuffled = list(candidates)
    rng.shuffle(shuffled)

    buckets: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for c in shuffled:
        buckets[(c["round_bucket"], c["top_rec_position"])].append(c)

    chosen: list[dict] = []
    # Cap how many scenarios any one source draft contributes, proportional
    # to how many drafts fed the pool -- keeps one unusually cooperative mock
    # from dominating the sample. At least 3 so a small `--drafts` count
    # (e.g. 1-2) doesn't starve the picker into returning fewer than asked.
    n_drafts = len({c["source_draft"] for c in candidates})
    per_draft_cap = max(3, -(-count // max(1, n_drafts)) + 1)
    draft_counts: dict[str, int] = defaultdict(int)

    while len(chosen) < count and any(buckets.values()):
        # Fullest bucket first, so a thin bucket (e.g. very few round-10+
        # candidates) doesn't get starved by round-robin order alone once
        # richer buckets have already given up their fair share.
        order = sorted(
            (k for k, v in buckets.items() if v),
            key=lambda k: -len(buckets[k]),
        )
        made_progress = False
        for key in order:
            if len(chosen) >= count:
                break
            bucket = buckets[key]
            # Skip (don't discard) a candidate over its draft's cap -- it
            # may still be pulled once other buckets/drafts are exhausted.
            idx = next(
                (i for i, c in enumerate(bucket) if draft_counts[c["source_draft"]] < per_draft_cap),
                None,
            )
            if idx is None:
                continue
            picked = bucket.pop(idx)
            chosen.append(picked)
            draft_counts[picked["source_draft"]] += 1
            made_progress = True
        if not made_progress:
            # Every remaining candidate is capped out -- relax the cap
            # rather than return short of `count`.
            per_draft_cap += 1

    return chosen


def build_pack(
    scenarios: Sequence[dict],
    cfg,
    board,
    *,
    pack_id: str,
    label: str = "",
    blind: bool = False,
    generator: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Wrap built scenarios with the draft-level facts a reviewer (and later,
    the report) needs: league shape, the tuning dials in force, and where the
    board's points came from -- the same provenance
    `draft_report.build_report` stamps on a real draft, for the same reason:
    a pack built on live Sleeper numbers and one built on a frozen CSV
    should never look alike.

    `player_board` is the full draftable universe, once, sorted by projected
    points -- the reviewer page's Player Board panel, which shows every
    player alongside the much shorter recommendation table so a reviewer can
    place the engine's picks in context or rank someone off the table
    entirely. It rides on the PACK rather than per scenario: `board` is the
    same object for every scenario in one generation run (see
    `scripts/make_training_pack.py`, which loads it once and reuses it across
    every simulated draft) -- only who's already taken differs between
    scenarios, and that's `scenario["taken_keys"]`. Team is resolved with
    `webapi._bp_team`, the same defense-key handling the live draft room's
    roster/opponents panels use, so a DEF row never reads "-" here while
    showing its real abbreviation everywhere else.
    """
    return {
        "schema": SCHEMA,
        "pack_id": pack_id,
        "label": label,
        "generated_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "blind": blind,
        "num_teams": cfg.draft.num_teams,
        "rounds": cfg.draft.rounds,
        "order": cfg.draft.order,
        "roster_positions": dict(cfg.roster_positions),
        "config": {
            field: getattr(cfg.draft, field)
            for field in _TUNING_FIELDS
            if hasattr(cfg.draft, field)
        },
        "board": {
            "players": len(board.players),
            "points_source": cfg.draft.board_points_source,
            "league_scored": cfg.league is not None,
            "replacement": dict(board.replacement),
            "bench_replacement": dict(board.bench_replacement),
            "starters_per_pos": dict(board.starters_per_pos),
        },
        "player_board": [
            {
                "key": bp.key,
                "name": bp.name,
                "position": bp.position,
                "team": _bp_team(bp),
                "bye_week": bp.bye_week,
                "proj": bp.points,
                "adp": bp.adp,
            }
            for bp in sorted(board.players, key=lambda b: -b.points)
        ],
        "generator": dict(generator or {}),
        "scenarios": list(scenarios),
    }


def grade_response(scenario: dict, answer: dict) -> dict:
    """Grade one reviewer answer against the frozen table `scenario` showed.

    `answer` is `{"choices": [key1, key2, key3], "verdict": "...",
    "conviction": "...", "roster_health": "...", "note": "..."}` --
    `choices` may hold 0-3 keys (an unranked or partially-ranked answer is
    still gradable per choice given). Each choice is graded with
    `draft_report.taken_block` against `scenario["state"]["recommendations"]`
    exactly as a real pick would be, via a synthetic `BoardPlayer`-shaped
    stand-in built from that same table -- `taken_block` only reads `.name`/
    `.position`/`.points` off it, so a lightweight namespace is enough and
    no `Board`/config is needed here at all.

    `conviction` ("toss" | "lean" | "strong" | None) is how strongly the
    reviewer holds their OWN pick, which is a different question from
    `verdict` (what they think of the engine's). It exists to be read
    against the engine's own `confidence.effective_options`: the pairing
    worth tuning on is "reviewer certain, engine flat", and inferring that
    from the wording of a free-text note is not a measurement.

    `roster_health` ("good" | "ok" | "bad" | None) rates the partial roster
    the situation was built on, NOT the pick in front of them. A synthetic
    roster is bot-drafted (see `scripts/make_training_pack.py`'s
    `--my-spice`), so a reviewer objecting to "only two RB by round 7" may
    be objecting to picks 1-6 rather than to this recommendation. Without
    this field the two are indistinguishable in the notes; with it they
    separate cleanly, and it doubles as a human read on how well a given
    bot spice level actually drafts.

    Returns the scenario's identity, the raw answer, and one graded block
    per choice under `"graded"` (in the order given, so `graded[0]` is
    always the reviewer's own #1).
    """
    table = scenario["state"]["recommendations"]
    row_by_key = {row["key"]: row for row in table}

    class _Stub:
        __slots__ = ("name", "position", "points")

        def __init__(self, row: dict) -> None:
            self.name = row["name"]
            self.position = row["position"]
            self.points = row["proj"]

    graded = []
    for key in answer.get("choices") or []:
        row = row_by_key.get(key)
        stub = _Stub(row) if row is not None else None
        graded.append(taken_block(table, key, stub))

    return {
        "scenario_id": scenario["id"],
        "round": scenario["round"],
        "pick": scenario["pick"],
        "round_bucket": scenario["round_bucket"],
        "top_rec_position": scenario["top_rec_position"],
        "confidence": scenario["state"].get("confidence") or {},
        "choices": list(answer.get("choices") or []),
        "verdict": answer.get("verdict"),
        "conviction": answer.get("conviction"),
        "roster_health": answer.get("roster_health"),
        "note": answer.get("note", ""),
        "graded": graded,
    }


def merge_responses(pack: dict, responses: dict) -> tuple[list[dict], list[str]]:
    """Join a returned responses file to the `pack` that produced it.

    `responses` is `{"pack_id": ..., "reviewer": ..., "answers":
    {scenario_id: {"choices": [...], "verdict": ..., "note": ...}}}` -- the
    shape `web/train.html`'s Download button writes.

    Returns `(graded_records, warnings)`. A `scenario_id` present in
    `responses` but not in `pack` (a mismatched pack/responses pair, or a
    hand-edited file) is skipped and reported as a warning rather than
    raising -- the rest of a partially-broken responses file is still worth
    grading. A scenario with no answer at all is simply absent from the
    result; the caller's answered/total accounting is against `len(pack
    ["scenarios"])`, not `len(graded_records)`.
    """
    by_id = {s["id"]: s for s in pack["scenarios"]}
    answers = responses.get("answers") or {}
    graded: list[dict] = []
    warnings: list[str] = []
    for scenario_id, answer in answers.items():
        scenario = by_id.get(scenario_id)
        if scenario is None:
            warnings.append(f"response for unknown scenario_id {scenario_id!r} -- skipped")
            continue
        record = grade_response(scenario, answer)
        record["reviewer"] = responses.get("reviewer")
        graded.append(record)
    return graded, warnings


def read_pack(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_pack(pack: dict, path: str | Path) -> Path | None:
    """Write `pack` as JSON, creating the directory. The only I/O in this
    module. Never raises out to the caller -- the same contract
    `week_log.write_week_log` and `draft_report.write_report` use: returns
    the path on success, `None` on failure (a locked file, a full disk)."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(pack, indent=2, default=str), encoding="utf-8")
    except OSError:
        return None
    return p


def write_responses_template(pack: dict, path: str | Path) -> Path | None:
    """Write a blank `answers` skeleton for `pack`, one entry per scenario --
    a convenience for hand-editing responses outside the exported HTML page
    (e.g. reviewing over the phone and typing answers back later). Not
    required by the normal flow, which downloads a filled-in file straight
    from `web/train.html`."""
    template = {
        "pack_id": pack["pack_id"],
        "reviewer": "",
        "answers": {
            s["id"]: {
                "choices": [], "verdict": None,
                "conviction": None, "roster_health": None, "note": "",
            }
            for s in pack["scenarios"]
        },
    }
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(template, indent=2), encoding="utf-8")
    except OSError:
        return None
    return p
