"""Optional live sync from Yahoo's draft_results() — the last, lowest-priority
piece of the draft assistant, since Yahoo's Fantasy API cannot make picks and
API access is not guaranteed to be approved before any given draft.

Manual entry always wins. This exists purely to save keystrokes when the API
happens to be available; the assistant must be fully usable without it.

All Yahoo I/O and thread management live in `DraftSync`, confined to a daemon
thread. `DraftState` mutation never happens off the main thread — the
background thread only reads from Yahoo and pushes raw results onto a
thread-safe queue; `apply_synced_picks()` (plain, synchronous, and the part
that is actually tested against a real `DraftState`) does the applying, and
is always called from the main loop.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence

from .draft import DraftState, Pick


class _League(Protocol):
    def draft_results(self) -> list[dict]: ...


@dataclass(frozen=True)
class SyncedPick:
    number: int
    key: str | None
    mine: bool | None


class DraftSync:
    """Polls `league.draft_results()` on a daemon thread, non-blocking.

    `id_map` translates Yahoo player ids to board keys (produced by
    `scripts/draft_export.py --yahoo-players`); a pick for an id not in the
    map still advances the pick counter with `key=None`, same as a manually
    recorded "missed" pick. `my_team_key`, if given, lets sync determine
    ownership directly from Yahoo's own `team_key` field rather than the
    snake-order guess — more accurate, and it survives trades/keepers.
    """

    def __init__(
        self,
        league: _League,
        id_map: dict[int, str],
        my_team_key: str | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self._league = league
        self._id_map = id_map
        self._my_team_key = my_team_key
        self._poll_seconds = poll_seconds
        self._queue: "queue.Queue[SyncedPick]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._status = "off"
        self._seen_picks: set[int] = set()

    def status(self) -> str:
        return self._status

    def start(self) -> None:
        self._stop_event.clear()
        self._status = "live"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_seconds + 1)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(self._poll_seconds)

    def _poll_once(self) -> None:
        try:
            results = self._league.draft_results()
        except Exception:
            self._status = "degraded"
            return
        self._status = "live"
        for row in results:
            pick_no = row.get("pick")
            if pick_no is None or pick_no in self._seen_picks:
                continue
            self._seen_picks.add(pick_no)
            key = self._id_map.get(row.get("player_id"))
            mine = (row.get("team_key") == self._my_team_key) if self._my_team_key else None
            self._queue.put(SyncedPick(number=pick_no, key=key, mine=mine))

    def drain(self) -> list[SyncedPick]:
        """Pop everything currently queued. Never blocks."""
        items: list[SyncedPick] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items


def apply_synced_picks(draft: DraftState, items: Sequence[SyncedPick]) -> list[Pick]:
    """Apply queued sync results to `draft`. Manual entries always win.

    Returns every `Pick` actually appended (including any gap-fill picks),
    in order — the caller uses this both to display a message and to persist
    each one to the resume log, so `--resume` reproduces sync-derived state
    exactly, not just interactively-typed commands.

    Any pick number already recorded (manually, or by an earlier sync) is
    skipped rather than overwritten. A pick that arrives ahead of what we've
    recorded (a missed poll, a burst of fast live picks) fills the gap with
    unknown picks first, so the pick counter stays aligned with Yahoo's.
    """
    applied: list[Pick] = []
    for item in sorted(items, key=lambda s: s.number):
        if item.number <= len(draft.picks):
            continue  # already recorded -- manual (or an earlier sync) wins

        while draft.current_pick() < item.number:
            draft.record(None, mine=None, source="api")
            applied.append(draft.picks[-1])

        try:
            draft.record(item.key, mine=item.mine, source="api")
        except ValueError:
            continue  # a manual entry beat the sync to this exact pick
        applied.append(draft.picks[-1])

    return applied
