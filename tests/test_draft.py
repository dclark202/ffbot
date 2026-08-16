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
    demand_ahead,
    expected_best_later,
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
    _bye_pressure,
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


class TestLinearOrder:
    def test_round_trip_exhaustive(self):
        for num_teams in range(1, 17):
            for round_ in range(1, 21):
                for slot in range(1, num_teams + 1):
                    pick = pick_number(round_, slot, num_teams, order="linear")
                    assert round_and_slot(pick, num_teams, order="linear") == (round_, slot)

    def test_bijection_onto_range(self):
        num_teams, rounds = 10, 15
        seen = set()
        for round_ in range(1, rounds + 1):
            for slot in range(1, num_teams + 1):
                pick = pick_number(round_, slot, num_teams, order="linear")
                assert pick not in seen
                seen.add(pick)
        assert seen == set(range(1, num_teams * rounds + 1))

    def test_same_slot_order_every_round(self):
        # Unlike snake, slot 1 always picks first in its round and slot T
        # always picks last -- no reversal on even rounds.
        num_teams = 8
        for round_ in range(1, 6):
            assert pick_number(round_, 1, num_teams, order="linear") == (round_ - 1) * num_teams + 1
            assert pick_number(round_, num_teams, num_teams, order="linear") == round_ * num_teams

    def test_differs_from_snake_on_even_rounds(self):
        num_teams = 6
        assert pick_number(2, 1, num_teams, order="linear") != pick_number(2, 1, num_teams, order="snake")
        assert pick_number(2, 1, num_teams, order="linear") == pick_number(2, 1, num_teams, order="linear")

    def test_matches_snake_on_round_one(self):
        # Round 1 is identical under both orders -- the reversal only ever
        # shows up from round 2 on.
        num_teams = 12
        for slot in range(1, num_teams + 1):
            assert pick_number(1, slot, num_teams, order="linear") == pick_number(1, slot, num_teams, order="snake")

    def test_default_order_is_snake(self):
        assert pick_number(2, 1, 10) == pick_number(2, 1, 10, order="snake")


class TestMyPickNumbers:
    def test_matches_pick_number(self):
        num_teams, rounds, slot = 10, 6, 4
        expected = [pick_number(r, slot, num_teams) for r in range(1, rounds + 1)]
        assert my_pick_numbers(slot, num_teams, rounds) == expected

    def test_length(self):
        assert len(my_pick_numbers(1, 12, 15)) == 15

    def test_linear_order_threaded_through(self):
        num_teams, rounds, slot = 8, 5, 3
        expected = [pick_number(r, slot, num_teams, order="linear") for r in range(1, rounds + 1)]
        assert my_pick_numbers(slot, num_teams, rounds, order="linear") == expected


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

    def test_defaults_to_snake_order(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        assert state.order == "snake"
        assert state.my_picks() == my_pick_numbers(3, 12, 15, order="snake")

    def test_linear_order_threaded_into_my_picks(self, tmp_path):
        rng = random.Random(5)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 30, "WR": 40, "TE": 15, "K": 10, "DEF": 10})
        state = DraftState(
            board=board, num_teams=12, my_slot=3, rounds=15, roster_positions=STANDARD_LAYOUT, order="linear"
        )
        assert state.my_picks() == my_pick_numbers(3, 12, 15, order="linear")
        assert state.my_picks() != my_pick_numbers(3, 12, 15, order="snake")

    def test_slot_for_matches_arithmetic_snake_order(self, tmp_path):
        state, cfg, board = self._state(tmp_path)  # my_slot=3, num_teams=12
        for _ in range(20):
            key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
            state.record(key)
        for pick in state.picks:
            assert state.slot_for(pick) == team_slot_at(pick.number, 12, "snake")

    def test_slot_for_a_pick_i_made_is_always_my_slot(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
        # Pick 1 does not arithmetically belong to my_slot=3 -- force it mine
        # anyway (a trade), and slot_for must still say my_slot, not slot 1.
        pick = state.record(key, mine=True)
        assert team_slot_at(pick.number, 12, "snake") != state.my_slot
        assert state.slot_for(pick) == state.my_slot

    def test_slot_for_a_traded_away_pick_is_unknown(self, tmp_path):
        rng = random.Random(5)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 30, "WR": 40, "TE": 15, "K": 10, "DEF": 10})
        # my_picks_override present but does not include pick 3 -- the
        # arithmetic slot for pick 3 under my_slot=3/num_teams=12 is mine,
        # but the override means I don't actually own it (traded away), so
        # its true owner is unknown, not "arithmetic says slot 3".
        state = DraftState(
            board=board, num_teams=12, my_slot=3, rounds=15, roster_positions=STANDARD_LAYOUT,
            my_picks_override=[1, 2, 15],
        )
        for _ in range(3):
            key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
            state.record(key)
        third_pick = state.picks[2]
        assert third_pick.number == 3
        assert third_pick.mine is False
        assert team_slot_at(3, 12, "snake") == 3 == state.my_slot
        assert state.slot_for(third_pick) is None

    def test_rosters_by_slot_groups_known_picks(self, tmp_path):
        state, cfg, board = self._state(tmp_path)  # my_slot=3, num_teams=12
        for _ in range(15):
            key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
            state.record(key)
        rosters = state.rosters_by_slot()
        assert 3 not in rosters or all(
            bp.key in {p.key for p in state.picks if p.mine} for bp in rosters[3]
        )
        # Every rostered player across all slots equals every known pick.
        total = sum(len(v) for v in rosters.values())
        assert total == len([p for p in state.picks if p.key is not None])
        for slot, roster in rosters.items():
            expected = {
                p.key for p in state.picks
                if p.key is not None and state.slot_for(p) == slot
            }
            assert {bp.key for bp in roster} == expected

    def test_rosters_by_slot_omits_unknown_and_missed_picks(self, tmp_path):
        state, cfg, board = self._state(tmp_path)
        state.record(None)  # missed / unknown pick
        key = next(bp.key for bp in board.players if bp.key not in state.taken_keys())
        state.record(key)
        rosters = state.rosters_by_slot()
        assert sum(len(v) for v in rosters.values()) == 1


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

    def test_bye_collision_weight_lowers_the_colliding_candidate(self, tmp_path):
        rng = random.Random(66)
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12, bye_collision_weight=5.0))
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}, cfg=cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)

        # Roster already has two RBs on bye week 9 -- a third on the same
        # week is a real collision; the flex slot cannot cover all three.
        rbs = [bp for bp in board.players if bp.position == "RB"][:2]
        forced = [replace(bp, bye_week=9) for bp in rbs]
        for bp in forced:
            board.by_key[bp.key] = bp
        for bp in forced:
            state.record(bp.key, mine=True)

        # Two more RB candidates, otherwise identical, differing only in bye.
        base = next(bp for bp in board.players if bp.position == "RB" and bp.key not in state.taken_keys())
        colliding = replace(base, key="collide:RB", name="Collide", bye_week=9)
        clean = replace(base, key="clean:RB", name="Clean", bye_week=3)
        board.by_key[colliding.key] = colliding
        board.by_key[clean.key] = clean
        board.players.append(colliding)
        board.players.append(clean)

        recs = {r.player.key: r.value for r in recommend(state, cfg, limit=9999)}
        assert recs[colliding.key] < recs[clean.key]

    def test_bye_collision_weight_zero_is_an_exact_noop(self, tmp_path):
        rng = random.Random(67)
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12, bye_collision_weight=0.0))
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}, cfg=cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=15, roster_positions=STANDARD_LAYOUT)
        rbs = [bp for bp in board.players if bp.position == "RB"][:2]
        forced = [replace(bp, bye_week=9) for bp in rbs]
        for bp in forced:
            board.by_key[bp.key] = bp
        for bp in forced:
            state.record(bp.key, mine=True)
        base = next(bp for bp in board.players if bp.position == "RB" and bp.key not in state.taken_keys())
        colliding = replace(base, key="collide:RB", name="Collide", bye_week=9)
        board.by_key[colliding.key] = colliding
        board.players.append(colliding)

        rec = next(r for r in recommend(state, cfg, limit=9999) if r.player.key == colliding.key)
        # At the 0.0 default the exact same candidate must score identically
        # to `need` + depth alone -- no bye term contribution at all.
        assert rec.value == pytest.approx(rec.need + cfg.draft.depth_weight * max(0.0, colliding.vor))

    def test_block_weight_lifts_a_position_opponents_still_need(self, tmp_path):
        rng = random.Random(71)
        counts = {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=3))
        board, cfg = _build_board(tmp_path, rng, counts, num_teams=3, cfg=cfg)
        state = DraftState(board=board, num_teams=3, my_slot=1, rounds=5, roster_positions=STANDARD_LAYOUT)

        my_first = next(bp for bp in board.players if bp.position == "WR")
        state.record(my_first.key, mine=True)  # pick 1, mine
        opp_pick = next(bp for bp in board.players if bp.position == "QB" and bp.key not in state.taken_keys())
        state.record(opp_pick.key, mine=False)  # pick 2, slot 2 -- only QB filled

        # Force one remaining RB to be the unambiguous best player left, so
        # it's a contender regardless of the random point draw.
        original = next(bp for bp in board.players if bp.position == "RB" and bp.key not in state.taken_keys())
        boosted = replace(original, points=999.0, vor=999.0)
        board.by_key[boosted.key] = boosted
        board.players[board.players.index(original)] = boosted

        val_off = next(r.value for r in recommend(state, cfg, limit=9999) if r.player.key == boosted.key)

        cfg_on = replace(cfg, draft=replace(cfg.draft, block_weight=10.0))
        val_on = next(r.value for r in recommend(state, cfg_on, limit=9999) if r.player.key == boosted.key)

        assert val_on > val_off


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


def _force(board, key: str, **changes):
    """Replace one board player in place, keeping `players`/`by_key` in sync."""
    original = board.by_key[key]
    forced = replace(original, **changes)
    board.by_key[key] = forced
    board.players[board.players.index(original)] = forced
    return forced


class TestSurvivalTarget:
    """Survival is measured to my next turn STRICTLY after the current pick.

    The bug this guards: `recommend()` used `next_my_pick()`, which returns
    the CURRENT pick whenever I'm on the clock. `survival(from_pick=p,
    to_pick=p)` is 1.0 by definition, so every candidate reported "100%
    survives" on exactly the picks where "what does passing on him cost?"
    is the question being asked.
    """

    def _state(self, tmp_path, seed, num_teams=12, my_slot=1, rounds=15):
        rng = random.Random(seed)
        board, cfg = _build_board(
            tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8},
            num_teams=num_teams,
            cfg=Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=num_teams)),
        )
        state = DraftState(
            board=board, num_teams=num_teams, my_slot=my_slot, rounds=rounds,
            roster_positions=STANDARD_LAYOUT,
        )
        return state, cfg, board

    def test_strictly_after_the_given_pick(self, tmp_path):
        state, _, _ = self._state(tmp_path, 90)
        assert state.current_pick() == 1
        assert state.next_my_pick() == 1  # I am the one on the clock
        assert state.next_my_pick_after(1) == 24

    def test_agrees_with_next_my_pick_off_the_clock(self, tmp_path):
        state, _, board = self._state(tmp_path, 90, my_slot=3)
        assert state.next_my_pick() == 3
        # The current pick isn't mine, so ">= current" and "> current" pick
        # out the same turn -- the two only diverge on my own clock.
        assert state.next_my_pick_after(state.current_pick()) == 3

    def test_on_the_clock_survival_is_not_uniformly_certain(self, tmp_path):
        state, cfg, board = self._state(tmp_path, 91)
        assert state.current_pick() == state.next_my_pick()  # on the clock

        early = next(bp for bp in board.players if bp.position == "RB")
        forced = _force(board, early.key, adp=2.0, adp_stdev=3.0, points=999.0, vor=999.0)

        rec = next(r for r in recommend(state, cfg, limit=9999) if r.player.key == forced.key)
        assert rec.survival is not None
        # ADP 2 with my next turn 23 picks away: essentially gone.
        assert rec.survival < 0.05

    def test_no_target_after_my_final_pick_means_no_survival_number(self, tmp_path):
        state, cfg, board = self._state(tmp_path, 92, num_teams=2, my_slot=1, rounds=2)
        for _ in range(3):  # my picks (snake, 2 teams) are 1 and 4
            state.record(next(bp.key for bp in board.players if bp.key not in state.taken_keys()))
        assert state.current_pick() == 4
        assert state.next_my_pick() == 4
        assert state.next_my_pick_after(4) is None
        assert all(r.survival is None for r in recommend(state, cfg, limit=20))


class TestAlertsFocus:
    """The trimmed alert set: RUN (opponents only), WAIT, BYE — no tiers."""

    COUNTS = {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8}

    def _build(self, tmp_path, seed, draft_kwargs=None, num_teams=12, my_slot=1, rounds=15):
        rng = random.Random(seed)
        cfg = Config(
            roster_positions=STANDARD_LAYOUT,
            draft=DraftConfig(num_teams=num_teams, **(draft_kwargs or {})),
        )
        board, cfg = _build_board(tmp_path, rng, self.COUNTS, num_teams=num_teams, cfg=cfg)
        state = DraftState(
            board=board, num_teams=num_teams, my_slot=my_slot, rounds=rounds,
            roster_positions=STANDARD_LAYOUT,
        )
        return state, cfg, board

    def test_no_tier_based_alerts_remain(self, tmp_path):
        # CLIFF and ROOM were removed: both framed scarcity as a tier
        # boundary, and CLIFF fired at nearly every position on nearly every
        # pick because a "best remaining tier" is thin by construction.
        state, cfg, board = self._build(tmp_path, 40, my_slot=6)
        for _ in range(30):
            state.record(next(bp.key for bp in board.players if bp.key not in state.taken_keys()))
        out = alerts(state, cfg)
        assert not any("CLIFF" in a or "ROOM" in a or "tier" in a.lower() for a in out)

    def test_run_counts_only_opponent_picks(self, tmp_path):
        # A run is what the ROOM is doing. Counting my own picks let a roster
        # I had deliberately built trip an alert about my own strategy.
        state, cfg, board = self._build(
            tmp_path, 41,
            {"run_window": 10, "run_threshold": 5, "position_targets": {"RB": 10}},
        )
        for k in [bp.key for bp in board.players if bp.position == "RB"][:6]:
            state.record(k, mine=True)
        assert not any("RUN" in a for a in alerts(state, cfg))

        # The identical six picks, made by opponents, do fire it.
        state.picks = [replace(p, mine=False) for p in state.picks]
        assert any("RUN" in a and "RB" in a for a in alerts(state, cfg))

    def test_run_is_silent_on_a_position_i_have_capped(self, tmp_path):
        state, cfg, board = self._build(
            tmp_path, 42, {"run_window": 10, "run_threshold": 5, "position_caps": {"RB": 1}},
        )
        rbs = [bp.key for bp in board.players if bp.position == "RB"][:7]
        state.record(rbs[0], mine=True)  # at the cap -- RB is no longer a candidate
        for k in rbs[1:]:
            state.record(k, mine=False)
        assert not any("RUN" in a for a in alerts(state, cfg))

        uncapped = replace(cfg, draft=replace(cfg.draft, position_caps={}))
        assert any("RUN" in a and "RB" in a for a in alerts(state, uncapped))

    def test_wait_reports_the_points_lost_by_passing(self, tmp_path):
        state, cfg, board = self._build(tmp_path, 43)
        rbs = [bp for bp in board.players if bp.position == "RB"]
        _force(board, rbs[0].key, points=300.0, adp=1.0, adp_stdev=1.0)  # gone well before pick 24
        for bp in rbs[1:]:
            _force(board, bp.key, points=200.0, adp=200.0, adp_stdev=5.0)  # all still there

        out = [a for a in alerts(state, cfg) if a.startswith("WAIT:") and " RB " in a]
        assert len(out) == 1
        assert "100 pts" in out[0]
        assert "pick 24" in out[0]

    def test_wait_is_silent_when_the_best_player_survives(self, tmp_path):
        state, cfg, board = self._build(tmp_path, 44)
        for bp in [bp for bp in board.players if bp.position == "RB"]:
            _force(board, bp.key, points=200.0, adp=200.0, adp_stdev=5.0)
        assert not any(a.startswith("WAIT:") and " RB " in a for a in alerts(state, cfg))

    def test_wait_respects_the_configured_gap_threshold(self, tmp_path):
        state, cfg, board = self._build(tmp_path, 45, {"alert_gap_points": 1000.0})
        rbs = [bp for bp in board.players if bp.position == "RB"]
        _force(board, rbs[0].key, points=300.0, adp=1.0, adp_stdev=1.0)
        for bp in rbs[1:]:
            _force(board, bp.key, points=200.0, adp=200.0, adp_stdev=5.0)
        assert not any(a.startswith("WAIT:") for a in alerts(state, cfg))

    def test_alert_limit_caps_the_panel(self, tmp_path):
        state, cfg, board = self._build(tmp_path, 46, {"alert_limit": 1})
        rbs = [bp for bp in board.players if bp.position == "RB"]
        _force(board, rbs[0].key, points=300.0, adp=1.0, adp_stdev=1.0)
        for bp in rbs[1:]:
            _force(board, bp.key, points=200.0, adp=200.0, adp_stdev=5.0)
        assert len(alerts(state, cfg)) == 1


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


class TestByePressure:
    def test_zero_on_an_empty_roster(self, tmp_path):
        rng = random.Random(80)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        # Nothing rostered yet to collide with.
        assert _bye_pressure([], STANDARD_LAYOUT, "RB", 9, cfg) == 0.0

    def test_pressure_grows_with_more_colliding_depth(self, tmp_path):
        # A second RB sharing the same bye stacks a real hole deeper than a
        # single RB on that bye already does -- monotone, not a step
        # function that only fires at 2+.
        rng = random.Random(84)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        rbs = [replace(bp, bye_week=9) for bp in board.players if bp.position == "RB"]
        one = _bye_pressure(rbs[:1], STANDARD_LAYOUT, "RB", 9, cfg)
        two = _bye_pressure(rbs[:2], STANDARD_LAYOUT, "RB", 9, cfg)
        assert 0.0 <= one <= two <= 1.0

    def test_positive_once_the_position_and_bye_already_collide(self, tmp_path):
        rng = random.Random(81)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        rbs = [replace(bp, bye_week=9) for bp in board.players if bp.position == "RB"][:2]
        assert _bye_pressure(rbs, STANDARD_LAYOUT, "RB", 9, cfg) > 0.0

    def test_lower_for_a_bye_that_does_not_collide(self, tmp_path):
        rng = random.Random(82)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        rbs = [replace(bp, bye_week=9) for bp in board.players if bp.position == "RB"][:2]
        colliding = _bye_pressure(rbs, STANDARD_LAYOUT, "RB", 9, cfg)
        distinct = _bye_pressure(rbs, STANDARD_LAYOUT, "RB", 3, cfg)
        assert distinct < colliding

    def test_bounded_zero_to_one(self, tmp_path):
        rng = random.Random(83)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        rbs = [replace(bp, bye_week=9) for bp in board.players if bp.position == "RB"][:4]
        p = _bye_pressure(rbs, STANDARD_LAYOUT, "RB", 9, cfg)
        assert 0.0 <= p <= 1.0


class TestDemandAhead:
    _LAYOUT = {"QB": 1, "RB": 1, "WR": 1, "BN": 1}

    def _state(self, tmp_path, my_slot, num_teams, rounds, seed=77):
        rng = random.Random(seed)
        cfg = Config(roster_positions=self._LAYOUT, draft=DraftConfig(num_teams=num_teams))
        board, cfg = _build_board(
            tmp_path, rng, {"QB": 10, "RB": 10, "WR": 10, "TE": 5, "K": 5, "DEF": 5},
            num_teams=num_teams, cfg=cfg,
        )
        state = DraftState(
            board=board, num_teams=num_teams, my_slot=my_slot, rounds=rounds, roster_positions=self._LAYOUT
        )
        return state, cfg, board

    def _take(self, state, board, position, mine=False):
        bp = next(
            b for b in board.players if b.position == position and b.key not in state.taken_keys()
        )
        state.record(bp.key, mine=mine)
        return bp

    def test_empty_when_it_is_already_my_turn(self, tmp_path):
        state, cfg, board = self._state(tmp_path, my_slot=1, num_teams=3, rounds=2)
        assert state.next_my_pick() == state.current_pick() == 1
        assert demand_ahead(state, cfg) == {}

    def test_empty_once_my_picks_are_exhausted(self, tmp_path):
        state, cfg, board = self._state(tmp_path, my_slot=1, num_teams=3, rounds=1)
        for _ in range(3):
            self._take(state, board, "QB")
        assert state.next_my_pick() is None
        assert demand_ahead(state, cfg) == {}

    def test_reflects_an_opponents_unfilled_starting_slot(self, tmp_path):
        state, cfg, board = self._state(tmp_path, my_slot=1, num_teams=3, rounds=2)
        self._take(state, board, "WR", mine=True)  # pick 1, mine
        self._take(state, board, "QB")  # pick 2, slot 2 -- QB now covered for them
        demand = demand_ahead(state, cfg)
        # Slot 3 hasn't picked at all yet (wants everything); slot 2 already
        # has a QB. Every team in the gap still wants RB and WR, so those
        # read strictly higher than QB, which only one of them needs.
        assert demand["RB"] > 0.0
        assert demand["WR"] > 0.0
        assert demand["QB"] < demand["RB"]

    def test_nearer_opponent_need_outweighs_a_farther_identical_one(self, tmp_path):
        state, cfg, board = self._state(tmp_path, my_slot=1, num_teams=3, rounds=2)
        self._take(state, board, "WR", mine=True)  # pick 1, mine
        self._take(state, board, "QB")  # pick 2, slot 2 -- leaves RB/WR open
        self._take(state, board, "RB")  # pick 3, slot 3 -- leaves QB/WR open
        # Round 2 (reversed) puts slot 3 back on the clock immediately and
        # slot 2 one pick later.
        assert team_slot_at(state.current_pick(), 3, "snake") == 3
        demand = demand_ahead(state, cfg)
        # slot 3 (nearer) is missing QB; slot 2 (farther) is missing RB --
        # both single-team needs, but the nearer one must weigh more.
        assert demand["QB"] > demand["RB"]


def _scarcity_board(tmp_path, pos_specs, cfg, num_teams=12):
    """Board from an explicit `{position: [(points, adp), ...]}` spec.

    `_random_rows` above is deliberately random, which is exactly wrong for
    the scarcity term: the behaviour under test is how one position's ADP
    profile compares to another's, so both have to be stated exactly. An
    `adp` of None is written as an empty AVG cell -- how `read_fantasypros`
    represents "no ADP", one of the cases this term has a documented
    convention for.
    """
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    for pos, entries in pos_specs.items():
        for i, (points, adp) in enumerate(entries):
            cell = "" if adp is None else adp
            lines.append(f"{pos}{i},XXX,{pos},7,{points},{cell}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return load_board([path], cfg.roster_positions, num_teams, cfg)


class TestExpectedBestLater:
    """The survival-weighted "what will this position still offer at my next
    pick" estimate behind `DraftConfig.scarcity_weight`."""

    def test_no_next_pick_is_empty(self):
        bases = [("RB", 50.0, 10.0, None), ("WR", 40.0, 12.0, None)]
        assert expected_best_later(bases, 30, None, DraftConfig()) == {}

    def test_certain_survivor_is_worth_its_full_value(self):
        # ADP far in the future -> survives ~certainly -> waiting costs
        # nothing, so the position's "later" value is its best player's.
        later = expected_best_later([("RB", 50.0, 400.0, None)], 30, 45, DraftConfig())
        assert later["RB"] == pytest.approx(50.0, abs=0.5)

    def test_certainly_gone_player_contributes_nothing(self):
        # ADP long past -- `survival` returns 0.0, so the only player at the
        # position leaves nothing behind to wait for.
        later = expected_best_later([("RB", 50.0, 1.0, None)], 100, 115, DraftConfig())
        assert later["RB"] == pytest.approx(0.0, abs=1e-6)

    def test_missing_adp_survives_at_full_probability(self):
        # The documented convention: the ADP model has no opinion at all
        # about someone it never priced, so "nobody takes him before my next
        # pick" is the honest read rather than treating him as vanished.
        later = expected_best_later([("RB", 50.0, None, None)], 30, 45, DraftConfig())
        assert later["RB"] == pytest.approx(50.0)

    def test_negative_bases_floor_at_zero(self):
        # A position whose only survivors are worth negative points is worth
        # exactly nothing to wait for -- never a bonus for waiting.
        bases = [("RB", -30.0, None, None), ("RB", -10.0, None, None)]
        later = expected_best_later(bases, 30, 45, DraftConfig())
        assert later["RB"] == pytest.approx(0.0)

    def test_thin_position_is_worth_less_later_than_a_deep_one(self):
        # Same best-available value now; the deep position keeps more of it.
        thin = [("RB", 50.0, 31.0, 1.0)]
        deep = [("WR", 50.0, 200.0, 1.0), ("WR", 49.0, 210.0, 1.0)]
        later = expected_best_later(thin + deep, 30, 45, DraftConfig())
        assert later["WR"] > later["RB"]

    def test_expectation_is_bounded_by_the_best_available(self):
        # An expected MAXIMUM can never exceed the largest value in the pool
        # -- the guard against the probability bookkeeping double-counting.
        bases = [("RB", 50.0, None, None), ("RB", 40.0, None, None), ("RB", 30.0, None, None)]
        later = expected_best_later(bases, 30, 45, DraftConfig())
        assert later["RB"] <= 50.0 + 1e-9

    def test_more_depth_never_lowers_the_expectation(self):
        # Monotonicity: another available player at a position can only make
        # waiting more attractive, never less.
        one = expected_best_later([("RB", 50.0, 40.0, 5.0)], 30, 45, DraftConfig())
        two = expected_best_later(
            [("RB", 50.0, 40.0, 5.0), ("RB", 45.0, 60.0, 5.0)], 30, 45, DraftConfig()
        )
        assert two["RB"] >= one["RB"]


class TestScarcityWeight:
    """`scarcity_weight` prices the opportunity cost of NOT taking a position
    now -- the fix for the 7-WR/1-RB rosters two live mock drafts produced
    while following the top recommendation at every turn.

    The failure it addresses: in full PPR `derive_replacement` gives WR and
    RB nearly the same replacement level, so `need` treats "a WR now" and "a
    WR next round too" as the same decision even when every startable RB is
    about to be gone and equivalent WRs are not.
    """

    LAYOUT = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "BN": 6}

    def _cfg(self, scarcity: float, **draft_kwargs):
        return Config(
            roster_positions=self.LAYOUT,
            draft=DraftConfig(num_teams=12, scarcity_weight=scarcity, **draft_kwargs),
        )

    def _spec_thin_rb_deep_wr(self):
        """WR is strictly the better STATIC pick (higher points top to
        bottom) but survives to my next turn; the whole RB pool is priced to
        be gone before then. Pools are deep enough that replacement level
        sits well below the top of each -- a four-player position collapses
        replacement onto its own last man and flattens VOR to nothing,
        which tests the arithmetic rather than the behaviour.
        """
        # The two ladders use different steps on purpose: an identical step
        # gives both positions the same top-of-board VOR no matter what
        # offset the points carry (replacement moves with the ladder), which
        # would leave the "static pick" ambiguous and decided by rank
        # tiebreak rather than by value.
        return {
            "RB": [(250.0 - 10.0 * i, 26.0 + i) for i in range(12)],
            "WR": [(255.0 - 12.0 * i, 200.0 + 5.0 * i) for i in range(12)],
            "QB": [(200.0 - 8.0 * i, 300.0 + 5.0 * i) for i in range(6)],
            "TE": [(160.0 - 8.0 * i, 330.0 + 5.0 * i) for i in range(6)],
        }

    def test_zero_weight_leaves_the_original_formula_untouched(self, tmp_path):
        # A stock DraftConfig has every edge weight at 0.0 and an empty
        # roster gives every position a depth factor of 1.0, so the whole
        # score reduces to exactly `need + depth_weight * max(0, vor)` --
        # and on an empty roster `need` is exactly `vor`. Any leakage from
        # the scarcity term would break this equality.
        rng = random.Random(101)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        assert cfg.draft.scarcity_weight == 0.0
        state = DraftState(board=board, num_teams=12, my_slot=4, rounds=15, roster_positions=STANDARD_LAYOUT)

        for r in recommend(state, cfg, limit=999):
            assert r.later == 0.0
            expected = r.need + cfg.draft.depth_weight * max(0.0, r.player.vor)
            assert r.value == pytest.approx(expected, abs=1e-9)

    def test_zero_weight_never_computes_the_estimate(self, tmp_path, monkeypatch):
        # Structural, not just numeric: prove the no-op skips the work, the
        # same way the bye_pressure/position_demand guards do.
        import ffbot.draft as draft_mod

        rng = random.Random(102)
        board, cfg = _build_board(tmp_path, rng, {"QB": 10, "RB": 25, "WR": 30, "TE": 12, "K": 8, "DEF": 8})
        state = DraftState(board=board, num_teams=12, my_slot=4, rounds=15, roster_positions=STANDARD_LAYOUT)

        def _poisoned(*args, **kwargs):
            raise AssertionError("expected_best_later must not run at scarcity_weight=0.0")

        monkeypatch.setattr(draft_mod, "expected_best_later", _poisoned)
        recommend(state, cfg, limit=5)  # must not raise

    def test_prefers_the_evaporating_position_over_the_stable_one(self, tmp_path):
        # Two positions, identical points. Every RB goes within the next few
        # picks; the WRs last well past my next turn. Taking the RB now is
        # the only way to get one at all, so it must rank first -- and with
        # the term off, the same board ranks a WR first, which is the bug.
        cfg = self._cfg(1.0)
        board = _scarcity_board(tmp_path, self._spec_thin_rb_deep_wr(), cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=6, roster_positions=self.LAYOUT)
        for _ in range(24):
            state.record(None, mine=False)

        assert recommend(state, cfg, limit=1)[0].player.position == "RB"
        assert recommend(state, self._cfg(0.0), limit=1)[0].player.position == "WR"

    def test_a_deep_stable_position_is_discounted_toward_zero(self, tmp_path):
        # If waiting costs nothing, taking the player now WINS nothing --
        # even at a large raw VOR.
        cfg = self._cfg(1.0)
        board = _scarcity_board(
            tmp_path,
            {
                "WR": [(300.0, 400.0), (299.0, 401.0), (298.0, 402.0), (297.0, 403.0)],
                "RB": [(150.0, 405.0), (149.0, 406.0)],
                "QB": [(140.0, 407.0)],
                "TE": [(130.0, 408.0)],
            },
            cfg,
        )
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=6, roster_positions=self.LAYOUT)
        for _ in range(12):
            state.record(None, mine=False)

        best_wr = max(
            (r for r in recommend(state, cfg, limit=999) if r.player.position == "WR"),
            key=lambda r: r.need,
        )
        assert best_wr.later > 0.0
        assert best_wr.value < 5.0

    def test_last_pick_of_the_draft_is_not_discounted(self, tmp_path):
        # No next pick -> no "later" -> nothing subtracted, so every value
        # matches the same board with the term switched off.
        cfg_on, cfg_off = self._cfg(1.0), self._cfg(0.0)
        board = _scarcity_board(tmp_path, self._spec_thin_rb_deep_wr(), cfg_on)
        rounds = 2
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=rounds, roster_positions=self.LAYOUT)
        while state.current_pick() < pick_number(rounds, 1, 12):
            state.record(None, mine=False)
        assert state.next_my_pick_after(state.current_pick()) is None

        off = {r.player.key: r.value for r in recommend(state, cfg_off, limit=999)}
        for r in recommend(state, cfg_on, limit=999):
            assert r.later == 0.0
            assert r.value == pytest.approx(off[r.player.key], abs=1e-9)

    def test_reason_explains_a_scarcity_driven_ranking(self, tmp_path):
        # A ranking that puts a lower-VOR player first has to say why.
        cfg = self._cfg(1.0)
        board = _scarcity_board(tmp_path, self._spec_thin_rb_deep_wr(), cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=6, roster_positions=self.LAYOUT)
        for _ in range(24):
            state.record(None, mine=False)
        assert "over waiting" in recommend(state, cfg, limit=1)[0].reason

    def test_reason_stays_silent_when_the_term_is_off(self, tmp_path):
        cfg = self._cfg(0.0)
        board = _scarcity_board(tmp_path, self._spec_thin_rb_deep_wr(), cfg)
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=6, roster_positions=self.LAYOUT)
        for _ in range(12):
            state.record(None, mine=False)
        for r in recommend(state, cfg, limit=999):
            assert "over waiting" not in r.reason

    def test_position_filter_reports_the_same_values_as_the_full_board(self, tmp_path):
        # A filtered view (`p RB`) must never show a different number than
        # the unfiltered board does for the same player.
        cfg = self._cfg(1.0)
        board = _scarcity_board(
            tmp_path,
            {
                "RB": [(240.0, 26.0), (238.0, 40.0), (236.0, 55.0)],
                "WR": [(240.0, 200.0), (238.0, 210.0)],
                "QB": [(180.0, 300.0)],
                "TE": [(150.0, 310.0)],
            },
            cfg,
        )
        state = DraftState(board=board, num_teams=12, my_slot=1, rounds=6, roster_positions=self.LAYOUT)
        for _ in range(12):
            state.record(None, mine=False)

        full = {r.player.key: r.value for r in recommend(state, cfg, limit=999)}
        for r in recommend(state, cfg, limit=999, position="RB"):
            assert r.value == pytest.approx(full[r.player.key], abs=1e-9)

    def _draft_out(self, board, cfg, rounds=6, num_teams=12):
        """Draft the whole board: I take the top recommendation at my turns,
        the room takes best-available by ADP (the same simple opponent model
        `ffbot/backtest/draft_sim.py` uses). Returns my position counts."""
        state = DraftState(
            board=board, num_teams=num_teams, my_slot=1, rounds=rounds,
            roster_positions=self.LAYOUT,
        )
        my_picks = set(state.my_picks())
        while state.current_pick() <= num_teams * rounds:
            if state.current_pick() in my_picks:
                state.record(recommend(state, cfg, limit=1)[0].player.key, mine=True)
            else:
                taken = state.taken_keys()
                pool = [bp for bp in board.players if bp.key not in taken and bp.adp is not None]
                state.record(min(pool, key=lambda bp: bp.adp).key, mine=False)

        counts: dict[str, int] = {}
        for bp in state.my_roster():
            counts[bp.position] = counts.get(bp.position, 0) + 1
        return counts

    def test_roster_construction_stops_ignoring_the_scarce_position(self, tmp_path):
        # The end-to-end regression, in the shape of the real failure: a
        # full-PPR-style board where WR is deep and the market lets it fall,
        # while the RB pool is short and drafted early. Following the top
        # recommendation every turn used to yield a roster with a single RB
        # on it; the scarcity term has to buy real RBs instead.
        cfg = self._cfg(1.0)
        # Deep enough to fill a 12x6 draft (72 picks) without exhausting the
        # pool, which would say nothing about roster construction.
        spec = {
            "RB": [(250.0 - 6 * i, 12.0 + 4.0 * i) for i in range(16)],
            "WR": [(250.0 - 2 * i, 20.0 + 3.0 * i) for i in range(40)],
            "QB": [(200.0 - 3 * i, 90.0 + 6.0 * i) for i in range(14)],
            "TE": [(160.0 - 4 * i, 100.0 + 8.0 * i) for i in range(14)],
        }
        board = _scarcity_board(tmp_path, spec, cfg)

        with_term = self._draft_out(board, cfg)
        without_term = self._draft_out(board, self._cfg(0.0))

        # The concrete failure: the old ranking starves the scarce position.
        assert without_term.get("RB", 0) <= 1, without_term
        assert with_term.get("RB", 0) >= 3, with_term
        assert with_term["RB"] > without_term.get("RB", 0)


class TestBenchReplacementDepth:
    """`bench_replacement_depth` prices bench players against a waiver-level
    baseline instead of the starter replacement level.

    The failure it fixes, measured on a real board mid-draft: `need` is
    exactly 0 for anyone who can't crack a starting lineup, and the old
    depth term `depth_weight * max(0, vor)` is exactly 0 for anyone below
    replacement -- so at pick 124 of a 14-round draft ALL 505 remaining
    candidates scored an identical 0.00, a 132-point RB indistinguishable
    from a 90-point WR. `edge.decision_scale` then floors at 1.0, shrinking
    every fraction-of-scale weight (including `balance_weight`, which is how
    `position_targets` is supposed to steer) to a rounding error.
    """

    LAYOUT = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "BN": 6}

    def _cfg(self, bench_depth: float, **kw):
        return Config(
            roster_positions=self.LAYOUT,
            draft=DraftConfig(num_teams=12, bench_replacement_depth=bench_depth, **kw),
        )

    def _board(self, tmp_path, cfg, per_pos=40):
        spec = {
            pos: [(300.0 - 3.0 * i, float(i + 1) * 2.0) for i in range(per_pos)]
            for pos in ("QB", "RB", "WR", "TE")
        }
        return _scarcity_board(tmp_path, spec, cfg)

    def _saturated_state(self, board, cfg, rounds=14):
        """A state whose starting slots are all covered by good players --
        the regime where every remaining candidate used to tie at 0.00."""
        state = DraftState(
            board=board, num_teams=12, my_slot=1, rounds=rounds,
            roster_positions=self.LAYOUT,
        )
        best = sorted(board.players, key=lambda bp: -bp.points)
        for bp in best[:8]:
            state.record(bp.key, mine=True)
        while state.current_pick() < 100:
            pool = [bp for bp in board.players if bp.key not in state.taken_keys()]
            state.record(max(pool, key=lambda bp: bp.points).key, mine=False)
        return state

    def test_zero_depth_is_the_historical_formula_exactly(self, tmp_path):
        cfg = self._cfg(0.0)
        board = self._board(tmp_path, cfg)
        assert not board.bench_replacement
        state = DraftState(
            board=board, num_teams=12, my_slot=1, rounds=14, roster_positions=self.LAYOUT
        )
        for r in recommend(state, cfg, limit=999):
            expected = r.need + cfg.draft.depth_weight * max(0.0, r.player.vor)
            assert r.value == pytest.approx(expected, abs=1e-9)

    def test_below_replacement_players_used_to_tie_and_now_do_not(self, tmp_path):
        # THE regression. Same board, same state, both dial settings.
        off = self._cfg(0.0)
        board = self._board(tmp_path, off)
        state = self._saturated_state(board, off)

        off_vals = [r.value for r in recommend(state, off, limit=999)]
        assert off_vals.count(pytest.approx(off_vals[0])) > 50, (
            "expected the old formula to collapse a large block onto one value"
        )

        on = self._cfg(2.0)
        board_on = self._board(tmp_path, on)
        state_on = self._saturated_state(board_on, on)
        recs = [r for r in recommend(state_on, on, limit=999) if r.player.position == "RB"]
        distinct = {round(r.value, 6) for r in recs[:20]}
        assert len(distinct) > 10, f"still collapsing: {sorted(distinct)}"

    def test_better_bench_player_outranks_a_worse_one_at_the_same_position(self, tmp_path):
        # The concrete consequence: among below-replacement players at a
        # covered position, more projected points must mean more value.
        cfg = self._cfg(2.0)
        board = self._board(tmp_path, cfg)
        state = self._saturated_state(board, cfg)
        rbs = [r for r in recommend(state, cfg, limit=999) if r.player.position == "RB"]
        ranked = sorted(rbs, key=lambda r: -r.player.points)[:10]
        for better, worse in zip(ranked, ranked[1:]):
            assert better.value >= worse.value

    def test_decision_scale_no_longer_floors(self, tmp_path):
        # Guards the "no second knob needed" claim: reviving the base values
        # is what restores every fraction-of-scale weight, `balance_weight`
        # included, without making it point-denominated.
        from ffbot import edge as edge_mod
        from ffbot.draft import _depth_factors, _depth_value

        for depth, expect_floored in ((0.0, True), (2.0, False)):
            cfg = self._cfg(depth)
            board = self._board(tmp_path, cfg)
            state = self._saturated_state(board, cfg)
            roster = state.my_roster()
            factors = _depth_factors(roster, self.LAYOUT, cfg)
            bases = [
                need(bp, [b.key for b in roster], board, cfg)
                + _depth_value(bp, board, factors, cfg)
                for bp in board.players
                if bp.key not in state.taken_keys()
            ]
            scale = edge_mod.decision_scale(bases)
            if expect_floored:
                assert scale == pytest.approx(edge_mod._MIN_DECISION_SCALE)
            else:
                assert scale > 5.0

    def test_reported_value_matches_the_single_depth_formula(self, tmp_path):
        # `recommend()` builds its pre-edge `scored` list and `_combine`
        # builds the final value; both must use `_depth_value`, or the
        # ranking and the number on screen silently diverge.
        from ffbot.draft import _depth_factors, _depth_value

        cfg = self._cfg(2.0)
        board = self._board(tmp_path, cfg)
        state = self._saturated_state(board, cfg)
        roster_keys = [bp.key for bp in state.my_roster()]
        factors = _depth_factors(state.my_roster(), self.LAYOUT, cfg)
        for r in recommend(state, cfg, limit=40):
            expected_depth = _depth_value(r.player, board, factors, cfg)
            # value = need + depth + edge - scarcity*later; with every edge
            # weight at 0 and scarcity off, it is exactly need + depth.
            assert r.value == pytest.approx(r.need + expected_depth, abs=1e-9)

    def test_a_player_below_the_bench_baseline_still_scores_zero_depth(self, tmp_path):
        # The clamp stays: worse than the waiver-level baseline is worth
        # nothing as depth, never a penalty.
        from ffbot.draft import _depth_value

        cfg = self._cfg(2.0)
        # Deep enough that the 2x cut lands mid-pool rather than clamping
        # onto the last man -- otherwise there is no "below the baseline"
        # player to test with.
        board = self._board(tmp_path, cfg, per_pos=100)
        worst = min(board.players, key=lambda bp: bp.points)
        assert worst.points < board.bench_replacement[worst.position]
        assert _depth_value(worst, board, {}, cfg) == 0.0
