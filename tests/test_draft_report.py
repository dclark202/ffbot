from __future__ import annotations

import json

import pytest

from ffbot.board import load_board
from ffbot.config import Config, DraftConfig
from ffbot.draft import DraftState, recommend
from ffbot.draft_report import DraftReporter, build_report, capture_pick, report_path, write_report
from ffbot.draft_sync import SyncedPick, apply_synced_picks

LAYOUT = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "BN": 4}


def _board(tmp_path, cfg):
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    for pos, base in (("QB", 300.0), ("RB", 290.0), ("WR", 285.0), ("TE", 230.0)):
        for i in range(24):
            lines.append(f"{pos}{i},XXX,{pos},7,{base - 4.0 * i},{i * 4 + 1}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return load_board([path], cfg.roster_positions, 12, cfg)


def _state(board, cfg, my_slot=1, rounds=6):
    return DraftState(
        board=board, num_teams=12, my_slot=my_slot, rounds=rounds, roster_positions=LAYOUT
    )


@pytest.fixture
def setup(tmp_path):
    cfg = Config(roster_positions=LAYOUT, draft=DraftConfig(num_teams=12, gui_recommend_count=5))
    board = _board(tmp_path, cfg)
    return cfg, board


class TestCapturePick:
    def test_records_the_table_and_where_the_taken_player_sat(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        recs = recommend(state, cfg, limit=5)
        second = recs[1].player.key

        entry = capture_pick(state, cfg, recs, second)
        assert entry["pick"] == 1 and entry["round"] == 1
        assert len(entry["recommendations"]) == 5
        assert entry["taken"]["key"] == second
        assert entry["taken"]["rank_in_table"] == 2
        assert entry["taken"]["was_top_recommendation"] is False
        # The tuning signal: how much value the engine thought was given up.
        assert entry["taken"]["value_gap_to_top"] == pytest.approx(
            recs[0].value - recs[1].value
        )

    def test_taking_the_top_recommendation_reports_no_gap(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        recs = recommend(state, cfg, limit=5)
        entry = capture_pick(state, cfg, recs, recs[0].player.key)
        assert entry["taken"]["was_top_recommendation"] is True
        assert entry["taken"]["value_gap_to_top"] == pytest.approx(0.0)

    def test_a_player_outside_the_table_is_flagged_not_dropped(self, setup):
        # The engine never even offered this pick -- itself a strong signal,
        # so it must be recorded rather than silently omitted.
        cfg, board = setup
        state = _state(board, cfg)
        recs = recommend(state, cfg, limit=3)
        shown = {r.player.key for r in recs}
        outsider = next(bp.key for bp in board.players if bp.key not in shown)
        entry = capture_pick(state, cfg, recs, outsider)
        assert entry["taken"]["key"] == outsider
        assert entry["taken"]["rank_in_table"] is None
        assert entry["taken"]["value_gap_to_top"] is None

    def test_every_rec_table_column_is_present(self, setup):
        # Shared with the GUI's own serializer on purpose -- a second copy
        # would drift and make stored reports describe a table that never
        # existed.
        cfg, board = setup
        state = _state(board, cfg)
        entry = capture_pick(state, cfg, recommend(state, cfg, limit=3), None)
        row = entry["recommendations"][0]
        for key in (
            "rank", "key", "name", "position", "team", "bye_week", "proj", "vor", "need",
            "value", "adp", "survival", "upside", "volatility", "arbitrage", "scoring_edge",
            "later", "tier", "why", "why_parts", "intel_note", "flags",
        ):
            assert key in row, key

    def test_roster_before_excludes_the_pick_being_made(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        first = recommend(state, cfg, limit=1)[0].player
        state.record(first.key, mine=True)
        recs = recommend(state, cfg, limit=3)
        entry = capture_pick(state, cfg, recs, recs[0].player.key)
        keys = [r["key"] for r in entry["roster_before"]]
        assert first.key in keys
        assert recs[0].player.key not in keys


class TestBuildReport:
    def test_ownership_source_is_carried_verbatim(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        report = build_report(
            state, cfg, [], ownership_source="sleeper_roster_id", my_roster_id=7
        )
        assert report["ownership_source"] == "sleeper_roster_id"
        assert report["my_roster_id"] == 7

    def test_defaults_to_the_guess_label(self, setup):
        # A report built on inferred ownership must never read like one built
        # on Sleeper's ground truth.
        cfg, board = setup
        report = build_report(_state(board, cfg), cfg, [])
        assert report["ownership_source"] == "snake_order_guess"
        assert report["my_roster_id"] is None

    def test_stamps_the_tuning_dials_actually_in_effect(self, setup):
        cfg, board = setup
        cfg.draft.scarcity_weight = 1.0
        cfg.draft.rank_calibration_blend = 0.5
        report = build_report(_state(board, cfg), cfg, [])
        assert report["config"]["scarcity_weight"] == 1.0
        assert report["config"]["rank_calibration_blend"] == 0.5

    def test_records_board_provenance(self, setup):
        cfg, board = setup
        report = build_report(_state(board, cfg), cfg, [], board=board)
        assert report["board"]["players"] == len(board.players)
        assert report["board"]["replacement"]

    def test_final_roster_and_counts(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        for bp in board.players[:3]:
            state.record(bp.key, mine=True)
        report = build_report(state, cfg, [])
        assert len(report["final_roster"]) == 3
        assert sum(report["final_position_counts"].values()) == 3

    def test_is_json_serializable(self, setup, tmp_path):
        cfg, board = setup
        state = _state(board, cfg)
        entry = capture_pick(state, cfg, recommend(state, cfg, limit=3), None)
        report = build_report(state, cfg, [entry])
        dest = write_report(report, tmp_path / "r.json")
        assert json.loads(dest.read_text(encoding="utf-8"))["schema"] == 1


class TestDraftReporter:
    def test_captures_and_writes_on_each_pick(self, setup, tmp_path):
        cfg, board = setup
        state = _state(board, cfg)
        reporter = DraftReporter(cfg, draft_id="d1", reports_dir=tmp_path)

        for _ in range(3):
            key = recommend(state, cfg, limit=1)[0].player.key
            reporter.capture(state, key)
            state.record(key, mine=True)

        assert len(reporter.entries) == 3
        # Written incrementally: an abandoned mock still leaves data behind.
        written = json.loads(reporter.path.read_text(encoding="utf-8"))
        assert len(written["picks"]) == 3

    def test_never_double_counts_a_pick(self, setup, tmp_path):
        cfg, board = setup
        state = _state(board, cfg)
        reporter = DraftReporter(cfg, draft_id="d1", reports_dir=tmp_path)
        key = recommend(state, cfg, limit=1)[0].player.key
        reporter.capture(state, key)
        reporter.capture(state, key)  # a re-applied sync item
        assert len(reporter.entries) == 1

    def test_a_capture_failure_never_breaks_the_draft(self, setup, tmp_path, monkeypatch):
        cfg, board = setup
        state = _state(board, cfg)
        reporter = DraftReporter(cfg, draft_id="d1", reports_dir=tmp_path)
        monkeypatch.setattr(
            "ffbot.draft.recommend", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        reporter.capture(state, board.players[0].key)  # must not raise
        assert reporter.entries == []

    def test_report_path_is_stable_across_picks(self, tmp_path):
        a = report_path("draft-42", tmp_path)
        b = report_path("draft-42", tmp_path)
        assert a == b


class TestSyncCaptureHook:
    """`apply_synced_picks` fires the hook after gap-fill, so the state is
    positioned at the right pick -- the reason the hook lives there rather
    than being reimplemented by each caller."""

    def test_fires_only_for_my_picks(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        seen: list[str] = []
        items = [
            SyncedPick(number=1, key=board.players[0].key, mine=True),
            SyncedPick(number=2, key=board.players[1].key, mine=False),
        ]
        apply_synced_picks(state, items, on_my_pick=lambda d, k: seen.append(k))
        assert seen == [board.players[0].key]

    def test_fires_after_gap_fill_so_the_pick_number_is_right(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        seen: list[int] = []
        items = [SyncedPick(number=4, key=board.players[0].key, mine=True)]
        apply_synced_picks(state, items, on_my_pick=lambda d, k: seen.append(d.current_pick()))
        assert seen == [4]

    def test_unknown_ownership_falls_back_to_snake_order(self, setup):
        cfg, board = setup
        state = _state(board, cfg, my_slot=1)
        seen: list[str] = []
        # Pick 1 belongs to slot 1 arithmetically; `mine=None` means Sleeper
        # did not tell us, so the fallback should still capture it.
        items = [SyncedPick(number=1, key=board.players[0].key, mine=None)]
        apply_synced_picks(state, items, on_my_pick=lambda d, k: seen.append(k))
        assert seen == [board.players[0].key]

    def test_a_raising_hook_never_costs_a_pick(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        items = [SyncedPick(number=1, key=board.players[0].key, mine=True)]
        applied = apply_synced_picks(
            state, items, on_my_pick=lambda d, k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert len(applied) == 1
        assert state.picks[0].key == board.players[0].key

    def test_no_hook_behaves_exactly_as_before(self, setup):
        cfg, board = setup
        state = _state(board, cfg)
        items = [SyncedPick(number=2, key=board.players[0].key, mine=True)]
        applied = apply_synced_picks(state, items)
        assert [p.key for p in applied] == [None, board.players[0].key]


class TestLogSegmentation:
    """A log file is append-only ACROSS drafts: an abandoned mock and a
    restart leave their picks in the same file (a real one here held seven
    sessions). Replaying that as one continuous draft is not a small error
    -- pick numbers run past the final round, players repeat, and ownership
    scrambles, yielding a report that looks plausible and describes nothing
    that happened.
    """

    def _write(self, tmp_path, lines):
        p = tmp_path / "log.jsonl"
        p.write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")
        return p

    def test_a_pick_number_reset_starts_a_new_session(self, tmp_path):
        from scripts.draft_report import _read_log

        p = self._write(tmp_path, [
            {"sync": {"number": 1, "key": "a:RB", "mine": True}},
            {"sync": {"number": 2, "key": "b:WR", "mine": False}},
            {"sync": {"number": 1, "key": "c:QB", "mine": True}},
        ])
        segments = _read_log(p)
        assert len(segments) == 2
        assert [len(s["picks"]) for s in segments] == [2, 1]

    def test_a_draft_id_line_starts_a_new_session(self, tmp_path):
        from scripts.draft_report import _read_log

        p = self._write(tmp_path, [
            {"sync": {"number": 1, "key": "a:RB", "mine": True}},
            {"draft_id": "D2"},
            {"sync": {"number": 1, "key": "b:WR", "mine": True}},
        ])
        segments = _read_log(p)
        assert len(segments) == 2
        assert segments[1]["draft_id"] == "D2"

    def test_a_leading_draft_id_labels_the_first_session(self, tmp_path):
        from scripts.draft_report import _read_log

        p = self._write(tmp_path, [
            {"draft_id": "D1"},
            {"sync": {"number": 1, "key": "a:RB", "mine": True}},
        ])
        segments = _read_log(p)
        assert len(segments) == 1
        assert segments[0]["draft_id"] == "D1"

    def test_a_single_session_log_is_one_segment(self, tmp_path):
        from scripts.draft_report import _read_log

        p = self._write(tmp_path, [
            {"sync": {"number": n, "key": f"p{n}:RB", "mine": n == 1}} for n in range(1, 6)
        ])
        assert len(_read_log(p)) == 1

    def test_no_empty_segments_are_emitted(self, tmp_path):
        from scripts.draft_report import _read_log

        p = self._write(tmp_path, [
            {"draft_id": "D1"}, {"draft_id": "D2"},
            {"sync": {"number": 1, "key": "a:RB", "mine": True}},
        ])
        segments = _read_log(p)
        assert all(s["picks"] for s in segments)

    def test_an_empty_log_yields_nothing(self, tmp_path):
        from scripts.draft_report import _read_log

        assert _read_log(self._write(tmp_path, [])) == []
