"""A per-run JSON record of what the weekly manager recommended, and why.

The draft side has had this for a while (`ffbot/draft_report.py`) and the
reasoning carries over exactly: every "why did it bench him", "why was that
worth a claim" question was answered by reconstructing the run afterwards and
re-deriving what the recommendation panel must have said. That works, and it
is slow, and it depends on the inputs still being rebuildable weeks later --
which for the weekly path they are NOT. Live projections move, the wire
turns over, injury designations resolve. A week's view is genuinely
unrecoverable once it has passed.

So every run writes one of these. It holds the full recommendation set
exactly as it was shown -- start/sit, adds, claims, IR stash -- with the
complete metric block behind each row (`ffbot/gameplan.py`'s `PlayerMetrics`
/ `DecisionMetrics`), the lineup both before and after, which live source
every number came from, and the config dials in force.

Serialized through `ffbot.webapi`'s own serializers, not a second copy, for
the reason `draft_report` gives about `rec_row`: a hand-maintained duplicate
of those keys would drift, and then every stored log would describe a plan
that was never on screen.

Pure: `build_week_log` does no I/O and no fetching. `write_week_log` is the
only filesystem call, kept separate so the builder stays testable -- the same
split `ffbot/draft_report.py`, `ffbot/roster_editor.py`, and
`ffbot/reports_index.py` already use. Neither ever raises into a caller's
path: a report that already succeeded must not fail because a log file
couldn't be written.

Note on where this is CALLED from, which is not arbitrary. `webapi` imports
`report`, so a log module that reuses `webapi`'s serializers can never be
called from `report.load_everything` the way `markets/kalshi_log.py` is --
that would close a `report -> week_log -> webapi -> report` import cycle. The
call sites are the two entry points that own a completed run instead
(`scripts/week_report.py` and `scripts/gui.py`), matching how
`draft_report.DraftReporter` is constructed independently in both of its own.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from .webapi import adddrop_json, player_metrics_json, swap_line_json

if TYPE_CHECKING:  # pragma: no cover -- typing only, keeps this module import-light
    from .gameplan import GamePlan, MetricsIndex
    from .report import LoadedReport

WEEK_LOG_DIR = Path("weekly/reports")

# Season dials worth stamping into every log. Deliberately an explicit
# allowlist rather than "every SeasonConfig field", the same call
# `draft_report._TUNING_FIELDS` makes and for the same reason: the question
# this answers is "what was the engine configured to do when it said this",
# and a reader drowning in every knob is a reader who stops checking. Keep it
# in step with whatever the current tuning conversation is actually about.
_TUNING_FIELDS: tuple[str, ...] = (
    "spice_level",
    "ros_blend",
    "waiver_value_mode",
    "recommend_count",
    "denial_weight",
    "denial_row_limit",
    "opponent_correlation_weight",
    "volatility_weight",
    "upside_lean_weight",
    "usage_weight",
    "momentum_weight",
    "divergence_weight",
    "kalshi_weight",
    "blocking_hold_bonus",
    "stream_positions",
    "weather_weight",
    "vegas_weight",
)


def _lineup_json(plan, metrics: "MetricsIndex | None", week: int) -> dict:
    """One optimized lineup: its slots, its bench, and its weekly total.

    The per-player metric block rides along so a stored log can answer
    questions about players who were never NAMED in a recommendation -- "what
    did it think of the guy I left on the bench" is exactly the review
    question a recommendations-only record cannot answer.
    """
    return {
        "total": sum(p.projected_points or 0.0 for _, p in plan.assignments),
        "is_noop": plan.is_noop(),
        "unfilled_slots": list(plan.unfilled_slots),
        "starters": [
            {
                "slot": slot,
                "metrics": player_metrics_json(
                    metrics.for_player(p, week=week) if metrics is not None else None
                ),
            }
            for slot, p in sorted(plan.assignments, key=lambda t: t[0])
        ],
        "bench": [
            {
                "metrics": player_metrics_json(
                    metrics.for_player(p, week=week) if metrics is not None else None
                ),
            }
            for p in plan.bench
        ],
    }


def build_week_log(
    loaded: "LoadedReport",
    plan: "GamePlan",
    *,
    season: int,
    source: str,
    alerts: Sequence[str] = (),
    week_source: str = "",
    refreshed: bool = False,
    waiver_priority: int | None = None,
    now: Optional[datetime] = None,
) -> dict:
    """The whole run as one JSON-safe dict. Pure -- no I/O, no fetching.

    `source` names the surface that produced it ("gui", "cli", or an
    autorun trigger id) and lands in the filename, so a pre-kickoff check and
    a Tuesday waiver check are different files rather than one overwriting
    the other.
    """
    cfg = loaded.cfg
    metrics = plan.metrics
    return {
        "schema": 1,
        "generated_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "season": season,
        "week": plan.week,
        "source": source,
        "week_source": week_source,
        "refreshed": refreshed,
        # Which live seam produced each number. A log that records a
        # recommendation without recording whether its projections were live
        # or frozen is a log you cannot argue with later.
        "sources": {
            "projection": loaded.projection_source,
            "roster": loaded.roster_source,
            "slots": loaded.slots_source,
            "league_rosters": loaded.league_rosters_source,
            "season_ptd": loaded.season_ptd_source,
            "pool": "ros_board" if loaded.ros_board is not None else "board",
            "board_players": len(loaded.board.players) if loaded.board is not None else 0,
            "league_scored": cfg.league is not None,
        },
        "config": {
            f: getattr(cfg.season, f) for f in _TUNING_FIELDS if hasattr(cfg.season, f)
        },
        "alerts": list(alerts),
        "run": {
            "decision_scale": plan.decision_scale,
            "open_spots": plan.open_spots,
            "roster_capacity": plan.roster_capacity,
            "current_total": plan.current_total,
            "base_total": plan.base_total,
            "waiver_priority": waiver_priority,
            "opponent": plan.opponent,
        },
        # Both lineups, not just the recommended one: the whole point of the
        # start/sit rows is the DIFFERENCE between these two, and a reader
        # checking the engine's call needs the baseline it moved from.
        "lineup_current": _lineup_json(plan.current_plan, metrics, plan.week),
        "lineup_recommended": _lineup_json(plan.base_plan, metrics, plan.week),
        "start_sit": [swap_line_json(l) for l in plan.start_sit],
        "adds": [adddrop_json(r) for r in plan.adds],
        "claims": [adddrop_json(r) for r in plan.claims],
        "ir_stash": [
            {"add_name": c.add_name, "position": c.position, "value": c.value, "reason": c.reason}
            for c in plan.ir_stash
        ],
        "unfilled_slots": list(plan.unfilled_slots),
        "missing": list(plan.missing),
        "notes": list(plan.notes),
    }


def week_log_path(
    season: int, week: int, source: str, log_dir: Path | str = WEEK_LOG_DIR,
) -> Path:
    """`weekly/reports/2026-w03-cli.json`.

    Stable per `(season, week, source)` and therefore OVERWRITTEN on a repeat
    run of the same surface. That is deliberate: `web/weekly.html` soft-syncs
    every five minutes and on every tab focus, so a timestamped name would
    turn one open browser tab into hundreds of files a day. Autorun passes
    its own trigger id here instead, and those are already unique per kickoff
    slot and per waiver deadline -- so the snapshots actually worth keeping
    separate stay separate, and idle polling does not accumulate.

    `source` is sanitized for the filesystem: an autorun pre-kickoff trigger
    id embeds an ISO timestamp, and its colons are not legal in a Windows
    filename (`scripts/autorun.py` already learned this for its own reports).
    """
    safe = "".join("-" if ch in '<>:"/\\|?*' else ch for ch in source).strip() or "run"
    return Path(log_dir) / f"{season}-w{week:02d}-{safe}.json"


def write_week_log(log: dict, path: Path | str) -> Optional[Path]:
    """Write `log` as JSON, creating the directory. The only I/O here.

    Never raises out to the caller: a weekly report that already rendered
    must not fail because a log file couldn't be written (a locked file, a
    full disk, a read-only directory). Returns the path on success and
    `None` on failure, the same contract `markets/kalshi_log.py` uses.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    except OSError:
        return None
    return p
