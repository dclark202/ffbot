"""The unified weekly recommendations engine behind the web GUI's
Recommendations panel (and, via the same call, `scripts/week_report.py`'s
CLI sections) -- one coherent plan instead of four independently-rendered
categories (start/sit moves, waiver claims, streamers, denial holds).

Three things this module fixes by construction, not by convention:

- **One valuation pool for everything.** `webapi.py`/`week_report.py` used
  to value denial off the raw `board` while waivers/streamers valued off
  `loaded.ros_board or board` -- a real, silent inconsistency when a live
  ROS provider is configured. `build_gameplan` resolves the pool exactly
  once and every candidate scan reads it.
- **Streaming and denial are REASONS on an ordinary add/drop row, not
  separate categories.** A K/DEF need is detected (bye/OUT/outscored
  incumbent) and produces a normal, same-shape row with `forced_need` set;
  a denial-motivated add is a normal row whose `reasons` mention
  "blocks <team> (+N to their lineup)". Nothing renders a flat N-per-
  position dump regardless of whether a pickup is actually worth it.
- **The lineup shown is the one you'd field AFTER the recommended adds.**
  Recommended free-agent adds are treated as certain and baked into
  `base_plan`; each waiver CLAIM (not guaranteed to clear) instead carries
  its own conditional `ClaimConsequence`, computed by re-optimizing the
  post-add roster with that one claim substituted in.

Pure composition over existing machinery -- no network, no I/O, importable
offline like every other module in this package. `lineup.optimize()` is
never modified; swap pairing (`pair_moves`) is a presentation-layer read of
its existing `LineupPlan.moves` diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Sequence

from . import denial, policy
from . import week as weekmod
from .board import Board, BoardPlayer, to_player
from .config import Config
from .league_rosters import LeagueRosters
from .lineup import LineupPlan, Move, optimize
from .models import BENCH, IR_SLOTS, Player, SLOT_ELIGIBILITY, starting_slots
from .names import normalize_name

if TYPE_CHECKING:
    from .report import LoadedReport

# Sleeper's own standalone flex spellings, preferred over a Yahoo-style
# "/"-joined layout slot when displaying a slot name to a human -- these are
# the names actually shown in the Sleeper app itself.
_PREFERRED_FLEX_DISPLAY: dict[frozenset, str] = {
    frozenset({"WR", "RB", "TE"}): "FLEX",
    frozenset({"QB", "WR", "RB", "TE"}): "SUPER_FLEX",
    frozenset({"WR", "RB"}): "WRRB_FLEX",
    frozenset({"WR", "TE"}): "REC_FLEX",
}

# A synthetic Player id range that can never collide with a real roster id
# (Sleeper ids are positive) or `waiver_candidates`' own single synthetic
# `-1` -- each hypothetical addition in this module gets its own id from
# this descending counter.
_SYNTHETIC_ID_START = -100


def display_slot(slot: str) -> str:
    """The friendly, Sleeper-app-matching name for a layout slot ("W/R/T"
    -> "FLEX"); anything with no flex equivalent (QB, RB, WR, TE, K, DEF,
    BENCH, an IR slot, or a genuinely unknown name) passes through
    unchanged."""
    wanted = SLOT_ELIGIBILITY.get(slot)
    if wanted is None:
        return slot
    return _PREFERRED_FLEX_DISPLAY.get(wanted, slot)


def _fmt_player(name: str, team: str) -> str:
    return f"{name} ({team})" if team else name


def _primary_position(player: Player) -> str:
    return player.eligible_positions[0] if player.eligible_positions else ""


# --- Metrics: the numbers behind a recommendation --------------------------
#
# Every recommendation here used to survive only as a sentence. The engine
# computed a rich set of floats per row, formatted one or two of them into
# `reason`/`text`, and dropped the rest -- which made a plan impossible to
# review after the fact, since "why was he benched" could then only be
# re-derived by rerunning the whole pipeline against live data that had
# since moved on.
#
# These two dataclasses carry those numbers out instead. Nearly every field
# was ALREADY a live local at the row's construction site: `WaiverCandidate`'s
# typed `net` components (which `ffbot/week.py` added for exactly this
# purpose and which nothing ever read), the `BoardPlayer` already looked up
# in `bp_by_name`, the drop cost already computed to price the row. This is
# bookkeeping, not new computation.
#
# Everything is defaulted, so no existing constructor call breaks -- the same
# additive discipline `WaiverCandidate`'s own typed fields used.


@dataclass
class PlayerMetrics:
    """Every number the engine already knew about one player at the moment a
    recommendation named him.

    The three point horizons are deliberately kept separate rather than
    collapsed into one "points" field: `week_proj` is this week AFTER
    `week.adjusted_players`' weather/Vegas/status multipliers, `ros_proj` is
    the rest-of-season total off `LoadedReport.ros_board`, and `season_proj`
    is the full-season board number. They are on different scales on purpose
    (see `week.waiver_candidates`' SCALE note), and a reader comparing a
    recommendation against a roster needs to know which one they are looking
    at.

    `season_ptd`/`games_played` are ACTUAL points scored so far, and are
    DESCRIPTIVE ONLY -- nothing in this module, `ffbot/week.py`, or
    `ffbot/lineup.py` ever reads them back. They exist so a human reviewing a
    stored plan can see how a player had really been performing, without that
    fact quietly entering a valuation no backtest has graded.
    """

    name: str = ""
    position: str = ""
    team: str = ""
    board_key: str = ""

    # Availability
    status: str = ""
    bye_week: int | None = None
    on_bye: bool = False

    # Points, three horizons plus realized
    week_proj: float | None = None
    ros_proj: float | None = None
    season_proj: float | None = None
    season_ptd: float | None = None
    games_played: int | None = None

    # Which board `ros_proj` actually came from: "ros_board" when a live
    # rest-of-season pool was configured, else "board". This is NOT
    # decoration -- `build_gameplan` prices everything off
    # `ros_board or board`, so the same field is a genuine ROS total on a
    # live run and a full-season total offline. Labelling it "rest of
    # season" unconditionally would silently mislead on every offline run,
    # so the label travels with the number.
    pool_source: str = "board"

    # Board valuation
    vor: float | None = None
    tier: int | None = None
    board_rank: int | None = None
    adp: float | None = None
    adp_stdev: float | None = None
    adp_spread: float | None = None
    points_fp: float | None = None
    points_source: str = ""

    # Live market signals (roster_source: sleeper only)
    percent_owned: float | None = None
    started_pct: float | None = None

    # Researched intel (draft/intel.yml, weekly/week-NN.yml)
    upside: float | None = None
    availability_risk: float | None = None
    intel_note: str = ""
    intel_flags: tuple[str, ...] = ()


@dataclass
class DecisionMetrics:
    """The arithmetic behind one recommendation row -- every typed component
    of the number it was ranked on.

    `net` is the ranking key and the components below are what produced it,
    so a reader can see WHICH half of a call carried it: a claim that is all
    `week_gain` is a one-week rental, one that is all `ros_gain` is a real
    roster upgrade, and the two deserve different decisions on waiver
    priority. Before this existed those two cases rendered identically.

    `decision_scale` is the per-run unit the spice/edge terms are expressed
    in (`week.decision_scale`), included so a `stack_delta` of -2.1 can be
    read as large or small rather than just as a number.
    """

    net: float = 0.0
    value: float = 0.0
    ros_gain: float = 0.0
    week_gain: float = 0.0
    drop_cost: float = 0.0
    claim_cost: float = 0.0
    urgency: float = 0.0
    stack_delta: float = 0.0
    handoff_value: float = 0.0
    handoff_team: str = ""
    hold_margin: float | None = None
    denial_gain: float = 0.0
    denial_team: str = ""
    decision_scale: float = 0.0
    week_delta: float | None = None


class MetricsIndex:
    """Bound lookups for building a `PlayerMetrics`, so every construction
    site downstream is a one-liner instead of six dictionary probes.

    Deliberately holds BOTH boards. `pool` (`ros_board or board`) is the one
    valuation pool everything in this module prices against, but `board` is
    still the only true full-season total -- `LoadedReport.ros_board`'s own
    note explains why the two must not be conflated. A reader of a stored
    plan wants both: "worth 140 the rest of the way, on a 210-point season
    pace" says something neither number says alone.

    Every lookup degrades to `None` rather than raising. A name that isn't
    on the board at all (the `missing` list `roster_board_keys` returns) is
    an ordinary, expected state, not an error.
    """

    def __init__(
        self,
        board: Board | None = None,
        pool: Board | None = None,
        season_ptd: dict[str, float] | None = None,
        season_ptd_games: dict[str, int] | None = None,
        weekly_points: dict[str, float] | None = None,
    ) -> None:
        self._season_ptd = season_ptd or {}
        self._season_ptd_games = season_ptd_games or {}
        self._weekly_points = weekly_points or {}
        self._pool_by_name = {normalize_name(bp.name): bp for bp in pool.players} if pool else {}
        self._board_by_name = {normalize_name(bp.name): bp for bp in board.players} if board else {}
        # `pool is board` exactly when no live ROS board was configured --
        # see PlayerMetrics.pool_source for why the distinction has to
        # travel with the number rather than be assumed by the reader.
        self._pool_source = "ros_board" if (pool is not None and pool is not board) else "board"

    def _keyed(self, table: dict, name: str, position: str):
        """`name:POS` first (the key convention `weekly_points` and
        `season_to_date_rows` both use), then a bare-name fallback so a
        position mismatch between sources degrades to a hit rather than a
        silent blank."""
        norm = normalize_name(name)
        if position:
            hit = table.get(f"{norm}:{position.upper()}")
            if hit is not None:
                return hit
        return table.get(norm)

    def for_name(
        self,
        name: str,
        position: str = "",
        team: str = "",
        *,
        week_proj: float | None = None,
        on_bye: bool = False,
        player: Player | None = None,
    ) -> PlayerMetrics:
        bp = self._pool_by_name.get(normalize_name(name))
        season_bp = self._board_by_name.get(normalize_name(name))
        position = position or (bp.position if bp else "")
        if week_proj is None:
            week_proj = self._keyed(self._weekly_points, name, position)
        return PlayerMetrics(
            name=name,
            position=position,
            team=team or (bp.team if bp else ""),
            board_key=bp.key if bp else "",
            status=player.status if player is not None else "",
            bye_week=bp.bye_week if bp else (player.bye_week if player is not None else None),
            on_bye=on_bye,
            week_proj=week_proj,
            ros_proj=bp.points if bp else None,
            season_proj=season_bp.points if season_bp else None,
            pool_source=self._pool_source,
            season_ptd=self._keyed(self._season_ptd, name, position),
            games_played=self._keyed(self._season_ptd_games, name, position),
            vor=bp.vor if bp else None,
            tier=bp.tier if bp else None,
            board_rank=bp.rank if bp else None,
            adp=bp.adp if bp else None,
            adp_stdev=bp.adp_stdev if bp else None,
            adp_spread=bp.adp_spread if bp else None,
            points_fp=season_bp.points_fp if season_bp else None,
            points_source=season_bp.points_source if season_bp else "",
            percent_owned=player.percent_owned if player is not None else None,
            started_pct=player.started_pct if player is not None else None,
            upside=bp.upside if bp else None,
            availability_risk=bp.availability_risk if bp else None,
            intel_note=bp.intel_note if bp else "",
            intel_flags=bp.intel_flags if bp else (),
        )

    def for_player(self, p: Player, *, week: int | None = None) -> PlayerMetrics:
        """A rostered player. `week_proj` comes off the `Player` itself --
        by the time this module sees a roster it has already been through
        `week.adjusted_players`, so `projected_points` IS the adjusted
        this-week number, not the raw projection."""
        return self.for_name(
            p.name,
            _primary_position(p),
            p.team,
            week_proj=p.projected_points,
            on_bye=week is not None and p.bye_week == week,
            player=p,
        )


# --- Start/sit: pairing the optimizer's raw move diff into human lines -----


@dataclass
class SwapLine:
    kind: str  # "swap" | "slot_shift" | "add_start" | "start_only" | "bench_only"
    slot: str = ""
    slot_display: str = ""
    from_slot: str = ""  # slot_shift only
    from_slot_display: str = ""
    start_name: str = ""
    start_team: str = ""
    start_pos: str = ""
    start_proj: float | None = None
    bench_name: str = ""  # who left the slot -- a benched player, OR a dropped one
    bench_team: str = ""
    bench_proj: float | None = None
    bench_is_drop: bool = False  # bench_name is who was DROPPED, not benched
    reason: str = ""
    # Why this starter is correlated with your head-to-head opponent's own
    # lineup, and what that correlation cost (or, for leverage, paid) in
    # points -- `week.adjusted_players` really does apply that nudge and then
    # discards the explanation, so without this the penalty moves start/sit
    # calls invisibly. Empty whenever opponent correlation is off, no
    # opponent starters loaded, or this player is uncorrelated.
    #
    # NOTE: this used to be documented as "non-empty only when the penalty
    # FLIPPED this call". Nothing ever assigned it, so that promise was
    # never kept by any code. Proving a flip needs a second optimize() of
    # the un-nudged roster; until something needs that, the honest reading
    # is the one above -- correlation present, not causation proven.
    opp_stack_note: str = ""
    text: str = ""

    # See PlayerMetrics/DecisionMetrics above. `None` whenever `pair_moves`
    # was called without a metrics index (the board-less early return in
    # `build_gameplan`, and every direct caller in the tests).
    start_metrics: PlayerMetrics | None = None
    bench_metrics: PlayerMetrics | None = None
    decision: DecisionMetrics | None = None


def _swap_text(line: SwapLine) -> str:
    if line.kind in ("swap", "add_start", "start_only"):
        verb = "Add & start" if line.kind == "add_start" else "Start"
        starter = f"{line.slot_display}: {verb} {_fmt_player(line.start_name, line.start_team)}"
        if line.bench_name and line.bench_is_drop:
            # The vacated slot's occupant was DROPPED (see `dropped_by_slot`
            # in `pair_moves`) -- "reason" here is just "replaces dropped X",
            # already fully said by "— Drop X"; no separate parenthetical.
            base = f"{starter} — Drop {_fmt_player(line.bench_name, line.bench_team)}"
        elif line.bench_name:
            # A genuine benching paired to this start -- a plain "he simply
            # scored more" swap needs no explanation; a bye/status-forced
            # one does. `reason` is the BENCH move's own reason.
            base = f"{starter} — Bench {_fmt_player(line.bench_name, line.bench_team)}"
            if line.reason and not line.reason.startswith("outscored") and line.reason != "not startable":
                base += f" ({line.reason})"
        elif line.reason:
            # No paired departure at all -- an open spot, or a slot vacated
            # by a lateral shift (see the `slot_shift` line rendered
            # separately for that departure) -- `reason` explains why.
            base = f"{starter} ({line.reason})"
        else:
            base = starter
    elif line.kind == "slot_shift":
        base = f"{line.from_slot_display} → {line.slot_display}: {_fmt_player(line.start_name, line.start_team)}"
    elif line.kind == "bench_only":
        base = (
            f"{line.slot_display}: Bench {_fmt_player(line.bench_name, line.bench_team)} "
            f"({line.reason}) — slot unfilled"
        )
    else:  # pragma: no cover -- exhaustive over the kinds this module produces
        base = ""
    if line.opp_stack_note:
        base += f" — opp-stack: {line.opp_stack_note}"
    return base


def pair_moves(
    plan: LineupPlan,
    roster_positions: dict[str, int],
    *,
    added_ids: frozenset = frozenset(),
    dropped: Sequence[Player] = (),
    metrics: MetricsIndex | None = None,
    week: int | None = None,
    decision_scale: float = 0.0,
    opp_index: dict | None = None,
    opp_weight: float = 0.0,
) -> list[SwapLine]:
    """Turn `LineupPlan.moves`' raw, unpaired diff into human-shaped lines.

    `lineup.optimize()` records every changed slot as an independent `Move`
    (a start into a slot, a benching out of one, or a lateral shift between
    two starting slots) -- correct for the optimizer's own purposes but not
    how a person reads a lineup change. This resolves each entrant's
    vacancy locally: who left the slot they're entering, and how.

    - An entrant paired with someone BENCHED directly out of that slot is
      one `"swap"` line.
    - An entrant filling a slot someone SHIFTED out of (into another
      starting slot) is a `"start_only"` line naming the shift; the shift
      itself always renders as its own first-class `"slot_shift"` line
      (e.g. "WR -> FLEX: Jim Bojimbo (DAL)") -- multi-hop chains read as
      two clear lines rather than one nested one.
    - An entrant filling a slot nobody left (an open spot, or one a
      recommended DROP vacated -- see `dropped`) is `"start_only"`.
    - Any benching left unpaired (the roster came up short a body) is
      `"bench_only"`.

    `added_ids` (player ids from `build_gameplan`'s recommended free-agent
    adds) tags the matching entrant lines `"add_start"` instead of
    `"swap"`/`"start_only"`. `dropped` (the `Player`s removed before
    `plan` was optimized, at their PRE-drop `selected_position`) is what
    lets a start into a now-empty starting slot read as "replaces dropped
    X" instead of the less useful "slot was empty."

    Deterministic: entrants and shifts are ordered by `roster_positions`'
    own slot order, then player name; benchings the same way by their
    vacated slot. `text` is rendered once here -- every caller (GUI, CLI)
    prints it verbatim.

    `metrics` (a `MetricsIndex`) attaches the full per-player numbers to both
    sides of each line and the this-week delta between them; `opp_index`/
    `opp_weight`/`decision_scale` fill `opp_stack_note` on the START side,
    the same opponent-correlation read `build_gameplan` already applies to
    add/claim rows. All four default to inert, so a caller that passes none
    of them gets exactly the lines this function produced before they
    existed.
    """
    starts: list[Move] = []
    benches: list[Move] = []
    shifts: list[Move] = []
    for m in plan.moves:
        if m.to_slot == BENCH:
            benches.append(m)
        elif m.from_slot == BENCH:
            starts.append(m)
        else:
            shifts.append(m)

    bench_by_from_slot: dict[str, Move] = {}
    for m in benches:
        bench_by_from_slot.setdefault(m.from_slot, m)
    shift_by_from_slot: dict[str, Move] = {m.from_slot: m for m in shifts}
    dropped_by_slot: dict[str, Player] = {
        p.selected_position: p for p in dropped
        if p.selected_position != BENCH and p.selected_position not in IR_SLOTS
    }

    layout_order = starting_slots(roster_positions)
    slot_rank: dict[str, int] = {}
    for i, s in enumerate(layout_order):
        slot_rank.setdefault(s, i)

    consumed_bench_ids: set = set()
    lines: list[SwapLine] = []

    def _finish(line: SwapLine, start: Player | None, bench: Player | None) -> SwapLine:
        """Attach metrics and the opponent-stack note, THEN render `text` --
        `_swap_text` folds `opp_stack_note` into the sentence, so the order
        matters."""
        if metrics is not None:
            if start is not None:
                line.start_metrics = metrics.for_player(start, week=week)
            if bench is not None:
                line.bench_metrics = metrics.for_player(bench, week=week)
            line.decision = DecisionMetrics(
                decision_scale=decision_scale,
                week_delta=(
                    (line.start_proj or 0.0) - (line.bench_proj or 0.0)
                    if line.start_proj is not None and line.bench_proj is not None
                    else None
                ),
            )
        if start is not None and opp_index and opp_weight != 0.0:
            _, note = _opponent_stack_note(
                _primary_position(start), start.team, opp_index, opp_weight, decision_scale,
            )
            line.opp_stack_note = note
        line.text = _swap_text(line)
        lines.append(line)
        return line

    for m in sorted(starts, key=lambda m: (slot_rank.get(m.to_slot, 999), m.player.name)):
        slot = m.to_slot
        is_add = m.player.player_id in added_ids
        vacating_bench = bench_by_from_slot.get(slot)
        if vacating_bench is not None and vacating_bench.player.player_id not in consumed_bench_ids:
            consumed_bench_ids.add(vacating_bench.player.player_id)
            _finish(SwapLine(
                kind="add_start" if is_add else "swap",
                slot=slot, slot_display=display_slot(slot),
                start_name=m.player.name, start_team=m.player.team, start_pos=_primary_position(m.player),
                start_proj=m.player.projected_points,
                bench_name=vacating_bench.player.name, bench_team=vacating_bench.player.team,
                bench_proj=vacating_bench.player.projected_points,
                reason=vacating_bench.reason,
            ), m.player, vacating_bench.player)
        else:
            vacating_shift = shift_by_from_slot.get(slot)
            dropped_player = dropped_by_slot.get(slot)
            if vacating_shift is not None:
                reason = f"slot opened by {vacating_shift.player.name}'s move to {display_slot(vacating_shift.to_slot)}"
            elif dropped_player is not None:
                reason = f"replaces dropped {dropped_player.name}"
            else:
                reason = "slot was empty"
            _finish(SwapLine(
                kind="add_start" if is_add else "start_only",
                slot=slot, slot_display=display_slot(slot),
                start_name=m.player.name, start_team=m.player.team, start_pos=_primary_position(m.player),
                start_proj=m.player.projected_points,
                bench_name=dropped_player.name if dropped_player else "",
                bench_team=dropped_player.team if dropped_player else "",
                bench_is_drop=dropped_player is not None,
                reason=reason,
            ), m.player, dropped_player)

    for m in sorted(shifts, key=lambda m: (slot_rank.get(m.to_slot, 999), m.player.name)):
        _finish(SwapLine(
            kind="slot_shift",
            slot=m.to_slot, slot_display=display_slot(m.to_slot),
            from_slot=m.from_slot, from_slot_display=display_slot(m.from_slot),
            start_name=m.player.name, start_team=m.player.team, start_pos=_primary_position(m.player),
            start_proj=m.player.projected_points, reason=m.reason,
        ), m.player, None)

    for m in sorted(benches, key=lambda m: (slot_rank.get(m.from_slot, 999), m.player.name)):
        if m.player.player_id in consumed_bench_ids:
            continue
        _finish(SwapLine(
            kind="bench_only",
            slot=m.from_slot, slot_display=display_slot(m.from_slot),
            bench_name=m.player.name, bench_team=m.player.team, bench_proj=m.player.projected_points,
            reason=m.reason,
        ), None, m.player)

    return lines


# --- Add/drop and waiver claims ---------------------------------------


@dataclass
class ClaimConsequence:
    starts: bool
    slot_display: str = ""
    over_name: str = ""
    week_delta: float = 0.0
    text: str = ""

    # The two lineup totals `week_delta` is the difference of. Carried so a
    # "+2.1 this week" reads against the size of the lineup it moves (2.1 on
    # a 96-point week is noise; on a 9-point kicker slot it is not).
    base_total: float = 0.0
    hyp_total: float = 0.0


@dataclass
class AddDropRec:
    kind: str  # "add" (executable now) | "claim" (needs priority to clear)
    position: str
    add_name: str
    add_team: str = ""
    drop_name: str | None = None
    drop_team: str = ""
    drop_reason: str = ""
    net: float = 0.0
    value: float = 0.0
    claim_note: str = ""
    reasons: tuple[str, ...] = ()
    forced_need: str = ""
    denial_team: str = ""
    denial_gain: float = 0.0
    on_bye: bool = False
    if_clears: ClaimConsequence | None = None
    text: str = ""

    # See PlayerMetrics/DecisionMetrics above. `drop_metrics` is None on a
    # row with no paired drop (an open roster spot).
    add_metrics: PlayerMetrics | None = None
    drop_metrics: PlayerMetrics | None = None
    decision: DecisionMetrics | None = None


def _adddrop_text(row: AddDropRec) -> str:
    if row.drop_name:
        base = (
            f"{row.position}: Add {_fmt_player(row.add_name, row.add_team)} — "
            f"Drop {_fmt_player(row.drop_name, row.drop_team)}"
        )
    else:
        base = f"{row.position}: Add {_fmt_player(row.add_name, row.add_team)} — no drop needed (open spot)"
    if row.reasons:
        base += f" ({'; '.join(row.reasons)})"
    return base


@dataclass
class GamePlan:
    week: int
    current_plan: LineupPlan
    base_plan: LineupPlan
    start_sit: list[SwapLine] = field(default_factory=list)
    adds: list[AddDropRec] = field(default_factory=list)
    claims: list[AddDropRec] = field(default_factory=list)
    ir_stash: list = field(default_factory=list)
    unfilled_slots: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    opponent: str = ""

    # Run-level context for the metrics above: the unit every spice/edge
    # term is a fraction of, the roster capacity the add set was fitted
    # into, and the projected weekly totals of the CURRENT lineup versus the
    # recommended post-pickup one. `base_total - current_total` is the whole
    # plan's this-week value in one number, which nothing surfaced before.
    decision_scale: float = 0.0
    open_spots: int = 0
    roster_capacity: int = 0
    current_total: float = 0.0
    base_total: float = 0.0

    # The bound lookups this run's metrics were built from, kept so a
    # consumer can price a player the recommendations never named --
    # `ffbot/week_log.py` uses it to attach the same metric block to every
    # bench and starter, not just the ones a row mentions. Not serialized by
    # anything; `webapi` emits the per-row blocks, never the index itself.
    metrics: "MetricsIndex | None" = None


def _hypothetical_player(bp: BoardPlayer, uid: int, week_pts: float) -> Player:
    """A hypothetical addition's `Player`, for feeding `lineup.optimize()` --
    `board.to_player` plus a THIS-WEEK point estimate (season-scale by
    default), and a negative synthetic id that can't collide with a real
    roster id or `waiver_candidates`' own `-1`.
    """
    return replace(to_player(bp, uid), projected_points=week_pts)


def _opponent_stack_note(
    position: str, team: str, opp_index: dict, weight: float, scale: float,
) -> tuple[float, str]:
    """The opponent-correlation penalty (or leverage bonus) for one
    position/team, and its rendered note.

    Split out of `_opponent_stack_adjustment` so `pair_moves` can reach it
    for a START/SIT line, where only a `Player` is in hand and never a
    `BoardPlayer`. That gap is why `SwapLine.opp_stack_note` was declared,
    serialized, and rendered but never once assigned.
    """
    if not opp_index or weight == 0.0:
        return 0.0, ""
    corr, why = weekmod.opponent_overlap(position, team, opp_index)
    if corr == 0.0:
        return 0.0, ""
    delta = -weight * scale * corr
    label = "opp-stack" if corr > 0 else "leverage"
    sign = "-" if delta < 0 else "+"
    return delta, f"{label}: {why} ({sign}{abs(delta):.1f})"


def _opponent_stack_adjustment(
    bp: BoardPlayer, opp_index: dict, weight: float, scale: float,
) -> tuple[float, str]:
    return _opponent_stack_note(bp.position, bp.team, opp_index, weight, scale)


def _drop_metrics_for(
    metrics: MetricsIndex, roster: Sequence[Player], drop_name: str | None, week: int,
) -> PlayerMetrics | None:
    """The dropped player's metrics, by name off the adjusted roster. `None`
    for a row with no paired drop (an open roster spot) or a name that no
    longer resolves -- both ordinary states, never an error."""
    if not drop_name:
        return None
    p = next((p for p in roster if p.name == drop_name), None)
    return metrics.for_player(p, week=week) if p is not None else None


def build_gameplan(
    loaded: "LoadedReport",
    week_num: int,
    players: Sequence[Player],
    *,
    my_priority: int | None = None,
    weeks_in_season: int = 17,
) -> GamePlan:
    """The end-to-end weekly plan: adjust the current roster, price every
    candidate move off ONE valuation pool, bake certain free-agent adds
    into a post-pickup base lineup, and pair every start/sit change into
    a readable line -- see the module docstring for the three fixes this
    consolidates.

    `players` must already carry the correct BASELINE `selected_position`
    (live Sleeper starters, or a `weekly/lineup_state.yml` stamp on the
    file route) -- the same precondition `week.build_week_brief` has
    always had; this function does not resolve that itself.
    """
    cfg = loaded.cfg
    weekly = loaded.weekly
    board = loaded.board
    pool = loaded.ros_board or board
    league_rosters = loaded.league_rosters
    layout = cfg.roster_positions

    opp_index = weekmod.opponent_stack_index(loaded.opponent_starters) if loaded.opponent_starters else {}
    lean = weekmod._this_week_matchup_lean(players, layout, cfg, board, league_rosters)
    adjusted = weekmod.adjusted_players(players, weekly, cfg.season, loaded.stadiums, lean, loaded.opponent_starters)
    current_plan = optimize(adjusted, layout, week_num, cfg)

    plan = GamePlan(
        week=week_num, current_plan=current_plan, base_plan=current_plan,
        opponent=cfg.league.my_opponent if cfg.league is not None else "",
    )

    metrics = MetricsIndex(
        board=board, pool=pool,
        season_ptd=loaded.season_ptd, season_ptd_games=loaded.season_ptd_games,
        weekly_points=loaded.weekly_points,
    )
    plan.metrics = metrics
    plan.current_total = sum(p.projected_points or 0.0 for _, p in current_plan.assignments)
    plan.base_total = plan.current_total

    if board is None or pool is None:
        # No board: the per-player board valuation is unavailable, but the
        # roster-level numbers (this week's adjusted points, status,
        # ownership) still are, so the lines keep whatever metrics exist
        # rather than dropping to none at all.
        plan.start_sit = pair_moves(current_plan, layout, metrics=metrics, week=week_num)
        plan.unfilled_slots = list(current_plan.unfilled_slots)
        return plan

    naive = cfg.season.waiver_value_mode == "points"
    weeks_remaining = weeks_in_season
    num_teams = max(1, cfg.draft.num_teams)
    priority = my_priority if my_priority is not None else loaded.waiver_priority

    roster_keys, missing = weekmod.roster_board_keys(adjusted, pool)
    rostered_names = {normalize_name(p.name) for p in adjusted} | league_rosters.rostered_names()

    # The wire's next-best-available snapshot -- every denial/handoff gain
    # computed below gets discounted against it (see ffbot/denial.py's
    # fungibility note: a K/DEF hole, or any position with a comparable
    # free agent still sitting on the wire, is NOT real denial value).
    # Computed once, off the SAME pool everything else here reads, and
    # skipped entirely when denial is off -- the usual "don't even ask".
    alternatives = (
        denial.best_available_by_position(pool, rostered_names)
        if cfg.season.denial_weight != 0.0 and league_rosters.teams else None
    )

    # Raw candidate scan for every NON-stream position -- stream positions
    # get their own same-position-swap valuation below instead (a shared
    # worst-hold drop misprices "drop your bye K for a streamer"; see the
    # module docstring). Denial candidates are scanned and merged in the
    # same pass, off the SAME pool, so the two can never disagree about
    # what a player is worth.
    stream_positions = {p.upper() for p in cfg.season.stream_positions}
    raw_candidates, _ = weekmod.waiver_candidates(
        adjusted, pool, layout, cfg, my_priority=priority, weeks_remaining=weeks_remaining,
        league_rosters=league_rosters, limit=10_000, week=week_num, weekly=weekly,
        weekly_points=loaded.weekly_points or None, alternatives=alternatives,
    )
    scale = weekmod.decision_scale(adjusted)
    opp_weight = cfg.season.opponent_correlation_weight

    def _stack_reason_for_name(name: str, position: str, team: str) -> tuple[float, str]:
        if not opp_index or opp_weight == 0.0:
            return 0.0, ""
        corr, why = weekmod.opponent_overlap(position, team, opp_index)
        if corr == 0.0:
            return 0.0, ""
        delta = -opp_weight * scale * corr
        label = "opp-stack" if corr > 0 else "leverage"
        return delta, f"{label}: {why}"

    rows: list[AddDropRec] = []
    bp_by_name = {normalize_name(bp.name): bp for bp in pool.players}
    for c in raw_candidates:
        if c.position.upper() in stream_positions:
            continue  # superseded by the dedicated stream-swap valuation below
        bp = bp_by_name.get(normalize_name(c.add_name))
        stack_delta, stack_reason = (0.0, "")
        if bp is not None:
            stack_delta, stack_reason = _stack_reason_for_name(c.add_name, bp.position, bp.team)
        net = c.net + stack_delta
        if net <= 0.0:
            continue
        reasons = [c.reason]
        if stack_reason:
            reasons.append(stack_reason)
        drop_team = ""
        drop_player = None
        if c.drop_name:
            drop_player = next((p for p in adjusted if p.name == c.drop_name), None)
            drop_team = drop_player.team if drop_player is not None else ""
        rows.append(AddDropRec(
            kind="claim" if c.is_claim else "add",
            position=c.position, add_name=c.add_name, add_team=bp.team if bp is not None else "",
            drop_name=c.drop_name, drop_team=drop_team, drop_reason=c.drop_reason,
            net=net, value=c.value, claim_note=c.claim_note, reasons=tuple(reasons), on_bye=c.on_bye,
            add_metrics=metrics.for_name(
                c.add_name, c.position, bp.team if bp is not None else "", on_bye=c.on_bye,
            ),
            drop_metrics=(
                metrics.for_player(drop_player, week=week_num)
                if c.drop_name and drop_player is not None else None
            ),
            # `ros_gain`/`week_gain`/`paired_drop_cost`/`claim_cost`/`urgency`
            # are the typed components `week.waiver_candidates` has always
            # returned and which nothing has ever read -- see WaiverCandidate's
            # own note. This is the consumer they were added for.
            decision=DecisionMetrics(
                net=net, value=c.value,
                ros_gain=c.ros_gain, week_gain=c.week_gain,
                drop_cost=c.paired_drop_cost, claim_cost=c.claim_cost, urgency=c.urgency,
                stack_delta=stack_delta, decision_scale=scale,
            ),
        ))

    # --- Denial: merged in as a normal row, never a separate section ------
    # Capped at `denial_row_limit` (default 1) -- pure denial is meant to be
    # a rare, high-conviction call, not a routine category with its own row
    # budget the way ordinary adds get `recommend_count`.
    if cfg.season.denial_weight != 0.0 and league_rosters.teams:
        streaming_floor = weekmod.best_streaming_baseline(roster_keys, pool, cfg)
        denial_list = denial.denial_candidates(
            roster_keys, pool, layout, cfg, league_rosters, rostered_names, streaming_floor,
            limit=max(0, cfg.season.denial_row_limit), alternatives=alternatives,
        )
        if denial_list:
            verdict = policy.can_deny_claim(priority if priority is not None else num_teams, cfg)
            if not verdict.allowed:
                plan.notes.append(f"Denial claims suppressed: {verdict.reason}")
                denial_list = []
        existing_names = {normalize_name(r.add_name) for r in rows}
        for d in denial_list:
            if normalize_name(d.add_name) in existing_names:
                continue
            drop_cost, claim_cost, is_claim, claim_note, drop_name, drop_team, drop_reason = _price_a_drop(
                adjusted, roster_keys, pool, layout, cfg, naive, priority, num_teams, d.denial_value,
                league_rosters=league_rosters, alternatives=alternatives,
            )
            net = d.denial_value - drop_cost - claim_cost
            if net <= 0.0:
                continue
            rows.append(AddDropRec(
                kind="claim" if is_claim else "add",
                position=d.position, add_name=d.add_name,
                drop_name=drop_name, drop_team=drop_team, drop_reason=drop_reason,
                net=net, value=d.denial_value, claim_note=claim_note,
                reasons=(d.reason,), denial_team=d.best_team, denial_gain=d.best_gain,
                add_metrics=metrics.for_name(d.add_name, d.position),
                drop_metrics=_drop_metrics_for(metrics, adjusted, drop_name, week_num),
                decision=DecisionMetrics(
                    net=net, value=d.denial_value,
                    drop_cost=drop_cost, claim_cost=claim_cost,
                    denial_gain=d.best_gain, denial_team=d.best_team,
                    decision_scale=scale,
                ),
            ))

    # --- Stream-position rows: same-position swap against the incumbent ---
    for pos in stream_positions:
        rows.extend(_stream_swap_rows(
            adjusted, pool, layout, cfg, weekly, week_num, pos, priority, num_teams,
            rostered_names, weeks_remaining, loaded.weekly_points, opp_index, opp_weight, scale,
            metrics=metrics,
        ))

    rows.sort(key=lambda r: -r.net)
    limit = max(1, cfg.season.recommend_count)
    adds = [r for r in rows if r.kind == "add"][:limit]
    claims = [r for r in rows if r.kind == "claim"][:limit]

    # --- Coherent add transaction set --------------------------------------
    # Every accepted `add` must be independently executable: distinct
    # drops for distinct adds (not all sharing `waiver_candidates`' single
    # best-hold drop), open roster spots consumed first.
    space = weekmod.roster_space(adjusted, layout)
    open_spots = space.open_spots
    droppable = weekmod.ranked_droppable(adjusted, roster_keys, pool, cfg, naive)
    key_to_player = {f"{normalize_name(p.name)}:{_primary_position(p)}": p for p in adjusted}

    # `droppable[0]` is exactly the single shared drop `week.waiver_candidates`
    # priced every non-stream row's `net` against (see that module's "one
    # shared best available drop" note) -- captured here, before any
    # handoff-aware re-sort below, so a row whose ACTUAL drop ends up
    # different can have its `net` corrected against what it was really
    # priced with, not silently drift.
    original_best_key = droppable[0] if droppable else None
    original_best_drop_cost = (
        0.0 if naive or original_best_key is None
        else max(0.0, weekmod.drop_cost(original_best_key, roster_keys, pool, cfg))
    )

    # Prefer a fungible drop (the wire has a comparable replacement) over a
    # scarce one a rival could turn around and claim, even when hold_margin
    # alone ranks them similarly -- reuses `denial.handoff_risk` directly,
    # so a drop's cost here and a candidate's denial value above are priced
    # by the identical mechanism. Skipped (bit-identical to plain hold-
    # margin order) whenever denial is off or in naive mode, which does no
    # cost/benefit accounting at all.
    handoff_by_key: dict[str, tuple[float, str]] = {}
    if cfg.season.denial_weight != 0.0 and league_rosters.teams and not naive:
        for k in droppable:
            bp_for_drop = pool.by_key.get(k)
            handoff_by_key[k] = (
                denial.handoff_risk(bp_for_drop, league_rosters, pool, layout, cfg, alternatives)
                if bp_for_drop is not None else (0.0, "")
            )
        droppable = sorted(
            droppable,
            key=lambda k: weekmod.hold_margin(k, roster_keys, pool, cfg, key_to_player[k].blocking)
            + handoff_by_key.get(k, (0.0, ""))[0],
        )

    # Claims independently reuse the SAME handoff-aware best drop too --
    # unlike adds (which must partition DISTINCT drops across one coherent
    # transaction, since they all happen at once), each claim is its own
    # hypothetical "if I make just this one move" scenario, so every claim
    # may point at the same best available drop with no conflict. Most
    # positive-gain rows end up typed "claim" under the default priority
    # economics (see `week.claim_verdict`), so this is where handoff
    # pricing actually shows up in practice, not just the `adds` list.
    if droppable:
        best_drop_key = droppable[0]
        resolved_claim_rows: list[AddDropRec] = []
        for row in claims:
            if row.position.upper() in stream_positions or row.drop_name is None:
                resolved_claim_rows.append(row)
                continue
            drop_player = key_to_player.get(best_drop_key)
            if drop_player is None:
                resolved_claim_rows.append(row)
                continue
            handoff_val, handoff_team = handoff_by_key.get(best_drop_key, (0.0, ""))
            new_drop_cost = 0.0 if naive else max(0.0, weekmod.drop_cost(best_drop_key, roster_keys, pool, cfg))
            old_drop_cost = 0.0 if row.drop_name is None else original_best_drop_cost
            reasons = list(row.reasons)
            drop_reason = row.drop_reason
            if handoff_val > 0.0 and handoff_team:
                reasons.append(f"{handoff_team} could claim him (+{handoff_val:.1f} to their lineup)")
                drop_reason = f"{handoff_team} could claim him"
            repriced_net = row.net + old_drop_cost - new_drop_cost - handoff_val
            resolved_claim_rows.append(replace(
                row, drop_name=drop_player.name, drop_team=drop_player.team, drop_reason=drop_reason,
                net=repriced_net, reasons=tuple(reasons),
                drop_metrics=metrics.for_player(drop_player, week=week_num),
                decision=replace(
                    row.decision or DecisionMetrics(),
                    net=repriced_net, drop_cost=new_drop_cost,
                    handoff_value=handoff_val, handoff_team=handoff_team,
                    hold_margin=weekmod.hold_margin(
                        best_drop_key, roster_keys, pool, cfg, drop_player.blocking,
                    ),
                ),
            ))
        claims = resolved_claim_rows

    consumed_drop_keys: set = set()
    added_players: list[Player] = []
    dropped_players: list[Player] = []
    accepted_adds: list[AddDropRec] = []
    next_uid = _SYNTHETIC_ID_START

    for row in adds:
        drop_player = None
        # A stream-position row already names a SPECIFIC incumbent to swap
        # out (see `_stream_swap_rows`) -- its `gain` was computed against
        # that exact swap, so it must be honored as-is rather than
        # reassigned from the shared `droppable` ranking below (which would
        # silently re-price the row against a different player than the one
        # its `net` was actually computed for). Consumes neither an open
        # spot nor a `droppable` entry -- a same-position swap doesn't
        # change total roster size.
        if row.position.upper() in stream_positions and row.drop_name:
            drop_player = next((p for p in adjusted if p.name == row.drop_name), None)
        elif open_spots > 0:
            open_spots -= 1
        else:
            drop_key = next((k for k in droppable if k not in consumed_drop_keys), None)
            if drop_key is None:
                continue  # nobody left to drop -- this add can't actually be made
            consumed_drop_keys.add(drop_key)
            drop_player = key_to_player.get(drop_key)
            handoff_val, handoff_team = handoff_by_key.get(drop_key, (0.0, ""))
            new_drop_cost = 0.0 if naive else max(0.0, weekmod.drop_cost(drop_key, roster_keys, pool, cfg))
            old_drop_cost = 0.0 if row.drop_name is None else original_best_drop_cost
            reasons = list(row.reasons)
            drop_reason = row.drop_reason
            if handoff_val > 0.0 and handoff_team:
                reasons.append(f"{handoff_team} could claim him (+{handoff_val:.1f} to their lineup)")
                drop_reason = f"{handoff_team} could claim him"
            repriced_net = row.net + old_drop_cost - new_drop_cost - handoff_val
            row = replace(
                row, drop_name=drop_player.name if drop_player else row.drop_name,
                drop_team=drop_player.team if drop_player else row.drop_team,
                drop_reason=drop_reason,
                # Re-priced against the drop ACTUALLY assigned here, not the
                # single shared drop `week.waiver_candidates` guessed at.
                net=repriced_net,
                reasons=tuple(reasons),
                drop_metrics=(
                    metrics.for_player(drop_player, week=week_num)
                    if drop_player is not None else row.drop_metrics
                ),
                decision=replace(
                    row.decision or DecisionMetrics(),
                    net=repriced_net, drop_cost=new_drop_cost,
                    handoff_value=handoff_val, handoff_team=handoff_team,
                    hold_margin=weekmod.hold_margin(
                        drop_key, roster_keys, pool, cfg,
                        drop_player.blocking if drop_player is not None else False,
                    ),
                ),
            )
        bp = bp_by_name.get(normalize_name(row.add_name))
        week_pts, _ = weekmod.candidate_week_points(bp, week_num, weekly, loaded.weekly_points, weeks_remaining, cfg.season) if bp else (0.0, False)
        added_players.append(_hypothetical_player(bp, next_uid, week_pts) if bp else None)
        if drop_player is not None:
            dropped_players.append(drop_player)
        next_uid -= 1
        row.text = _adddrop_text(row)
        accepted_adds.append(row)

    added_players = [p for p in added_players if p is not None]
    added_ids = frozenset(p.player_id for p in added_players)
    dropped_names = {p.name for p in dropped_players}
    post_roster = [p for p in adjusted if p.name not in dropped_names] + added_players
    base_plan = optimize(post_roster, layout, week_num, cfg)
    plan.base_plan = base_plan
    plan.start_sit = pair_moves(
        base_plan, layout, added_ids=added_ids, dropped=dropped_players,
        metrics=metrics, week=week_num, decision_scale=scale,
        opp_index=opp_index, opp_weight=opp_weight,
    )
    plan.unfilled_slots = list(base_plan.unfilled_slots)
    plan.adds = accepted_adds
    plan.missing = list(missing)
    plan.decision_scale = scale
    plan.open_spots = space.open_spots
    plan.roster_capacity = space.capacity
    plan.base_total = sum(p.projected_points or 0.0 for _, p in base_plan.assignments)

    # --- Per-claim conditional consequence ---------------------------------
    base_starter_names = {p.name for _, p in base_plan.assignments}
    resolved_claims: list[AddDropRec] = []
    for row in claims:
        bp = bp_by_name.get(normalize_name(row.add_name))
        if bp is None:
            row.text = _adddrop_text(row)
            resolved_claims.append(row)
            continue
        drop_player = next((p for p in post_roster if p.name == row.drop_name), None)
        if drop_player is None and row.drop_name:
            drop_key = next((k for k in droppable if k not in consumed_drop_keys), None)
            if drop_key is not None:
                consumed_drop_keys.add(drop_key)
                drop_player = key_to_player.get(drop_key)
                row = replace(
                    row, drop_name=drop_player.name, drop_team=drop_player.team,
                    drop_metrics=metrics.for_player(drop_player, week=week_num),
                )
        week_pts, _ = weekmod.candidate_week_points(bp, week_num, weekly, loaded.weekly_points, weeks_remaining, cfg.season)
        claim_player = _hypothetical_player(bp, next_uid, week_pts)
        next_uid -= 1
        hyp_roster = [p for p in post_roster if drop_player is None or p.name != drop_player.name] + [claim_player]
        hyp_plan = optimize(hyp_roster, layout, week_num, cfg)
        hyp_starter_names = {p.name for _, p in hyp_plan.assignments}
        starts = claim_player.name in hyp_starter_names
        if starts:
            slot_display = next(
                (display_slot(slot) for slot, p in hyp_plan.assignments if p.name == claim_player.name), "",
            )
            displaced = base_starter_names - hyp_starter_names
            over_name = next(iter(displaced)) if displaced else ""
            base_total = sum(p.projected_points or 0.0 for _, p in base_plan.assignments)
            hyp_total = sum(p.projected_points or 0.0 for _, p in hyp_plan.assignments)
            week_delta = hyp_total - base_total
            text = (
                f"if it clears: start at {slot_display} over {over_name} (+{week_delta:.1f} this week)"
                if over_name else f"if it clears: start at {slot_display} (+{week_delta:.1f} this week)"
            )
            consequence = ClaimConsequence(
                starts=True, slot_display=slot_display, over_name=over_name,
                week_delta=week_delta, text=text,
                base_total=base_total, hyp_total=hyp_total,
            )
        else:
            consequence = ClaimConsequence(starts=False, text="if it clears: bench depth only this week")
        row.if_clears = consequence
        if row.decision is not None:
            row.decision = replace(row.decision, week_delta=consequence.week_delta)
        row.text = _adddrop_text(row)
        resolved_claims.append(row)

    plan.claims = resolved_claims

    ir_candidates = weekmod.ir_stash_candidates(adjusted, pool, layout, weekly, cfg, league_rosters=league_rosters)
    plan.ir_stash = ir_candidates

    return plan


def _price_a_drop(
    roster: Sequence[Player], roster_keys, pool: Board, layout, cfg: Config, naive: bool,
    priority, num_teams, gain: float,
    league_rosters: LeagueRosters | None = None,
    alternatives: dict | None = None,
):
    """Drop pairing + claim economics for a row NOT produced by
    `week.waiver_candidates` itself (denial rows) -- reuses the exact same
    `ranked_droppable`/`claim_verdict` machinery so a denial add is priced
    on identical footing to an ordinary one. `league_rosters`/`alternatives`
    (default `None`) add handoff-aware drop selection -- see
    `denial.handoff_risk`; `None` skips it entirely (bit-identical)."""
    key_to_player = {f"{normalize_name(p.name)}:{_primary_position(p)}": p for p in roster}
    space = weekmod.roster_space(roster, layout)
    droppable = weekmod.ranked_droppable(roster, roster_keys, pool, cfg, naive)
    handoff_val, handoff_team = 0.0, ""
    if space.open_spots > 0:
        drop_cost, drop_name, drop_team, drop_reason = 0.0, None, "", "open roster spot — no drop needed"
    elif droppable:
        best_key = droppable[0]
        if league_rosters is not None and league_rosters.teams and not naive:
            scored = [
                (
                    k,
                    weekmod.hold_margin(k, roster_keys, pool, cfg, key_to_player[k].blocking)
                    + denial.handoff_risk(pool.by_key[k], league_rosters, pool, layout, cfg, alternatives)[0],
                )
                for k in droppable if k in pool.by_key
            ]
            if scored:
                scored.sort(key=lambda t: t[1])
                best_key = scored[0][0]
        best_player = key_to_player[best_key]
        drop_cost = max(0.0, weekmod.drop_cost(best_key, roster_keys, pool, cfg)) if not naive else 0.0
        if league_rosters is not None and pool.by_key.get(best_key) is not None:
            handoff_val, handoff_team = denial.handoff_risk(pool.by_key[best_key], league_rosters, pool, layout, cfg, alternatives)
        drop_name, drop_team = best_player.name, best_player.team
        drop_reason = (
            f"{handoff_team} could claim him" if handoff_val > 0.0 and handoff_team
            else ("lowest projected points on your roster" if naive else "worst hold value on your roster")
        )
    else:
        drop_cost, drop_name, drop_team, drop_reason = 0.0, None, "", "no droppable player found — roster is full of protected players"
    claim_cost, claim_note, is_claim = weekmod.claim_verdict(gain, priority, num_teams, cfg)
    return drop_cost + handoff_val, claim_cost, is_claim, claim_note, drop_name, drop_team, drop_reason


def _stream_swap_rows(
    roster: Sequence[Player], pool: Board, layout, cfg: Config, weekly, week_num: int, position: str,
    priority, num_teams: int, rostered_names: set, weeks_remaining: int,
    weekly_points: dict | None, opp_index: dict, opp_weight: float, scale: float,
    metrics: "MetricsIndex | None" = None,
) -> list[AddDropRec]:
    """Same-position swap valuation for one streaming position: candidate
    priced against the CURRENT incumbent at that position (not a shared
    worst-hold drop), so a bye/OUT/outscored K or DEF surfaces a real row
    even when nothing else on the roster is worth touching.
    """
    from .draft import _season_score

    naive = cfg.season.waiver_value_mode == "points"
    incumbent = next(
        (p for p in roster if _primary_position(p) == position and p.selected_position not in IR_SLOTS), None,
    )
    week_base = sum(p.projected_points or 0.0 for _, p in optimize(list(roster), layout, week_num, cfg).assignments)
    roster_keys, _ = weekmod.roster_board_keys(roster, pool)
    incumbent_key = None
    if incumbent is not None:
        incumbent_key = f"{normalize_name(incumbent.name)}:{position}"
    base_season = _season_score(pool, roster_keys, None, cfg)

    incumbent_out = (
        incumbent is None
        or (week_num is not None and incumbent.bye_week == week_num)
        or incumbent.status in weekmod.STATUS_OUT
    )

    out: list[AddDropRec] = []
    for bp in pool.players:
        if bp.position != position or normalize_name(bp.name) in rostered_names:
            continue
        week_pts, on_bye = weekmod.candidate_week_points(bp, week_num, weekly, weekly_points, weeks_remaining, cfg.season)
        if on_bye:
            continue
        candidate = _hypothetical_player(bp, -1, week_pts)
        trial_roster = [p for p in roster if incumbent is None or p.name != incumbent.name] + [candidate]
        trial_total = sum(p.projected_points or 0.0 for _, p in optimize(trial_roster, layout, week_num, cfg).assignments)
        week_gain = trial_total - week_base

        # Season-scale half of the blend: the SAME swap (incumbent out,
        # candidate in), not a plain marginal add -- the roster keeps a
        # player at `position` either way, so there is no replacement
        # subtraction here the way `waiver_candidates`' own pure-add gain
        # needs one. No incumbent (nobody currently rostered at this
        # position at all) makes it a plain addition instead.
        if naive:
            ros_gain = 0.0
        elif incumbent_key is not None and incumbent_key in roster_keys:
            swapped_keys = [k for k in roster_keys if k != incumbent_key] + [bp.key]
            ros_gain = _season_score(pool, swapped_keys, None, cfg) - base_season
        else:
            ros_gain = _season_score(pool, roster_keys, bp, cfg) - base_season

        blend = cfg.season.ros_blend
        gain = week_gain if naive else (blend * ros_gain + (1.0 - blend) * week_gain)
        # A genuine need (incumbent on bye/OUT/missing) still surfaces a
        # row even at a small or borderline gain -- there is no "keep the
        # zero-point starter" alternative to compare it against. Otherwise
        # the ordinary bar applies: not worth recommending unless it's a
        # real improvement.
        if gain <= 0.0 and not incumbent_out:
            continue

        stack_delta, stack_reason = (0.0, "")
        if opp_index and opp_weight != 0.0:
            corr, why = weekmod.opponent_overlap(position, bp.team, opp_index)
            if corr != 0.0:
                stack_delta = -opp_weight * scale * corr
                stack_reason = f"{'opp-stack' if corr > 0 else 'leverage'}: {why}"

        claim_cost, claim_note, is_claim = weekmod.claim_verdict(gain, priority, num_teams, cfg)
        net = gain + stack_delta - claim_cost
        if net <= 0.0 and not incumbent_out:
            continue

        reasons = [f"+{gain:.1f} {'this week' if naive else 'blended'} vs your current {position}"]
        forced_need = ""
        if incumbent_out and incumbent is not None:
            why = "on bye this week" if incumbent.bye_week == week_num else f"status {incumbent.status}"
            forced_need = f"your {position} {incumbent.name} is {why}"
            reasons.append(forced_need)
        elif incumbent is None:
            forced_need = f"no {position} currently rostered"
            reasons.append(forced_need)
        if stack_reason:
            reasons.append(stack_reason)

        out.append(AddDropRec(
            kind="claim" if is_claim else "add",
            position=position, add_name=bp.name, add_team=bp.team,
            drop_name=incumbent.name if incumbent else None,
            drop_team=incumbent.team if incumbent else "",
            drop_reason="" if incumbent is None else f"streaming {position}",
            net=net, value=gain, claim_note=claim_note, reasons=tuple(reasons),
            forced_need=forced_need, on_bye=False,
            add_metrics=(
                metrics.for_name(bp.name, position, bp.team, week_proj=week_pts)
                if metrics is not None else None
            ),
            drop_metrics=(
                metrics.for_player(incumbent, week=week_num)
                if metrics is not None and incumbent is not None else None
            ),
            # `gain` here is already the ros/week BLEND (see above); keeping
            # only it is what made a one-week rental and a real rest-of-season
            # upgrade render identically. Both halves are carried now.
            decision=DecisionMetrics(
                net=net, value=gain,
                ros_gain=ros_gain, week_gain=week_gain,
                claim_cost=claim_cost, stack_delta=stack_delta, decision_scale=scale,
            ),
        ))

    out.sort(key=lambda r: -r.net)
    return out[: max(1, cfg.season.recommend_count)]
