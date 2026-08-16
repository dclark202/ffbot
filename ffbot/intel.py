"""Researched player intel — the one place non-consensus information enters.

Everything else on the board comes from a single FantasyPros export, which is
the market's own opinion. Beating the market with the market's numbers is not
possible, so this module carries the outside view: camp reports, depth-chart
changes, offensive-line and scheme changes, breakout profiles.

The file is produced by research *before* the draft and frozen. Nothing here
touches the network, because `recommend()` runs against a pick clock and the
whole draft path is deliberately offline. Refreshing intel means re-running the
research and rewriting the file, not calling anything at runtime.

Matching is by normalized name (`names.normalize_name`), so "Marvin Harrison
Jr." in the intel file finds "Marvin Harrison" on the board. A name that
matches nothing **warns and suggests** rather than failing or silently
vanishing — a typo'd intel entry that disappears without comment is how you
discover on draft day that your research never loaded.

The factual/speculative line, per the league owner: what is *verifiable* gets
baked into the ranking, what is rumor stays a note. Concretely:

- `upside` (0-100) raises value in the mid/late rounds — researched breakout
  potential beyond consensus.
- `risk` (0-100) lowers value in every round, and is reserved for
  **availability** — will he be on the field? Suspensions, PUP designations,
  holdouts, injuries with no timetable. A hard fact rates high; conflicting
  reports rate moderate *with* a note carrying the speculative remainder.
  Opinion fades (bad offense, camp struggles) must never use `risk`; they are
  notes, so the math cannot quietly bury a player over a take.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .board import Board, BoardPlayer
from .names import normalize_name, search_scored


@dataclass(frozen=True)
class IntelEntry:
    """One researched player."""

    name: str  # as written in the intel file, for error messages
    upside: float | None = None  # 0-100 breakout potential vs consensus
    risk: float | None = None  # 0-100 availability risk — see module docstring
    note: str = ""  # plain-English "why", shown in recommendations
    flags: tuple[str, ...] = ()


class IntelError(ValueError):
    """The intel file exists but could not be understood."""


def _coerce_entry(name: str, raw: Any) -> IntelEntry:
    if raw is None:
        return IntelEntry(name=name)
    if isinstance(raw, str):
        # Shorthand: a bare string is just the note. Most players need only a
        # sentence of "why", and making them write a mapping for that is
        # friction on the part of the process that should stay cheap.
        return IntelEntry(name=name, note=raw.strip())
    if not isinstance(raw, dict):
        raise IntelError(f"{name!r}: expected a mapping or a string, got {type(raw).__name__}")

    def _score(field_name: str):
        val = raw.get(field_name)
        if val is None:
            return None
        try:
            val = float(val)
        except (TypeError, ValueError):
            raise IntelError(f"{name!r}: {field_name} must be a number, got {val!r}") from None
        if not 0.0 <= val <= 100.0:
            raise IntelError(f"{name!r}: {field_name} must be 0-100, got {val}")
        return val

    upside = _score("upside")
    risk = _score("risk")

    flags = raw.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    if not isinstance(flags, list):
        raise IntelError(f"{name!r}: flags must be a list, got {type(flags).__name__}")

    note = raw.get("note") or ""
    # YAML block scalars keep the newlines and indentation of the source file;
    # these land in a single-line terminal table, so collapse the whitespace.
    note = " ".join(str(note).split())

    return IntelEntry(
        name=name,
        upside=upside,
        risk=risk,
        note=note,
        flags=tuple(str(f) for f in flags),
    )


def load_intel(path: str | Path) -> dict[str, IntelEntry]:
    """Parse an intel file into `{normalized name: IntelEntry}`.

    A missing file is not an error — intel is optional, and its absence must
    leave the board exactly as it would have been. An empty/falsy `path` is
    the same no-op case (e.g. `cfg.draft.intel_file` left unset) -- checked
    before `Path(path)` because `Path("")` resolves to `Path(".")`, the cwd,
    which `.exists()` reports True for; falling through would try to
    `read_text()` a directory and raise instead of degrading gracefully.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise IntelError(f"{p}: expected a mapping at the top level")

    players = raw.get("players") or {}
    if not isinstance(players, dict):
        raise IntelError(f"{p}: 'players' must be a mapping of name -> intel")

    out: dict[str, IntelEntry] = {}
    for name, body in players.items():
        entry = _coerce_entry(str(name), body)
        key = normalize_name(str(name))
        if key in out:
            warnings.warn(f"{p}: duplicate intel for {name!r}; keeping the last", stacklevel=2)
        out[key] = entry
    return out


def apply_intel(board: Board, intel: dict[str, IntelEntry]) -> Board:
    """Return a copy of `board` with intel merged onto matching players.

    Matches on normalized name across all positions, so an entry applies to a
    player regardless of how the board spells them. Entries matching nothing
    are reported with a best-guess suggestion.
    """
    if not intel:
        return board

    by_norm: dict[str, list[BoardPlayer]] = {}
    for bp in board.players:
        by_norm.setdefault(normalize_name(bp.name), []).append(bp)

    matched: set[str] = set()
    new_players: list[BoardPlayer] = []
    for bp in board.players:
        entry = intel.get(normalize_name(bp.name))
        if entry is None:
            new_players.append(bp)
            continue
        matched.add(normalize_name(bp.name))
        new_players.append(
            replace(
                bp,
                upside=entry.upside if entry.upside is not None else bp.upside,
                availability_risk=entry.risk if entry.risk is not None else bp.availability_risk,
                intel_note=entry.note or bp.intel_note,
                intel_flags=entry.flags or bp.intel_flags,
            )
        )

    unmatched = [entry for key, entry in intel.items() if key not in matched]
    if unmatched:
        _warn_unmatched(unmatched, board)

    # `dataclasses.replace`, not a hand-listed Board(...) constructor: this
    # only ever changes the player list, and enumerating the other fields
    # meant every field added to `Board` afterwards was silently dropped on
    # the way through here. `scoring_residual`, `bench_replacement`, and
    # `predictiveness` had all already been lost this way -- invisibly,
    # since each degrades to a harmless-looking empty dict, so the feature
    # reading it simply did nothing on any board loaded via
    # `load_board_from_config`.
    return replace(board, players=new_players, by_key={bp.key: bp for bp in new_players})


def _warn_unmatched(unmatched: list[IntelEntry], board: Board) -> None:
    """Report intel that matched nobody, with the closest board name.

    Uses the same permissive search the TUI uses, so a suggestion appears even
    for a fairly wrong spelling — the point is to make the fix obvious, not to
    guess silently on the user's behalf.
    """
    lines = []
    for entry in unmatched:
        hits = search_scored(entry.name, board.players)
        hint = f" — did you mean {hits[0][1].name!r}?" if hits else ""
        lines.append(f"  {entry.name!r}{hint}")
    warnings.warn(
        f"{len(unmatched)} intel entries matched no player on the board:\n"
        + "\n".join(lines),
        stacklevel=3,
    )


def diff_intel(
    old: dict[str, IntelEntry], new: dict[str, IntelEntry]
) -> dict[str, list[str]]:
    """Human-readable changes between two intel passes.

    Returns `{"added": [...], "removed": [...], "changed": [...]}`, each line
    already formatted for printing. This is what makes a refresh reviewable:
    "what did the research change since last time" is the entire point of
    re-running it, and eyeballing two YAML files does not answer it.
    """
    added = [f"{e.name}" + (f" (upside {e.upside:.0f})" if e.upside else "")
             for k, e in sorted(new.items()) if k not in old]
    removed = [old[k].name for k in sorted(old) if k not in new]

    changed: list[str] = []
    for k in sorted(set(old) & set(new)):
        o, n = old[k], new[k]
        deltas: list[str] = []
        for field_name in ("upside", "risk"):
            ov, nv = getattr(o, field_name), getattr(n, field_name)
            if ov != nv:
                fmt = lambda v: "-" if v is None else f"{v:.0f}"  # noqa: E731
                deltas.append(f"{field_name} {fmt(ov)} -> {fmt(nv)}")
        if o.note != n.note:
            deltas.append("note updated")
        if o.flags != n.flags:
            deltas.append(f"flags {sorted(set(n.flags) - set(o.flags)) or sorted(set(o.flags) - set(n.flags))}")
        if deltas:
            changed.append(f"{n.name}: {'; '.join(deltas)}")

    return {"added": added, "removed": removed, "changed": changed}


def coverage(board: Board, top_n: int = 200) -> tuple[int, int, list[BoardPlayer]]:
    """How much of the draftable universe has intel.

    Returns `(covered, total, missing)` over the `top_n` players by ADP —
    the players who will actually be drafted. Board rank is not the right
    ordering here: a player our board dislikes but the market takes in round 4
    is someone we still need an opinion about.
    """
    ranked = sorted(
        [bp for bp in board.players if bp.adp is not None],
        key=lambda b: b.adp,
    )[:top_n]
    missing = [bp for bp in ranked if not bp.intel_note and bp.upside is None]
    return len(ranked) - len(missing), len(ranked), missing
