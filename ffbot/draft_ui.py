"""Pure state machine for the live draft TUI.

`handle(state, line) -> UiState` and `render(state) -> str` have no terminal
dependency, so the whole interaction loop is unit-testable without a pty.
`scripts/draft.py` is a thin shell around these two functions plus the
redraw/input loop.

`UiState.draft` (a `DraftState`) is mutated in place by `record`/`undo` —
pick history is the one thing here that is genuinely expensive to copy on
every keystroke (it carries the whole board) and there is no benefit to
pretending otherwise. Everything specific to *this* UI turn (the message,
pending disambiguation, sort order, filter) is replaced immutably, so
`render()` is still a pure function of the `UiState` it's given.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .board import Board, BoardPlayer
from .config import Config
from .draft import DraftState, Recommendation, alerts, needs_between, recommend, round_and_slot
from .names import search_scored

_SORT_ORDER = ("value", "vor", "adp", "urgency")
_PANEL_WIDTH = 88


@dataclass
class UiState:
    draft: DraftState
    cfg: Config
    pending: list[BoardPlayer] = field(default_factory=list)
    message: str = ""
    sort: str = "value"
    filter_pos: str | None = None
    sync_status: str = "off"  # "off" | "live" | "degraded"
    should_quit: bool = False


def _replace(state: UiState, **changes) -> UiState:
    fields = dict(
        draft=state.draft,
        cfg=state.cfg,
        pending=state.pending,
        message=state.message,
        sort=state.sort,
        filter_pos=state.filter_pos,
        sync_status=state.sync_status,
        should_quit=state.should_quit,
    )
    fields.update(changes)
    return UiState(**fields)


def _pool(board: Board, taken: set[str]) -> list[BoardPlayer]:
    return [bp for bp in board.players if bp.key not in taken]


def _pick_label(pick_number: int, mine: bool) -> str:
    return f"pick {pick_number} ({'you' if mine else 'opponent'})"


def _search_and_pick(state: UiState, query: str, mine: bool | None) -> UiState:
    query = query.strip()
    if not query:
        return _replace(state, message="type a player name")

    board = state.draft.board
    available = _pool(board, state.draft.taken_keys())
    scored = search_scored(query, available)
    if not scored:
        return _replace(state, message=f'no match for "{query}"', pending=[])

    top_score = scored[0][0]
    tied = [bp for score, bp in scored if score == top_score]
    if len(tied) > 1:
        return _replace(
            state,
            pending=tied[:9],
            message=f'{len(tied)} matches for "{query}" — type a number to pick one',
        )

    return _apply_pick(state, tied[0], mine)


def _apply_pick(state: UiState, bp: BoardPlayer, mine: bool | None) -> UiState:
    try:
        state.draft.record(bp.key, mine=mine)
    except ValueError as exc:
        return _replace(state, message=str(exc), pending=[])
    pick = state.draft.picks[-1]
    return _replace(state, message=f"{bp.name} ({bp.position}) — {_pick_label(pick.number, pick.mine)}", pending=[])


def _record_missed(state: UiState) -> UiState:
    state.draft.record(None)
    pick = state.draft.picks[-1]
    return _replace(state, message=f"pick {pick.number} recorded as unknown", pending=[])


def _undo(state: UiState) -> UiState:
    popped = state.draft.undo()
    if popped is None:
        return _replace(state, message="nothing to undo", pending=[])
    if popped.key is None:
        return _replace(state, message=f"undid pick {popped.number}", pending=[])
    bp = state.draft.board.by_key.get(popped.key)
    name = bp.name if bp is not None else popped.key
    return _replace(state, message=f"undid pick {popped.number} ({name})", pending=[])


def _inspect(state: UiState, query: str) -> UiState:
    query = query.strip()
    if not query:
        return _replace(state, message="type a player name to inspect")
    board = state.draft.board
    scored = search_scored(query, board.players)
    if not scored:
        return _replace(state, message=f'no match for "{query}"', pending=[])
    _, bp = scored[0]
    status = "TAKEN" if bp.key in state.draft.taken_keys() else "available"
    bye = bp.bye_week if bp.bye_week is not None else "-"
    adp = f"{bp.adp:.1f}" if bp.adp is not None else "-"
    msg = (
        f"{bp.name} {bp.position} {bp.team} — bye {bye}, proj {bp.points:.1f}, "
        f"vor {bp.vor:.1f}, tier {bp.tier}, adp {adp} [{status}]"
    )
    return _replace(state, message=msg, pending=[])


def _cycle_sort(state: UiState) -> UiState:
    idx = _SORT_ORDER.index(state.sort) if state.sort in _SORT_ORDER else 0
    new_sort = _SORT_ORDER[(idx + 1) % len(_SORT_ORDER)]
    return _replace(state, sort=new_sort, message=f"sort: {new_sort}")


def _set_filter(state: UiState, arg: str) -> UiState:
    pos = arg.strip().upper()
    if not pos:
        return _replace(state, filter_pos=None, message="filter cleared", pending=[])
    return _replace(state, filter_pos=pos, message=f"filtering to {pos}", pending=[])


def _set_my_slot(state: UiState, arg: str) -> UiState:
    try:
        slot = int(arg.strip())
    except ValueError:
        return _replace(state, message=f"invalid slot: {arg.strip()!r}")
    if not (1 <= slot <= state.draft.num_teams):
        return _replace(state, message=f"slot must be in 1..{state.draft.num_teams}")
    state.draft.my_slot = slot
    return _replace(state, message=f"my draft slot set to {slot}", pending=[])


def handle(state: UiState, line: str) -> UiState:
    """Apply one command line. Returns the (possibly same) resulting state."""
    line = line.rstrip("\n")
    if not line.strip():
        return _replace(state)

    if state.pending and line.strip().isdigit():
        idx = int(line.strip()) - 1
        if 0 <= idx < len(state.pending):
            return _apply_pick(state, state.pending[idx], mine=None)
        return _replace(state, message=f"no option {line.strip()}")

    stripped = line.strip()
    if stripped == "u":
        return _undo(state)
    if stripped == "x":
        return _record_missed(state)
    if stripped == "s":
        return _cycle_sort(state)
    if stripped == "q":
        return _replace(state, should_quit=True)
    if stripped.startswith("p "):
        return _set_filter(state, stripped[2:])
    if stripped == "p":
        return _set_filter(state, "")
    if stripped.startswith("me "):
        return _set_my_slot(state, stripped[3:])
    if stripped.startswith("?"):
        return _inspect(state, stripped[1:])
    if stripped.startswith("*"):
        return _search_and_pick(state, stripped[1:], mine=True)
    if stripped.startswith("-"):
        return _search_and_pick(state, stripped[1:], mine=False)

    return _search_and_pick(state, stripped, mine=None)


# --- Rendering ---------------------------------------------------------


def _sorted_recs(recs: list[Recommendation], sort: str) -> list[Recommendation]:
    if sort == "vor":
        return sorted(recs, key=lambda r: -r.vor)
    if sort == "adp":
        return sorted(recs, key=lambda r: r.player.adp if r.player.adp is not None else float("inf"))
    if sort == "urgency":
        return sorted(
            recs,
            key=lambda r: -(r.value * (1.0 - (r.survival if r.survival is not None else 0.0))),
        )
    return sorted(recs, key=lambda r: -r.value)


def render(state: UiState) -> str:
    draft = state.draft
    cfg = state.cfg
    current = draft.current_pick()
    round_, slot_on_clock = round_and_slot(current, draft.num_teams)
    next_pick = draft.next_my_pick()
    my_upcoming = [p for p in draft.my_picks() if p >= current][:2]

    on_clock = "YOU ARE ON THE CLOCK" if slot_on_clock == draft.my_slot else f"team {slot_on_clock} on the clock"
    header = f"PICK {current} · R{round_}.{slot_on_clock:02d} · {on_clock}"
    if my_upcoming:
        gap = f" ({next_pick - current} away)" if next_pick is not None else ""
        header += f"    next: {', '.join(str(p) for p in my_upcoming)}{gap}"
    header += f"    sync: {state.sync_status}"

    lines = [header, "-" * _PANEL_WIDTH]

    if state.pending:
        lines.append("Multiple matches — type a number to pick one:")
        for i, bp in enumerate(state.pending, start=1):
            lines.append(f"  {i}) {bp.name} ({bp.position} {bp.team})")
    else:
        recs = recommend(draft, cfg, limit=cfg.draft.recommend_count, position=state.filter_pos)
        recs = _sorted_recs(recs, state.sort)
        lines.append(
            f"{'#':<3}{'PLAYER':<20}{'POS':<4}{'TM':<4}{'BYE':<5}"
            f"{'PROJ':>7}{'VOR':>7}{'NEED':>7}{'VAL':>7}{'ADP':>6}{'SURV':>6}  WHY"
        )
        for i, r in enumerate(recs, start=1):
            bp = r.player
            surv_s = f"{r.survival * 100:.0f}%" if r.survival is not None else "-"
            adp_s = f"{bp.adp:.0f}" if bp.adp is not None else "-"
            bye_s = str(bp.bye_week) if bp.bye_week is not None else "-"
            lines.append(
                f"{i:<3}{bp.name:<20}{bp.position:<4}{bp.team:<4}{bye_s:<5}"
                f"{bp.points:>7.1f}{bp.vor:>7.1f}{r.need:>7.1f}{r.value:>7.1f}"
                f"{adp_s:>6}{surv_s:>6}  {r.reason}"
            )
        if not recs:
            lines.append("(no players match the current filter)")

    lines.append("-" * _PANEL_WIDTH)

    triggered = alerts(draft, cfg)
    if triggered:
        lines.append("ALERTS")
        for a in triggered:
            lines.append(f"  {a}")
        lines.append("-" * _PANEL_WIDTH)

    roster = draft.my_roster()
    lines.append(f"MY ROSTER ({len(roster)})")
    for bp in roster:
        lines.append(f"  {bp.position:<5} {bp.name} ({bp.points:.0f})")

    nb = needs_between(draft)
    if nb:
        summary = ", ".join(f"{n} {pos}" for pos, n in sorted(nb.items(), key=lambda kv: -kv[1]))
        lines.append("")
        lines.append(f"SINCE YOUR LAST PICK: {summary}")

    recent = [p for p in draft.picks if p.key is not None][-5:]
    if recent:
        lines.append("")
        lines.append("LAST PICKS")
        for p in reversed(recent):
            bp = draft.board.by_key.get(p.key)
            name = bp.name if bp is not None else p.key
            who = "YOU" if p.mine else "opp"
            lines.append(f"  {p.number:>4}  {who:<3}  {name}")

    lines.append("-" * _PANEL_WIDTH)
    if state.message:
        lines.append(state.message)

    return "\n".join(lines)
