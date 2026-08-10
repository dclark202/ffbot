"""Read/write `roster.yml` for the GUI's roster editor.

Reading reuses `roster_source.load_roster_entries` directly — this module's
only new logic is the write side, which `roster_source` never needed since
`roster.yml` has always been hand-edited.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import roster_source as rs

_OPTIONAL_FIELDS = ("undroppable", "keeper_round", "acquired", "note", "blocking")


def roster_entries_json(path: str | Path = "roster.yml") -> list[dict]:
    """The current roster.yml, one dict per entry. `[]` if the file doesn't
    exist yet (a fresh roster the GUI is about to create) or can't be
    parsed — same missing-is-empty contract as everything else in this repo
    that would otherwise force the GUI to special-case a brand-new setup."""
    try:
        entries = rs.load_roster_entries(path)
    except rs.RosterError:
        return []
    return [
        {
            "name": e.name,
            "undroppable": e.undroppable,
            "keeper_round": e.keeper_round,
            "acquired": e.acquired,
            "note": e.note,
            "blocking": e.blocking,
        }
        for e in entries
    ]


def write_roster_entries(path: str | Path, entries: list[dict]) -> None:
    """Write `entries` (the shape `roster_entries_json` returns) back to
    `path`. An entry with no flags set is written as a bare name string —
    matches roster.yml's own convention that a plain name is the common case
    and the mapping form exists only for players who need one."""
    players: list = []
    for e in entries:
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        extras = {k: e[k] for k in _OPTIONAL_FIELDS if e.get(k)}
        players.append(name if not extras else {"name": name, **extras})

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({"players": players}, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
