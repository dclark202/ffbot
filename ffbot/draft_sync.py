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


# How many consecutive "nothing changed" polls may skip the picks fetch
# before one is forced anyway. See `_poll_once` for why an unbounded skip
# is a correctness bug and not just a staleness knob.
_FORCE_FETCH_EVERY = 5


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
        # `force_poll()` runs a poll on the CALLER's thread (a GUI request
        # handler) while the daemon thread may be mid-poll of its own. Only
        # this object's own bookkeeping is at risk -- `DraftState` is never
        # touched here, so the main-thread-only invariant still holds -- but
        # interleaving two polls would let one's `_last_seen_picked_at` land
        # against the other's pick list, recreating the split-cache bug this
        # module already fixed once. One lock, one poll at a time.
        self._poll_lock = threading.Lock()
        self._status = "off"
        self._seen_picks: set[int] = set()
        self._polled_before = False
        self._last_seen_picked_at = None
        self._unmapped_count = 0
        self._skipped_fetches = 0

    @property
    def draft_id(self) -> str:
        """Which draft this sync is following — read by the entry points to
        stamp the draft log, so `--resume` can tell whose picks it holds
        (see `scripts/draft.py`'s `resume_conflict`)."""
        return self._draft_id

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
            try:
                self._poll_once()
            except Exception:
                # `_poll_once` guards its own network calls, so reaching here
                # means something unforeseen (a shape Sleeper has never
                # returned before, say). Letting it escape would kill the
                # daemon thread outright while `status()` stayed frozen on
                # its last value -- a sync that reports "live" forever and
                # never fetches again, which is indistinguishable from a
                # working one until you notice the picks stopped.
                self._status = "degraded"
            self._stop_event.wait(self._poll_seconds)

    def ensure_running(self) -> bool:
        """Restart the poll thread if it isn't alive. Returns True if a
        restart was needed.

        A daemon thread that died takes sync with it while `status()` keeps
        reporting whatever it last said -- `_run` now guards against that,
        but this is the belt to that suspenders, and the thing an explicit
        "resync now" button should be able to repair without a restart of
        the whole server mid-draft.
        """
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def force_poll(self, timeout: float = 10.0) -> bool:
        """Read Sleeper right now, ignoring every skip heuristic. Returns
        True if the read finished within `timeout`.

        Bypasses the `last_picked` short-circuit entirely -- the whole point
        is to be the escape hatch for when that heuristic, or the pick feed's
        own cache, has left us behind.

        The fetch runs on a throwaway thread that the caller merely WAITS on,
        rather than inline, so a slow Sleeper can't pin the caller for the
        60s urllib timeout underneath. That matters more than it sounds:
        `scripts/gui.py` is deliberately single-threaded, so a request
        handler blocking on the network blocks the entire draft room -- the
        one thing that must never happen at the moment someone is frantically
        hitting Refresh on the clock. On timeout the work is abandoned to the
        background, where its picks still land in the queue and get applied
        by the next request that drains it; the caller just doesn't wait.
        """
        done = threading.Event()

        def _run_forced() -> None:
            try:
                self._poll_once(force=True)
            except Exception:
                self._status = "degraded"
            finally:
                done.set()

        threading.Thread(target=_run_forced, daemon=True).start()
        return done.wait(timeout)

    def _poll_once(self, force: bool = False) -> None:
        with self._poll_lock:
            self._poll_once_locked(force)

    def _poll_once_locked(self, force: bool) -> None:
        # The `draft()` call is an OPTIMIZATION, not a dependency: its only
        # job is the `last_picked` short-circuit below. Losing it must not
        # cost us the poll, because the two endpoints genuinely fail
        # independently -- Sleeper 404s a completed mock draft's metadata
        # object while happily continuing to serve its full pick list, so
        # treating this failure as fatal killed sync outright on exactly
        # the drafts a rehearsal run cares about.
        try:
            draft = self._client.draft(self._draft_id)
        except Exception:
            draft = None

        if isinstance(draft, dict):
            last_picked = draft.get("last_picked")
            unchanged = self._polled_before and last_picked == self._last_seen_picked_at
            # The skip is an optimization and must never be able to hide a
            # pick PERMANENTLY, which is what an unbounded version does:
            # Sleeper serves this endpoint and the picks endpoint from
            # different caches, so one poll can legitimately pair a fresh
            # `last_picked` with a picks list that hasn't caught up. Store
            # the newer timestamp against the older list and the next poll
            # sees "unchanged" -- correct in isolation, except the draft has
            # now gone quiet because it is YOUR turn and everyone is waiting
            # on you. `last_picked` then never moves again, the skip never
            # lifts, and sync sits a few picks short reporting "live" at
            # exactly the moment the board needs to be right. Re-reading
            # every `_FORCE_FETCH_EVERY` polls bounds that to seconds while
            # still dropping most of the traffic on an idle draft.
            if not force and unchanged and self._skipped_fetches < _FORCE_FETCH_EVERY:
                self._skipped_fetches += 1
                self._status = "live"
                return
        else:
            # No usable metadata (fetch failed, or Sleeper returned a bare
            # `null`): fall through and read the picks every poll.
            last_picked = self._last_seen_picked_at

        try:
            results = self._client.draft_picks(self._draft_id)
        except Exception:
            self._status = "degraded"
            return

        # Picks are flowing, which is the only thing sync exists to do --
        # a missing `draft()` is invisible to the user and changes nothing
        # about correctness, so this stays "live" rather than crying
        # "degraded" for the rest of a completed mock.
        self._status = "live"
        self._polled_before = True
        self._last_seen_picked_at = last_picked
        self._skipped_fetches = 0
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
