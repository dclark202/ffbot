"""Optional live sync from Sleeper's draft picks — a real convenience now
that Sleeper's API needs no auth at all, unlike the Yahoo equivalent this
module used to poll (Yahoo could never make picks either, and API approval
was never guaranteed in time for a real draft — see git history for that
version). Manual entry still always wins; this exists purely to save
keystrokes when a live draft is actually happening on Sleeper.

All Sleeper I/O and thread management live in `DraftSync`, confined to a
daemon thread. `DraftState` mutation never happens off the main thread — the
background thread only reads from Sleeper and pushes raw results onto a
thread-safe queue; `apply_synced_picks()` (synchronous, main-thread) does the
actual `DraftState` mutation — `DraftState` must never be touched off the
main thread.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence

from .draft import DraftState, Pick


class _DraftSource(Protocol):
    def draft(self, draft_id: str) -> dict: ...
    def draft_picks(self, draft_id: str) -> list[dict]: ...


@dataclass(frozen=True)
class SyncedPick:
    number: int
    key: str | None
    mine: bool | None


class DraftSync:
    """Polls Sleeper's draft on a daemon thread, non-blocking.

    Two-step poll, matching `ffbot.sleeper.client.SleeperClient`'s own
    comment on why `draft()` is never cached: check the cheap `draft()` call
    for a changed `last_picked` timestamp first, and only fetch the full
    (and larger) `draft_picks()` list when something actually moved — a live
    draft otherwise means polling every few seconds for the entire pick log,
    most of which never changes between polls.

    `id_map` translates Sleeper player ids to board keys (produced by
    `scripts/draft_export.py --reconcile`); a pick for an id not in the map
    still advances the pick counter with `key=None`, same as a manually
    recorded "missed" pick. `my_roster_id`, if given, lets sync determine
    ownership directly from Sleeper's own `roster_id` field rather than the
    snake-order guess — more accurate, and it survives trades/keepers.
    """

    def __init__(
        self,
        client: _DraftSource,
        draft_id: str,
        id_map: dict[str, str],
        my_roster_id: int | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self._draft_id = draft_id
        self._id_map = id_map
        self._my_roster_id = my_roster_id
        self._poll_seconds = poll_seconds
        self._queue: "queue.Queue[SyncedPick]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._status = "off"
        self._seen_picks: set[int] = set()
        self._polled_before = False
        self._last_seen_picked_at = None
        self._unmapped_count = 0

    def status(self) -> str:
        return self._status

    def unmapped_count(self) -> int:
        """How many synced picks so far had a Sleeper `player_id` missing
        from `id_map` -- a real reconciliation gap, not a transport error.
        Such a pick still advances the pick counter with `key=None` (see
        `SyncedPick`), which means it silently stays recommendable unless
        something surfaces the miss -- this counter is that something."""
        return self._unmapped_count

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
            draft = self._client.draft(self._draft_id)
        except Exception:
            self._status = "degraded"
            return
        last_picked = draft.get("last_picked")
        if self._polled_before and last_picked == self._last_seen_picked_at:
            self._status = "live"  # nothing new -- skip the heavier picks fetch
            return

        try:
            results = self._client.draft_picks(self._draft_id)
        except Exception:
            self._status = "degraded"
            return

        self._status = "live"
        self._polled_before = True
        self._last_seen_picked_at = last_picked
        for row in results:
            pick_no = row.get("pick_no")
            if pick_no is None or pick_no in self._seen_picks:
                continue
            self._seen_picks.add(pick_no)
            key = self._id_map.get(row.get("player_id"))
            if key is None and row.get("player_id") is not None:
                self._unmapped_count += 1
            mine = (row.get("roster_id") == self._my_roster_id) if self._my_roster_id is not None else None
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
    unknown picks first, so the pick counter stays aligned with Sleeper's.
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
