"""A per-draft JSON record of what the engine recommended and what I took.

Every tuning question this repo has hit — "why did it wait until round 12
for a quarterback", "is it really taking seven receivers" — was answered by
reconstructing a draft after the fact from a pick log and re-deriving what
the recommendation table must have said. That works, but it is slow, it
depends on the board being rebuildable identically weeks later, and it
throws away the one thing that is genuinely hard to recover: what the table
looked like *at the moment of the decision*.

So every draft writes one of these, by default. Each entry is a pick of
mine, holding the complete recommendation table as it stood (via
`webapi.rec_row`, the same serializer the GUI renders from — not a second
copy that could drift), the roster and alerts at that moment, and which
player was actually taken. The most useful field for tuning is
`taken.rank_in_table` together with `taken.value_gap_to_top`: those are
where the engine and a human disagreed, and by how much, which is exactly
the sample any weight change should be argued from.

Ownership is recorded, never inferred, when Sleeper can tell us: `DraftSync`
resolves `mine` from Sleeper's own `roster_id` when `sleeper.roster_id` is
configured, and falls back to the snake-order arithmetic otherwise. The
report stamps which of those produced it (`ownership_source`) so a report
can never silently misattribute picks to the wrong team — a report built on
a guess and one built on ground truth should never look alike.

Pure: nothing here fetches, and `build_report`/`capture_pick` do no I/O.
`write_report` is the only filesystem call, kept separate so the builders
stay testable, the same split `ffbot/roster_editor.py` and
`ffbot/reports_index.py` already use.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .board import Board
from .config import Config
from .draft import (
    DraftState,
    Recommendation,
    alerts,
    needs_between,
    picks_until,
    round_and_slot,
)
from .webapi import rec_rows

REPORTS_DIR = Path("draft/reports")

# Tuning dials worth stamping into every report. Deliberately explicit
# rather than "every DraftConfig field": the point is to answer "what was
# the engine configured to do when it made these picks", and a reader
# drowning in fuzzy_threshold/sync_poll_seconds is a reader who stops
# checking. Keep this list in step with whatever the current tuning
# conversation is actually about.
_TUNING_FIELDS: tuple[str, ...] = (
    "spice_level",
    "scarcity_weight",
    "depth_weight",
    "depth_decay",
    "replacement_depth",
    "bench_replacement_depth",
    "rank_calibration",
    "rank_calibration_blend",
    "balance_weight",
    "block_weight",
    "bye_collision_weight",
    "upside_weight",
    "risk_weight",
    "volatility_weight",
    "stack_bonus",
    "scoring_arbitrage_weight",
    "kalshi_weight",
    "pick_confidence_scale",
    "position_caps",
    "position_targets",
    "num_teams",
    "rounds",
    "order",
)


def _roster_rows(roster: Sequence) -> list[dict]:
    return [
        {
            "key": bp.key,
            "name": bp.name,
            "position": bp.position,
            "team": bp.team,
            "bye_week": bp.bye_week,
            "proj": bp.points,
            "vor": bp.vor,
            "adp": bp.adp,
            "tier": bp.tier,
        }
        for bp in roster
    ]


def capture_pick(
    state: DraftState,
    cfg: Config,
    recs: Sequence[Recommendation],
    taken_key: str | None,
) -> dict:
    """One entry for a pick of mine, from the state as it was BEFORE the
    pick was recorded.

    `recs` is what `recommend()` returned at that moment; `taken_key` is the
    player actually taken (None when the pick is being captured before the
    choice is known, e.g. a sync that told us the number but not the
    player).

    The `taken` block is the tuning payload: where the taken player sat in
    the engine's own ranking, and how much value the engine thought was
    given up versus its top row. `rank_in_table` is None when the taken
    player wasn't in the table at all — itself a strong signal, since it
    means the engine never even offered the pick a human made.
    """
    pick_no = state.current_pick()
    round_, slot = round_and_slot(pick_no, state.num_teams, state.order)
    roster = state.my_roster()
    table, confidence = rec_rows(recs, cfg)

    taken: dict | None = None
    if taken_key is not None:
        bp = state.board.by_key.get(taken_key)
        rank_in_table = next((row["rank"] for row in table if row["key"] == taken_key), None)
        taken = {
            "key": taken_key,
            "name": bp.name if bp else None,
            "position": bp.position if bp else None,
            "proj": bp.points if bp else None,
            "rank_in_table": rank_in_table,
            "was_top_recommendation": rank_in_table == 1,
            # How much of the engine's own probability mass the taken player
            # held. Pairs with `value_gap_to_top` below and is the sharper of
            # the two for tuning: a 20-point gap means something very
            # different at a pick with one standout than at a twenty-way
            # toss-up, and this is already normalized by that.
            "p_best_of_taken": next(
                (row["p_best"] for row in table if row["key"] == taken_key), None,
            ),
            # Positive means the engine valued its own top row above what
            # was taken -- the size of the disagreement, in season points.
            "value_gap_to_top": (
                table[0]["value"] - next(row["value"] for row in table if row["key"] == taken_key)
                if rank_in_table is not None and table
                else None
            ),
        }

    return {
        "pick": pick_no,
        "round": round_,
        "slot": slot,
        "picks_until_next": picks_until(pick_no, state.my_picks()),
        "next_my_pick_after": state.next_my_pick_after(pick_no),
        "roster_before": _roster_rows(roster),
        "position_counts_before": dict(Counter(bp.position for bp in roster)),
        "alerts": alerts(state, cfg),
        "needs_between": needs_between(state),
        "recommendations": table,
        "confidence": confidence,
        "taken": taken,
    }


def build_report(
    state: DraftState,
    cfg: Config,
    entries: Sequence[dict],
    draft_id: str | None = None,
    ownership_source: str = "snake_order_guess",
    my_roster_id: int | None = None,
    source: str = "live",
    board: Board | None = None,
) -> dict:
    """Wrap captured pick entries with the draft-level facts needed to read
    them later: who I was, how ownership was determined, what the engine was
    tuned to, and where its numbers came from.

    `ownership_source` must be `"sleeper_roster_id"` only when Sleeper's own
    `roster_id` actually resolved ownership — see this module's docstring.
    """
    board = board if board is not None else state.board
    roster = state.my_roster()
    return {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,  # "live" | "replay"
        "draft_id": draft_id,
        "my_slot": state.my_slot,
        "my_roster_id": my_roster_id,
        "ownership_source": ownership_source,
        "num_teams": state.num_teams,
        "rounds": state.rounds,
        "order": state.order,
        "roster_positions": dict(state.roster_positions),
        "my_picks": state.my_picks(),
        "picks_recorded": len(state.picks),
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
        "picks": list(entries),
        "final_roster": _roster_rows(roster),
        "final_position_counts": dict(Counter(bp.position for bp in roster)),
        "final_projected_points": sum(bp.points for bp in roster),
    }


def report_path(draft_id: str | None, reports_dir: Path | str = REPORTS_DIR) -> Path:
    """Stable per-draft filename, so re-writing after each pick overwrites
    the same file instead of littering one per pick."""
    stem = f"draft-{draft_id}" if draft_id else f"draft-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    return Path(reports_dir) / f"{stem}.json"


class DraftReporter:
    """Accumulates pick captures during a live draft and writes the report.

    The one stateful thing in this module. Both entry points (the web GUI
    and the terminal assistant) own one and call `capture` immediately
    before a pick of theirs is recorded — for synced picks that happens via
    `draft_sync.apply_synced_picks`' `on_my_pick` hook, so the gap-fill
    sequencing stays in the one place that owns it.

    Writes after every capture rather than at the end: a mock draft that
    gets abandoned at round 8 is still a perfectly good tuning sample, and
    the file is a few hundred KB.

    Every method swallows its own failures. A draft room must not break
    because a report could not be built, so a capture that raises is
    dropped and the draft continues.
    """

    def __init__(
        self,
        cfg: Config,
        draft_id: str | None = None,
        ownership_source: str = "snake_order_guess",
        my_roster_id: int | None = None,
        reports_dir: Path | str = REPORTS_DIR,
        limit: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.draft_id = draft_id
        self.ownership_source = ownership_source
        self.my_roster_id = my_roster_id
        self.path = report_path(draft_id, reports_dir)
        self.limit = limit if limit is not None else cfg.draft.gui_recommend_count
        self.entries: list[dict] = []
        self._captured_picks: set[int] = set()

    def capture(self, state: DraftState, taken_key: str) -> None:
        """Record the table as it stands, then persist. Call BEFORE
        `state.record(...)` — the whole value of this file is that it holds
        the pre-pick view, which cannot be recovered afterwards."""
        pick_no = state.current_pick()
        if pick_no in self._captured_picks:
            return  # a re-applied sync item; never double-count a pick
        try:
            from .draft import recommend

            recs = recommend(state, self.cfg, limit=self.limit)
            self.entries.append(capture_pick(state, self.cfg, recs, taken_key))
            self._captured_picks.add(pick_no)
            self.flush(state)
        except Exception:
            pass

    def flush(self, state: DraftState) -> None:
        try:
            report = build_report(
                state, self.cfg, self.entries,
                draft_id=self.draft_id,
                ownership_source=self.ownership_source,
                my_roster_id=self.my_roster_id,
                source="live",
            )
            write_report(report, self.path)
        except Exception:
            pass


def write_report(report: dict, path: Path | str) -> Path:
    """Write `report` as JSON, creating the directory. The only I/O here.

    Never raises out to the caller's hot path: a draft room must not die
    because a report file couldn't be written (a locked file, a full disk).
    Returns the path on success; on failure returns it anyway, having done
    nothing — the draft is what matters, the report is a convenience.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass
    return p
