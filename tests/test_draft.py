from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from ffbot.board import BoardPlayer, load_board
from ffbot.config import Config, DraftConfig
from ffbot.draft import (
    DraftState,
    alerts,
    my_pick_numbers,
    needs_between,
    need,
    pick_number,
    picks_until,
    recommend,
    round_and_slot,
    sigma_for,
    survival,
    team_slot_at,
    value,
    _season_score,
)


class TestSnakeOrder:
    def test_round_trip_exhaustive(self):
        for num_teams in range(1, 17):
            for round_ in range(1, 21):
                for slot in range(1, num_teams + 1):
                    pick = pick_number(round_, slot, num_teams)
                    assert round_and_slot(pick, num_teams) == (round_, slot)

    def test_bijection_onto_range(self):
        for num_teams in (1, 2, 8, 12):
            rounds = 15
            seen = set()
            for round_ in range(1, rounds + 1):
                for slot in range(1, num_teams + 1):
                    pick = pick_number(round_, slot, num_teams)
                    assert pick not in seen
                    seen.add(pick)
            assert seen == set(range(1, num_teams * rounds + 1))

    def test_single_team_league(self):
        assert pick_number(1, 1, 1) == 1
        assert pick_number(5, 1, 1) == 5
        assert round_and_slot(5, 1) == (5, 1)

    def test_slot_one_never_reverses(self):
        # Slot 1 gets pick 1 in round 1 (odd) and the last pick of round 2
        # (even) — i.e. it "turns" at the end of every even round.
        num_teams = 10
        assert pick_number(1, 1, num_teams) == 1
        assert pick_number(2, 1, num_teams) == 20  # last pick of round 2

    def test_last_slot_turns_back_to_back(self):
        # The team at slot T picks last in an odd round and first in the
        # next (even) round — two picks in a row.
        num_teams = 12
        p1 = pick_number(1, num_teams, num_teams)
        p2 = pick_number(2, num_teams, num_teams)
        assert p2 == p1 + 1

    def test_consecutive_gap_alternates(self):
        num_teams = 8
        for s in range(1, num_teams):
            gap_odd_to_even = pick_number(2, s, num_teams) - pick_number(1, s, num_teams)
            assert gap_odd_to_even == 2 * (num_teams - s) + 1
        for s in range(1, num_teams):
            gap_even_to_odd = pick_number(3, s, num_teams) - pick_number(2, s, num_teams)
            assert gap_even_to_odd == 2 * s - 1

    def test_invalid_slot_raises(self):
        with pytest.raises(ValueError):
            pick_number(1, 0, 10)
        with pytest.raises(ValueError):
            pick_number(1, 11, 10)

    def test_invalid_round_raises(self):
        with pytest.raises(ValueError):
            pick_number(0, 1, 10)

    def test_invalid_pick_raises(self):
        with pytest.raises(ValueError):
            round_and_slot(0, 10)

    def test_team_slot_at(self):
        assert team_slot_at(1, 12) == 1
        assert team_slot_at(12, 12) == 12
        assert team_slot_at(13, 12) == 12  # round 2 reverses
        assert team_slot_at(24, 12) == 1


class TestMyPickNumbers:
    def test_matches_pick_number(self):
        num_teams, rounds, slot = 10, 6, 4
        expected = [pick_number(r, slot, num_teams) for r in range(1, rounds + 1)]
        assert my_pick_numbers(slot, num_teams, rounds) == expected

    def test_length(self):
        assert len(my_pick_numbers(1, 12, 15)) == 15


class TestPicksUntil:
    def test_on_the_clock_now(self):
        assert picks_until(5, [5, 20, 30]) == 0

    def test_counts_forward(self):
        assert picks_until(3, [5, 20, 30]) == 2

    def test_ignores_past_picks(self):
        assert picks_until(21, [5, 20, 30]) == 9

    def test_no_more_picks_returns_none(self):
        assert picks_until(31, [5, 20, 30]) is None

    def test_my_picks_override_wins(self):
        # A keeper/traded-pick override list need not follow the arithmetic
        # snake progression at all — picks_until must just use it verbatim.
        weird = [1, 2, 40]
        assert picks_until(3, weird) == 37


class TestSurvival:
    def test_at_from_pick_is_certain(self):
        assert survival(adp=50, sigma=10, from_pick=10, to_pick=10) == 1.0

    def test_before_from_pick_is_certain(self):
        assert survival(adp=50, sigma=10, from_pick=10, to_pick=5) == 1.0

    def test_monotone_decreasing_in_to_pick(self):
        vals = [survival(adp=50, sigma=10, from_pick=1, to_pick=k) for k in range(1, 100)]
        assert all(a >= b for a, b in zip(vals, vals[1:]))

    def test_in_unit_interval(self):
        for to_pick in (1, 10, 50, 100, 500):
            s = survival(adp=50, sigma=10, from_pick=1, to_pick=to_pick)
            assert 0.0 <= s <= 1.0

    def test_later_adp_survives_longer(self):
        early = survival(adp=10, sigma=5, from_pick=1, to_pick=20)
        late = survival(adp=80, sigma=5, from_pick=1, to_pick=20)
        assert late > early

    def test_tighter_sigma_sharper_dropoff(self):
        loose = survival(adp=50, sigma=30, from_pick=1, to_pick=51)
        tight = survival(adp=50, sigma=5, from_pick=1, to_pick=51)
        assert tight < loose

    def test_degenerate_denominator_returns_zero(self):
        # ADP says this player should be long gone by from_pick; conditioning
        # on that near-impossible event must not raise or return >1.
        s = survival(adp=1, sigma=0.01, from_pick=200, to_pick=210)
        assert s == 0.0


class TestSigmaFor:
    def test_uses_csv_stdev_when_present(self):
        cfg = DraftConfig()
        assert sigma_for(adp=50, stdev=12.0, cfg=cfg) == 12.0

    def test_ignores_nonpositive_stdev(self):
        cfg = DraftConfig()
        assert sigma_for(adp=50, stdev=0.0, cfg=cfg) != 0.0
        assert sigma_for(adp=50, stdev=-1.0, cfg=cfg) != -1.0

    def test_derives_when_absent(self):
        cfg = DraftConfig(adp_sigma_floor=6.0, adp_sigma_scale=0.22)
        assert sigma_for(adp=100, stdev=None, cfg=cfg) == pytest.approx(22.0)
        assert sigma_for(adp=1, stdev=None, cfg=cfg) == pytest.approx(6.0)  # floor


# --- Shared board-building helpers for DraftState / valuation / alerts -----

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


def _random_rows(rng: random.Random, counts: dict[str, int]) -> list[dict]:
    rows = []
    for pos, n in counts.items():
        for i in range(n):
            rows.append(
                {
                    "name": f"{pos}{i}",
                    "team": "XXX",
                    "position": pos,
                    "bye": rng.randint(5, 14),
                    "points": round(rng.uniform(20, 340), 1),
                    "adp": round(rng.uniform(1, 250), 1),
                }
            )
    return rows


def _write_board_csv(tmp_path, rows: list[dict]):
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    for r in rows:
        lines.append(f"{r['name']},{r['team']},{r['position']},{r['bye']},{r['points']},{r['adp']}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _build_board(tmp_path, rng: random.Random, counts: dict[str, int], num_teams: int = 12, cfg=None):
    cfg = cfg or Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=num_teams))
    rows = _random_rows(rng, counts)
    path = _write_board_csv(tmp_path, rows)
    # Board and cfg must agree on the layout -- need()/value() read
    # cfg.roster_positions, and a mismatch here silently produces a board
    # valued against a different league shape than the one being tested.
    board = load_board([path], cfg.roster_positions, num_teams, cfg)
    return board, cfg


class TestDraftState:
    def _state(self, tmp_path):
        rng = random.Random(5)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 30, "WR": 40, "TE": 15, "K": 10, "DEF": 10})
        state = DraftState(board=board, num_teams=12, my_slot=3, rounds=15, roster_positions=STANDARD_LAYOUT)
        return state, cfg, board

    def test_record_infers_mine(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        my_picks = state.my_picks()
        for _ in range(my_picks[0] - 1):
            key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
            p = state.record(key)
            assert p.mine is False
        key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
        p = state.record(key)
        assert p.mine is True

    def test_force_flags_override_inference(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
        # pick 1 is not mine by the snake progression (my_slot=3) — force it.
        p = state.record(key, mine=True)
        assert p.mine is True

    def test_undo_restores_prior_state(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        for _ in range(5):
            key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
            state.record(key)
        before_taken = state.taken_keys()
        before_current = state.current_pick()
        before_roster = [bp.key for bp in state.my_roster()]

        key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
        state.record(key, mine=True)
        state.undo()

        assert state.taken_keys() == before_taken
        assert state.current_pick() == before_current
        assert [bp.key for bp in state.my_roster()] == before_roster

    def test_undo_on_empty_returns_none(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        assert state.undo() is None
        assert state.current_pick() == 1

    def test_duplicate_pick_rejected(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        key = board.players[0].key
        state.record(key)
        with pytest.raises(ValueError):
            state.record(key)
        assert state.current_pick() == 2  # unchanged: only the first record() applied

    def test_missed_pick_advances_without_key(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        state.record(None)
        assert state.current_pick() == 2
        assert state.undo().key is None

    def test_full_draft_ends_with_expected_roster_size(self, tmp_path):
        rng = random.Random(11)
        board, cfg = _build_board(tmp_path, rng, {"QB": 20, "RB": 50, "WR": 60, "TE": 25, "K": 20, "DEF": 20})
        num_teams, rounds = 12, 15
        state = DraftState(board=board, num_teams=num_teams, my_slot=6, rounds=rounds, roster_positions=STANDARD_LAYOUT)
        available = list(board.players)
        for _ in range(num_teams * rounds):
            key = available.pop(0).key
            state.record(key)
        assert state.current_pick() == num_teams * rounds + 1
        assert len(state.my_roster()) == rounds
        assert len(state.picks) == num_teams * rounds


class TestNeedValueProperties:
    def test_need_equals_vor_on_empty_roster(self, tmp_path):
        rng = random.Random(20260808)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 30, "WR": 40, "TE": 15, "K": 10, "DEF": 10})
        checked = 0
        for bp in board.players[:60]:
            if bp.position not in board.replacement:
                continue
            got = need(bp, [], board, cfg)
            assert got == pytest.approx(bp.vor, abs=1e-6)
            checked += 1
        assert checked > 0

    def test_need_zero_when_no_accepting_slot(self, tmp_path):
        rng = random.Random(3)
        layout_no_k = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "DEF": 1, "BN": 6}
        cfg = Config(roster_positions=layout_no_k, draft=DraftConfig(num_teams=12))
        board, _ = _build_board(tmp_path, rng, {"QB": 5, "RB": 10, "WR": 10, "TE": 5, "K": 5, "DEF": 5}, cfg=cfg)
        kickers = [bp for bp in board.players if bp.position == "K"]
        assert kickers
        # need() is the live, roster-aware computation: with no slot able to
        # ever start a K, adding one (or the synthetic replacement) never
        # changes the optimizer's total, so need is exactly 0 for every
        # kicker -- not just approximately, and not equal to vor, which is
        # a separate static number fixed at board-build time.
        for bp in kickers:
            assert need(bp, [], board, cfg) == 0.0
            assert bp.vor <= 1e-6  # board-level vor still sinks correctly
        best_kicker = max(kickers, key=lambda bp: bp.points)
        assert best_kicker.vor == pytest.approx(0.0, abs=1e-6)

    def test_marginal_nonnegative(self, tmp_path):
        rng = random.Random(42)
        board, cfg = _build_board(tmp_path, rng, {"QB": 8, "RB": 20, "WR": 25, "TE": 10, "K": 6, "DEF": 6})
        keys = [bp.key for bp in board.players]
        rng2 = random.Random(43)
        base_roster = rng2.sample(keys, 6)
        base = _season_score(board, base_roster, None, cfg)
        for bp in board.players[:80]:
            if bp.key in base_roster:
                continue
            marginal = _season_score(board, base_roster, bp, cfg) - base
            assert marginal >= -1e-9

    def test_submodularity(self, tmp_path):
        # marginal(X | R u {Y}) <= marginal(X | R): a real theorem for
        # max-weight independent sets in a matroid, so a violation here
        # indicts the optimizer, not this test.
        rng = random.Random(7)
        board, cfg = _build_board(tmp_path, rng, {"QB": 8, "RB": 20, "WR": 25, "TE": 10, "K": 6, "DEF": 6})
        keys = [bp.key for bp in board.players]
        rng2 = random.Random(8)
        remaining = list(keys)
        rng2.shuffle(remaining)
        r_keys = remaining[:5]
        remaining = [k for k in remaining if k not in r_keys]
        y_key = remaining[0]
        remaining = remaining[1:]

        base_r = _season_score(board, r_keys, None, cfg)
        base_ry = _season_score(board, r_keys + [y_key], None, cfg)

        for x_key in remaining[:60]:
            x = board.by_key[x_key]
            marg_r = _season_score(board, r_keys, x, cfg) - base_r
            marg_ry = _season_score(board, r_keys + [y_key], x, cfg) - base_ry
            assert marg_ry <= marg_r + 1e-9

    def test_empty_roster_value_ranking_matches_vor_ranking(self, tmp_path):
        rng = random.Random(100)
        board, cfg = _build_board(tmp_path, rng, {"QB": 8, "RB": 20, "WR": 25, "TE": 10, "K": 6, "DEF": 6})
        candidates = board.players[:40]
        by_value = sorted(candidates, key=lambda bp: -value(bp, [], board, cfg))
        by_vor = sorted(candidates, key=lambda bp: -bp.vor)
        assert [bp.key for bp in by_value] == [bp.key for bp in by_vor]

    def test_saturated_roster_value_ranking_matches_vor_ranking(self, tmp_path):
        rng = random.Random(101)
        board, cfg = _build_board(tmp_path, rng, {"QB": 8, "RB": 20, "WR": 25, "TE": 10, "K": 6, "DEF": 6})
        # Saturate every position with its best players so `need` for
        # anyone else on the board collapses to ~0 everywhere.
        by_pos: dict[str, list[BoardPlayer]] = {}
        for bp in board.players:
            by_pos.setdefault(bp.position, []).append(bp)
        saturating = []
        for plist in by_pos.values():
            saturating.extend(bp.key for bp in plist[:6])

        remaining = [bp for bp in board.players if bp.key not in saturating][:40]
        by_value = sorted(remaining, key=lambda bp: -value(bp, saturating, board, cfg))
        by_vor = sorted(remaining, key=lambda bp: -bp.vor)
        assert [bp.key for bp in by_value] == [bp.key for bp in by_vor]


class TestRecommend:
    def test_never_affected_by_bye_week(self, tmp_path):
        # week=None must be used throughout the draft path -- scrambling
        # every candidate's bye week must not change any recommendation.
        rng = random.Random(55)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        recs_a = {r.player.key: (r.value, r.need) for r in recommend(state, cfg, limit=999)}

        scrambled = [replace(bp, bye_week=((bp.bye_week or 5) + 3) % 17 + 1) for bp in board.players]
        board_b = replace(board, players=scrambled, by_key={bp.key: bp for bp in scrambled})
        state_b = DraftState(board=board_b, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        recs_b = {r.player.key: (r.value, r.need) for r in recommend(state_b, cfg, limit=999)}

        for key, (val_a, need_a) in recs_a.items():
            val_b, need_b = recs_b[key]
            assert val_a == pytest.approx(val_b, abs=1e-6)
            assert need_a == pytest.approx(need_b, abs=1e-6)

    def test_latency_under_budget(self, tmp_path):
        rng = random.Random(9)
        board, cfg = _build_board(
            tmp_path, rng, {"QB": 40, "RB": 160, "WR": 200, "TE": 80, "K": 40, "DEF": 40}
        )
        state = DraftState(board=board, num_teams=12, my_slot=4, rounds=15, roster_positions=STANDARD_LAYOUT)
        for bp in board.players[:20]:
            state.record(bp.key, mine=(bp.rank % 12 == 0))

        start = time.perf_counter()
        recommend(state, cfg, limit=12)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 250

    def test_limit_and_position_filter(self, tmp_path):
        rng = random.Random(12)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        recs = recommend(state, cfg, limit=5, position="TE")
        assert len(recs) <= 5
        assert all(r.player.position == "TE" for r in recs)

    def test_taken_players_excluded(self, tmp_path):
        rng = random.Random(13)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        top_key = board.players[0].key
        state.record(top_key, mine=False)
        recs = recommend(state, cfg, limit=999)
        assert top_key not in {r.player.key for r in recs}


class TestAlerts:
    def test_run_detected(self, tmp_path):
        rng = random.Random(20)
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12, run_window=10, run_threshold=5))
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}, cfg=cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        rb_keys = [bp.key for bp in board.players if bp.position == "RB"][:6]
        for k in rb_keys:
            state.record(k, mine=False)
        out = alerts(state, cfg)
        assert any("RUN" in a and "RB" in a for a in out)

    def test_no_run_when_mixed(self, tmp_path):
        rng = random.Random(21)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        one_each = [
            next(bp.key for bp in board.players if bp.position == pos)
            for pos in ("QB", "RB", "WR", "TE")
        ]
        for k in one_each:
            state.record(k, mine=False)
        out = alerts(state, cfg)
        assert not any("RUN" in a for a in out)

    def test_bye_hole_detected_via_optimizer(self, tmp_path):
        rng = random.Random(23)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        rbs = [bp for bp in board.players if bp.position == "RB"][:2]
        forced = [replace(bp, bye_week=9) for bp in rbs]
        for bp in forced:
            board.by_key[bp.key] = bp
        for bp in forced:
            state.record(bp.key, mine=True)
        out = alerts(state, cfg)
        assert any("BYE" in a and "9" in a for a in out)

    def test_bye_alert_reports_only_the_caused_slot_not_the_whole_roster(self, tmp_path):
        # A single early pick leaves nearly every slot unfilled regardless
        # of bye week -- that reflects an incomplete roster, not a bye
        # collision. The BYE alert must report only the slot(s) that
        # specific bye actually knocks out (here: QB, the one thing this
        # lone player would otherwise have filled), not the other 8 unfilled
        # starting slots that are unfilled for an unrelated reason.
        rng = random.Random(24)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        one_qb = next(bp for bp in board.players if bp.position == "QB")
        state.record(one_qb.key, mine=True)
        out = alerts(state, cfg)
        bye_alerts = [a for a in out if "BYE" in a]
        assert bye_alerts
        assert "QB" in bye_alerts[0]
        assert "DEF" not in bye_alerts[0]
        assert "K" not in bye_alerts[0]


class TestNeedsBetween:
    def test_counts_opponent_picks_since_my_last_turn(self, tmp_path):
        rng = random.Random(30)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=4, my_slot=2, rounds=5, roster_positions=STANDARD_LAYOUT)
        # my picks at num_teams=4, slot=2: pick 2, then pick 7 (snake)
        state.record(next(bp.key for bp in board.players if bp.position == "QB"), mine=False)  # 1
        state.record(next(bp.key for bp in board.players if bp.position == "RB"), mine=True)   # 2 (mine)
        rb_keys = [bp.key for bp in board.players if bp.position == "RB"][1:3]
        state.record(rb_keys[0], mine=False)  # 3
        state.record(rb_keys[1], mine=False)  # 4
        wr_key = next(bp.key for bp in board.players if bp.position == "WR")
        state.record(wr_key, mine=False)  # 5
        state.record(next(bp.key for bp in board.players if bp.position == "TE"), mine=False)  # 6
        counts = needs_between(state)
        assert counts.get("RB") == 2
        assert counts.get("WR") == 1
        assert counts.get("TE") == 1
        assert "QB" not in counts  # pick 1 was before my last turn (pick 2), not since it
