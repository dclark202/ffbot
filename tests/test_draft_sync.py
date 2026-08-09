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


class FakeLeague:
    """Stands in for yahoo_fantasy_api.League — only draft_results() is used."""

    def __init__(self):
        self.rows: list[dict] = []
        self.raise_next = False
        self.calls = 0

    def draft_results(self) -> list[dict]:
        self.calls += 1
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("simulated Yahoo timeout")
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

    def test_unmatched_yahoo_id_becomes_key_none(self):
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

    def test_already_seen_pick_number_from_a_second_batch_is_skipped(self):
        draft = _draft_state()
        p0, p1 = draft.board.players[:2]
        apply_synced_picks(draft, [SyncedPick(number=1, key=p0.key, mine=False)])
        # A second poll re-delivers pick 1 (e.g. Yahoo returned the full
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
    def test_poll_once_translates_id_map_and_team_key(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 42, "team_key": "l.t.1"}]
        sync = DraftSync(league, id_map={42: "someplayer:WR"}, my_team_key="l.t.1", poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert len(items) == 1
        assert items[0] == SyncedPick(number=1, key="someplayer:WR", mine=True)
        assert sync.status() == "live"

    def test_unmatched_player_id_yields_key_none(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 999, "team_key": "l.t.2"}]
        sync = DraftSync(league, id_map={42: "someplayer:WR"}, my_team_key="l.t.1", poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert items[0].key is None
        assert items[0].mine is False

    def test_no_my_team_key_leaves_mine_none(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 42, "team_key": "l.t.1"}]
        sync = DraftSync(league, id_map={42: "someplayer:WR"}, my_team_key=None, poll_seconds=100)
        sync._poll_once()
        items = sync.drain()
        assert items[0].mine is None

    def test_already_seen_pick_not_requeued(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 42, "team_key": "l.t.1"}]
        sync = DraftSync(league, id_map={42: "someplayer:WR"}, poll_seconds=100)
        sync._poll_once()
        sync.drain()
        sync._poll_once()  # same result again
        assert sync.drain() == []

    def test_raising_poller_sets_degraded_and_does_not_propagate(self):
        league = FakeLeague()
        league.raise_next = True
        sync = DraftSync(league, id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.status() == "degraded"

    def test_recovers_to_live_after_degraded(self):
        league = FakeLeague()
        league.raise_next = True
        sync = DraftSync(league, id_map={}, poll_seconds=100)
        sync._poll_once()
        assert sync.status() == "degraded"
        league.rows = [{"pick": 1, "player_id": 1, "team_key": "x"}]
        sync._poll_once()
        assert sync.status() == "live"

    def test_drain_is_nonblocking_and_empties_queue(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 1, "team_key": "x"}, {"pick": 2, "player_id": 2, "team_key": "y"}]
        sync = DraftSync(league, id_map={}, poll_seconds=100)
        sync._poll_once()
        first = sync.drain()
        assert len(first) == 2
        assert sync.drain() == []  # already emptied

    def test_row_missing_pick_number_ignored(self):
        league = FakeLeague()
        league.rows = [{"player_id": 1, "team_key": "x"}]  # malformed: no "pick"
        sync = DraftSync(league, id_map={}, poll_seconds=100)
        sync._poll_once()  # must not raise
        assert sync.drain() == []


class TestDraftSyncThreadLifecycle:
    def test_start_and_stop(self):
        league = FakeLeague()
        league.rows = [{"pick": 1, "player_id": 1, "team_key": "x"}]
        sync = DraftSync(league, id_map={}, poll_seconds=0.05)
        sync.start()
        time.sleep(0.2)
        sync.stop()
        assert league.calls >= 1
        assert sync.drain()  # at least the first poll's result should be queued

    def test_stop_without_start_is_safe(self):
        sync = DraftSync(FakeLeague(), id_map={}, poll_seconds=1)
        sync.stop()  # must not raise
