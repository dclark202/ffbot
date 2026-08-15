from __future__ import annotations

import time

import pytest

from ffbot.board import Board, BoardPlayer
from ffbot.draft import DraftState
from ffbot.draft_sync import DraftSync, SyncedPick, apply_synced_picks
from tests.conftest import mk_bp

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


def _bp(name: str, position: str) -> BoardPlayer:
    return mk_bp(name, position, team="XXX", rank=1)


def _draft_state() -> DraftState:
    players = [_bp(f"P{i}", "WR") for i in range(20)]
    board = Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})
    return DraftState(board=board, num_teams=12, my_slot=4, rounds=15, roster_positions=STANDARD_LAYOUT)


class FakeSleeperClient:
    """Stands in for `ffbot.sleeper.client.SleeperClient` — only `draft()`
    and `draft_picks()` are used. `last_picked` must be bumped by the test
    whenever `rows` changes, mirroring how a real draft's `last_picked`
    timestamp actually moves — `DraftSync` uses it to skip the heavier
    `draft_picks()` fetch when nothing changed.
    """

    def __init__(self):
        self.rows: list[dict] = []
        self.last_picked = 0
        self.raise_next = False
        self.raise_next_on_draft_picks = False
        self.draft_returns_none = False
        self.calls = 0
        self.draft_picks_calls = 0

    def draft(self, draft_id: str) -> dict:
        self.calls += 1
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("simulated Sleeper timeout")
        if self.draft_returns_none:
            return None  # Sleeper hands back a bare `null` for some reads
        return {"draft_id": draft_id, "last_picked": self.last_picked}

    def draft_picks(self, draft_id: str) -> list[dict]:
        self.draft_picks_calls += 1
        if self.raise_next_on_draft_picks:
            self.raise_next_on_draft_picks = False
            raise RuntimeError("simulated Sleeper timeout")
        return list(self.rows)


class TestApplySyncedPicks:
    def test_basic_pick_applied(self):
        draft = _draft_state()
        p0 = draft.board.players[0]
        applied = apply_synced_picks(draft, [SyncedPick(number=1, key=p0.key, mine=False)])
        assert len(applied) == 1
        assert applied[0].key == p0.key
        assert draft.current_pick() == 2

    def test_manual_wins_on_conflict(self):
        draft = _draft_state()
        p0, p1 = draft.board.players[0], draft.board.players[1]
        draft.record(p0.key, mine=True)  # manual pick 1
        applied = apply_synced_picks(draft, [SyncedPick(number=1, key=p1.key, mine=False)])
        assert applied == []  # sync for an already-recorded pick number is dropped
        assert draft.picks[0].key == p0.key  # manual entry untouched
        assert p1.key not in draft.taken_keys()

    def test_out_of_order_arrival_applied_in_pick_order(self):
        draft = _draft_state()
        p0, p1, p2 = draft.board.players[:3]
        items = [
            SyncedPick(number=3, key=p2.key, mine=False),
            SyncedPick(number=1, key=p0.key, mine=False),
            SyncedPick(number=2, key=p1.key, mine=False),
        ]
        applied = apply_synced_picks(draft, items)
        assert [p.number for p in applied] == [1, 2, 3]
        assert [p.key for p in draft.picks] == [p0.key, p1.key, p2.key]

    def test_unmatched_player_id_becomes_key_none(self):
        draft = _draft_state()
        applied = apply_synced_picks(draft, [SyncedPick(number=1, key=None, mine=False)])
        assert applied[0].key is None
        assert draft.current_pick() == 2

    def test_gap_fill_with_unknown_picks(self):
        draft = _draft_state()
        p4 = draft.board.players[4]
        applied = apply_synced_picks(draft, [SyncedPick(number=5, key=p4.key, mine=False)])
        assert len(applied) == 5  # 4 gap-fill unknowns + the real pick
        assert [p.key for p in applied[:4]] == [None, None, None, None]
        assert applied[4].key == p4.key
        assert draft.current_pick() == 6

    def test_mine_flag_propagated(self):
        draft = _draft_state()
        p0 = draft.board.players[0]
        applied = apply_synced_picks(draft, [SyncedPick(number=1, key=p0.key, mine=True)])
        assert applied[0].mine is True
        assert p0.key in {bp.key for bp in draft.my_roster()}

    def test_mine_none_falls_back_to_snake_order_inference(self):
        # DraftSync queues mine=None whenever it has no my_roster_id to
        # compare against -- true for a foreign/mock draft (see
        # scripts/draft.py's _build_sync). DraftState.record already
        # auto-infers ownership from the pick number in that case
        # (mine=None -> `number in self.my_picks()`), which is exact for a
        # mock: no trades, so pick number -> slot -> owner always lines up.
        draft = _draft_state()  # my_slot=4, num_teams=12 -- pick 4 is mine
        p3 = draft.board.players[3]
        applied = apply_synced_picks(draft, [SyncedPick(number=4, key=p3.key, mine=None)])
        # picks 1-3 are gap-filled as unknown (also mine=None -> inferred
        # False for slot 4); the real pick 4 is the last one applied.
        assert applied[-1].key == p3.key
        assert applied[-1].mine is True
        assert p3.key in {bp.key for bp in draft.my_roster()}

    def test_already_seen_pick_number_from_a_second_batch_is_skipped(self):
        draft = _draft_state()
        p0, p1 = draft.board.players[:2]
        apply_synced_picks(draft, [SyncedPick(number=1, key=p0.key, mine=False)])
        # A second poll re-delivers pick 1 (e.g. Sleeper returned the full
        # history again) alongside a genuinely new pick 2.
        applied = apply_synced_picks(
            draft,
            [SyncedPick(number=1, key=p0.key, mine=False), SyncedPick(number=2, key=p1.key, mine=False)],
        )
        assert [p.number for p in applied] == [2]
        assert draft.current_pick() == 3

    def test_empty_items_noop(self):
        draft = _draft_state()
        applied = apply_synced_picks(draft, [])
        assert applied == []
        assert draft.current_pick() == 1


class TestDraftSyncPolling:
    def test_poll_once_translates_id_map_and_roster_id(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert len(items) == 1
        assert items[0] == SyncedPick(number=1, key="someplayer:WR", mine=True)
        assert sync.status() == "live"

    def test_unmatched_player_id_yields_key_none(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "999", "roster_id": 2}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert items[0].key is None
        assert items[0].mine is False

    def test_unmapped_count_starts_at_zero(self):
        sync = DraftSync(FakeSleeperClient(), "D1", id_map={}, poll_seconds=100)
        assert sync.unmapped_count() == 0

    def test_unmatched_player_id_increments_unmapped_count(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "999", "roster_id": 2}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        assert sync.unmapped_count() == 1

    def test_matched_player_id_does_not_increment_unmapped_count(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        assert sync.unmapped_count() == 0

    def test_unmapped_count_accumulates_across_polls(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "999", "roster_id": 2}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        client.rows = [
            {"pick_no": 1, "player_id": "999", "roster_id": 2},
            {"pick_no": 2, "player_id": "888", "roster_id": 3},
        ]
        client.last_picked = 2
        sync._poll_once()
        assert sync.unmapped_count() == 2

    def test_no_my_roster_id_leaves_mine_none(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=None, poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert items[0].mine is None

    def test_unchanged_last_picked_skips_the_picks_fetch_entirely(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, poll_seconds=100)
        sync._poll_once()
        sync.drain()
        sync._poll_once()  # last_picked hasn't moved -- draft_picks() must not be called again
        assert sync.drain() == []
        assert client.draft_picks_calls == 1

    def test_changed_last_picked_triggers_a_refetch(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, poll_seconds=100)
        sync._poll_once()
        sync.drain()
        client.rows.append({"pick_no": 2, "player_id": "43", "roster_id": 2})
        client.last_picked = 2
        sync._poll_once()
        assert [p.number for p in sync.drain()] == [2]
        assert client.draft_picks_calls == 2

    def test_already_seen_pick_not_requeued_even_when_refetched(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, poll_seconds=100)
        sync._poll_once()
        sync.drain()
        client.last_picked = 2  # force a refetch even though rows are unchanged
        sync._poll_once()
        assert sync.drain() == []

    def test_draft_call_failing_still_syncs_picks(self):
        # Sleeper 404s a completed mock draft's metadata object while still
        # serving its picks -- `draft()` is only the `last_picked`
        # short-circuit, so losing it must not cost us the pick feed. This
        # used to abort the poll outright, which killed sync on exactly the
        # drafts a rehearsal run is pointed at.
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.raise_next = True  # only draft() raises; draft_picks() is fine
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert [i.number for i in items] == [1]
        assert items[0].key == "someplayer:WR"
        # Picks are flowing, which is all sync exists to do -- a missing
        # draft() is invisible to the user and changes nothing.
        assert sync.status() == "live"

    def test_draft_call_failing_refetches_picks_every_poll(self):
        # With no `last_picked` to compare against, the short-circuit can't
        # apply -- each poll must go straight to the picks instead of
        # wrongly concluding "nothing changed".
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        sync = DraftSync(client, "D1", id_map={}, my_roster_id=1, poll_seconds=100)
        client.raise_next = True
        sync._poll_once()
        client.raise_next = True
        client.rows.append({"pick_no": 2, "player_id": "43", "roster_id": 2})
        sync._poll_once()
        assert [i.number for i in sync.drain()] == [1, 2]

    def test_unchanged_last_picked_eventually_refetches_anyway(self):
        # The regression this exists for: Sleeper serves draft() and
        # draft_picks() from different caches, so a poll can pair a fresh
        # `last_picked` with a picks list that hasn't caught up. If the
        # draft then goes quiet (it's YOUR turn), `last_picked` never moves
        # again and an unbounded skip strands sync a few picks short --
        # reporting "live" -- for the rest of the draft.
        from ffbot.draft_sync import _FORCE_FETCH_EVERY

        client = FakeSleeperClient()
        client.last_picked = 1
        client.rows = [{"pick_no": 1, "player_id": "1", "roster_id": 1}]
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()
        assert sync.drain()  # first poll reads picks
        calls_after_first = client.draft_picks_calls

        # A pick lands but `last_picked` is stuck at its stale value -- the
        # exact split-cache case. The skip must lift on its own.
        client.rows.append({"pick_no": 2, "player_id": "2", "roster_id": 2})
        for _ in range(_FORCE_FETCH_EVERY):
            sync._poll_once()
            assert client.draft_picks_calls == calls_after_first  # still skipping
        sync._poll_once()  # the forced read
        assert client.draft_picks_calls == calls_after_first + 1
        assert [i.number for i in sync.drain()] == [2]

    def test_skip_counter_resets_after_a_real_fetch(self):
        from ffbot.draft_sync import _FORCE_FETCH_EVERY

        client = FakeSleeperClient()
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()
        for _ in range(_FORCE_FETCH_EVERY):
            sync._poll_once()
        client.last_picked = 2  # a genuine change forces a read and resets
        sync._poll_once()
        before = client.draft_picks_calls
        # Budget must be full again, not exhausted.
        for _ in range(_FORCE_FETCH_EVERY):
            sync._poll_once()
        assert client.draft_picks_calls == before

    def test_null_draft_metadata_does_not_kill_the_poll(self):
        # Sleeper hands back a bare `null` for some draft reads; `.get` on
        # that used to raise straight out of the daemon thread, killing sync
        # while status() stayed frozen on "live".
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "42", "roster_id": 1}]
        client.draft_returns_none = True
        sync = DraftSync(client, "D1", id_map={"42": "someplayer:WR"}, my_roster_id=1, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert [i.key for i in sync.drain()] == ["someplayer:WR"]

    def test_unexpected_error_degrades_instead_of_killing_the_thread(self):
        # A poll that blows up in an unforeseen way must not escape `_run`
        # -- a dead daemon thread leaves status() frozen on its last value,
        # so the UI reports a healthy sync that will never fetch again.
        class ExplodingClient:
            def draft(self, draft_id):
                raise AssertionError("not a network error -- an unforeseen shape")

            def draft_picks(self, draft_id):
                raise AssertionError("same")

        sync = DraftSync(ExplodingClient(), "D1", id_map={}, poll_seconds=0.01)
        sync.start()
        try:
            time.sleep(0.1)
            assert sync._thread is not None and sync._thread.is_alive()
            assert sync.status() == "degraded"
        finally:
            sync.stop()

    def test_force_poll_bypasses_the_skip_entirely(self):
        # The escape hatch behind the GUI's Refresh button: no matter how
        # many skips are budgeted, a forced poll reads Sleeper now.
        client = FakeSleeperClient()
        client.last_picked = 1
        client.rows = [{"pick_no": 1, "player_id": "1", "roster_id": 1}]
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()
        sync.drain()
        calls = client.draft_picks_calls

        client.rows.append({"pick_no": 2, "player_id": "2", "roster_id": 2})
        sync._poll_once()  # would skip -- last_picked unchanged
        assert client.draft_picks_calls == calls
        assert sync.force_poll() is True
        assert client.draft_picks_calls == calls + 1
        assert [i.number for i in sync.drain()] == [2]

    def test_force_poll_reports_false_when_it_outruns_the_timeout(self):
        # A slow Sleeper must not pin the caller -- gui.py is single-threaded,
        # so a blocked request handler blocks the whole draft room.
        class SlowClient:
            def draft(self, draft_id):
                time.sleep(5)
                return {"last_picked": 1}

            def draft_picks(self, draft_id):
                return []

        sync = DraftSync(SlowClient(), "D1", id_map={}, poll_seconds=100)
        started = time.monotonic()
        assert sync.force_poll(timeout=0.2) is False
        assert time.monotonic() - started < 2  # returned promptly, didn't wait out the fetch

    def test_ensure_running_restarts_a_dead_thread(self):
        client = FakeSleeperClient()
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync.start()
        try:
            assert sync.ensure_running() is False  # already alive
            sync.stop()
            assert sync.ensure_running() is True  # revived
            assert sync._thread is not None and sync._thread.is_alive()
        finally:
            sync.stop()

    def test_ensure_running_starts_a_never_started_sync(self):
        sync = DraftSync(FakeSleeperClient(), "D1", id_map={}, poll_seconds=100)
        try:
            assert sync.ensure_running() is True
            assert sync._thread is not None and sync._thread.is_alive()
        finally:
            sync.stop()

    def test_raising_on_draft_call_alone_does_not_propagate_or_degrade(self):
        # `draft()` failing on its own is NOT a degraded sync -- the pick
        # feed is the thing that matters and it's still readable. Degraded
        # is reserved for losing draft_picks() (below), the failure that
        # actually stops picks arriving.
        client = FakeSleeperClient()
        client.raise_next = True
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.status() == "live"

    def test_raising_on_draft_picks_call_sets_degraded_and_does_not_propagate(self):
        client = FakeSleeperClient()
        client.last_picked = 1
        client.raise_next_on_draft_picks = True
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.status() == "degraded"

    def test_both_endpoints_failing_sets_degraded(self):
        client = FakeSleeperClient()
        client.raise_next = True
        client.raise_next_on_draft_picks = True
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.status() == "degraded"

    def test_recovers_to_live_after_degraded(self):
        client = FakeSleeperClient()
        client.last_picked = 1
        client.raise_next_on_draft_picks = True
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()
        assert sync.status() == "degraded"
        client.rows = [{"pick_no": 1, "player_id": "1", "roster_id": 1}]
        client.last_picked = 2
        sync._poll_once()
        assert sync.status() == "live"

    def test_drain_is_nonblocking_and_empties_queue(self):
        client = FakeSleeperClient()
        client.rows = [
            {"pick_no": 1, "player_id": "1", "roster_id": 1},
            {"pick_no": 2, "player_id": "2", "roster_id": 2},
        ]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()
        first = sync.drain()
        assert len(first) == 2
        assert sync.drain() == []  # already emptied

    def test_row_missing_pick_number_ignored(self):
        client = FakeSleeperClient()
        client.rows = [{"player_id": "1", "roster_id": 1}]  # malformed: no "pick_no"
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.drain() == []


class TestDraftSyncThreadLifecycle:
    def test_start_and_stop(self):
        client = FakeSleeperClient()
        client.rows = [{"pick_no": 1, "player_id": "1", "roster_id": 1}]
        client.last_picked = 1
        sync = DraftSync(client, "D1", id_map={}, poll_seconds=0.05)
        sync.start()
        time.sleep(0.2)
        sync.stop()
        assert client.calls >= 1
        assert sync.drain()  # at least the first poll's result should be queued

    def test_stop_without_start_is_safe(self):
        sync = DraftSync(FakeSleeperClient(), "D1", id_map={}, poll_seconds=1)
        sync.stop()  # must not raise
