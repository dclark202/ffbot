from __future__ import annotations

import pytest

from ffbot import edge
from ffbot.board import Board
from ffbot.config import Config, DraftConfig
from tests.conftest import mk_bp


def _board(players) -> Board:
    return Board(
        players=list(players),
        by_key={p.key: p for p in players},
        replacement={},
        starters_per_pos={},
        tier_last={},
    )


def _with_spread(players, n=25):
    """Add filler so the pick has a value spread for the bonus to be a fraction of.

    The bonus is sized relative to what is at stake (`edge.decision_scale`), so
    on a board where every option is worth the same it is correctly zero — real
    boards always have a spread, but two-player test fixtures do not.
    """
    filler = [
        mk_bp(f"Filler{i}", "WR", points=100.0 - i * 4.0, adp=90.0 + i, adp_spread=3.0)
        for i in range(n)
    ]
    return list(players) + filler


def _spicy(**kw) -> Config:
    """A config with every edge term switched on, for testing that they bite."""
    defaults = dict(
        upside_weight=26.0,
        volatility_weight=18.0,
        stack_bonus=12.0,
        arbitrage_weight=0.15,
        risk_ramp_start=2,
        risk_ramp_full=5,
    )
    defaults.update(kw)
    return Config(draft=DraftConfig(**defaults))


class TestRiskRamp:
    def test_zero_through_start_then_one_after_full(self):
        cfg = _spicy()
        assert edge.risk_ramp(1, cfg) == 0.0
        assert edge.risk_ramp(2, cfg) == 0.0
        assert edge.risk_ramp(5, cfg) == 1.0
        assert edge.risk_ramp(15, cfg) == 1.0

    def test_monotone_non_decreasing(self):
        cfg = _spicy()
        values = [edge.risk_ramp(r, cfg) for r in range(1, 16)]
        assert values == sorted(values)
        assert all(0.0 <= v <= 1.0 for v in values)

    def test_interpolates_between(self):
        cfg = _spicy()
        # start=2, full=5 -> round 3 is one third of the way, round 4 two thirds.
        assert edge.risk_ramp(3, cfg) == pytest.approx(1 / 3, abs=1e-6)
        assert edge.risk_ramp(4, cfg) == pytest.approx(2 / 3, abs=1e-6)

    def test_degenerate_window_does_not_divide_by_zero(self):
        # A config where full <= start must not blow up; past start is full risk.
        cfg = _spicy(risk_ramp_start=5, risk_ramp_full=5)
        assert edge.risk_ramp(4, cfg) == 0.0
        assert edge.risk_ramp(6, cfg) == 1.0


class TestVolatilityMap:
    def test_excludes_kickers_and_defenses(self):
        # K/DEF top the raw spread list because nobody agrees on them, which is
        # noise rather than signal — they must not appear at all.
        players = [
            mk_bp("Kicker A", "K", adp=100.0, adp_spread=40.0),
            mk_bp("Kicker B", "K", adp=120.0, adp_spread=30.0),
            mk_bp("Def A", "DEF", adp=110.0, adp_spread=35.0),
            mk_bp("Def B", "DEF", adp=130.0, adp_spread=25.0),
            mk_bp("Wr A", "WR", adp=20.0, adp_spread=2.0),
            mk_bp("Wr B", "WR", adp=30.0, adp_spread=9.0),
        ]
        vol = edge.volatility_map(_board(players))
        assert not any(k.endswith(":K") or k.endswith(":DEF") for k in vol)
        assert set(vol) == {"wr a:WR", "wr b:WR"}

    def test_ranked_within_position(self):
        # A WR's uncertainty is judged against other WRs, so an RB with a huge
        # spread cannot make every WR look calm by comparison.
        players = [
            mk_bp("Wr Low", "WR", adp=10.0, adp_spread=1.0),
            mk_bp("Wr High", "WR", adp=20.0, adp_spread=4.0),
            mk_bp("Rb Low", "RB", adp=15.0, adp_spread=30.0),
            mk_bp("Rb High", "RB", adp=25.0, adp_spread=90.0),
        ]
        vol = edge.volatility_map(_board(players))
        assert vol["wr low:WR"] == 0.0
        assert vol["wr high:WR"] == 1.0
        assert vol["rb low:RB"] == 0.0
        assert vol["rb high:RB"] == 1.0

    def test_players_without_spread_are_absent(self):
        players = [
            mk_bp("Has", "WR", adp=10.0, adp_spread=3.0),
            mk_bp("Lacks", "WR", adp=20.0, adp_spread=None),
        ]
        vol = edge.volatility_map(_board(players))
        assert "lacks:WR" not in vol
        assert edge.bonus(players[1], edge.build_context(_board(players), [], 10), _spicy()) >= 0.0


class TestStacking:
    def _ctx(self, roster, round_=8):
        return edge.build_context(_board(roster), roster, round_)

    def test_pass_catcher_matches_rostered_qb(self):
        qb = mk_bp("My Qb", "QB", team="BUF")
        ctx = self._ctx([qb])
        assert edge.stack_match(mk_bp("Their Wr", "WR", team="BUF"), ctx) is True
        assert edge.stack_match(mk_bp("Their Te", "TE", team="BUF"), ctx) is True

    def test_qb_matches_rostered_pass_catcher(self):
        wr = mk_bp("My Wr", "WR", team="CIN")
        ctx = self._ctx([wr])
        assert edge.stack_match(mk_bp("Their Qb", "QB", team="CIN"), ctx) is True

    def test_different_team_does_not_stack(self):
        ctx = self._ctx([mk_bp("My Qb", "QB", team="BUF")])
        assert edge.stack_match(mk_bp("Other Wr", "WR", team="KC"), ctx) is False

    def test_running_back_is_not_a_stack(self):
        # An RB shares the offense but not the scoring event, so pairing him
        # with the QB does not correlate outcomes the way a receiver does.
        ctx = self._ctx([mk_bp("My Qb", "QB", team="BUF")])
        assert edge.stack_match(mk_bp("Their Rb", "RB", team="BUF"), ctx) is False

    def test_blank_team_never_stacks(self):
        # Defenses come through with an empty team field; an empty string must
        # not match another empty string and invent a stack.
        ctx = self._ctx([mk_bp("My Qb", "QB", team="")])
        assert edge.stack_match(mk_bp("No Team", "WR", team=""), ctx) is False


class TestArbitrage:
    def test_positive_when_market_drafts_later_than_our_rank(self):
        assert edge.arbitrage_picks(mk_bp("X", "WR", adp=60.0, rank=20)) == 40.0

    def test_negative_when_market_reaches(self):
        assert edge.arbitrage_picks(mk_bp("X", "WR", adp=10.0, rank=40)) == -30.0

    def test_zero_without_adp(self):
        assert edge.arbitrage_picks(mk_bp("X", "WR", adp=None, rank=40)) == 0.0

    def test_clamped_both_directions(self):
        # Beyond the cap it is far likelier to be a bad merge or a stale ADP
        # than a real edge, so the signal saturates rather than dominating.
        assert edge.arbitrage_picks(mk_bp("X", "WR", adp=500.0, rank=1)) == 60.0
        assert edge.arbitrage_picks(mk_bp("X", "WR", adp=1.0, rank=500)) == -60.0


class TestScoringEdge:
    def test_zero_without_league_scoring(self):
        # mk_bp defaults points_fp to mirror points -- exactly what a real
        # board with no league.yml produces (see board.apply_league_scoring).
        assert edge.scoring_edge(mk_bp("X", "QB", points=300.0)) == 0.0

    def test_positive_when_league_pays_more_than_consensus(self):
        bp = mk_bp("X", "DEF", points=140.0, points_fp=120.4)
        assert edge.scoring_edge(bp) == pytest.approx(19.6)

    def test_negative_when_league_pays_less(self):
        bp = mk_bp("X", "QB", points=360.0, points_fp=372.0)
        assert edge.scoring_edge(bp) == pytest.approx(-12.0)

    def test_clamped_both_directions(self):
        big_gain = mk_bp("X", "DEF", points=500.0, points_fp=100.0)
        big_loss = mk_bp("X", "DEF", points=0.0, points_fp=500.0)
        assert edge.scoring_edge(big_gain) == edge._MAX_SCORING_EDGE
        assert edge.scoring_edge(big_loss) == -edge._MAX_SCORING_EDGE

    def test_weight_is_a_noop_at_zero(self):
        cfg = Config()  # scoring_arbitrage_weight defaults to 0.0
        players = _with_spread([
            mk_bp("Underpaid", "WR", points=150.0, points_fp=100.0, adp=60.0, rank=10, adp_spread=9.0),
        ])
        ctx = edge.build_context(_board(players), [], round_=8)
        assert edge.bonus(players[0], ctx, cfg) == edge.bonus(
            mk_bp("Underpaid", "WR", points=150.0, points_fp=150.0, adp=60.0, rank=10, adp_spread=9.0),
            ctx, cfg,
        )

    def test_positive_weight_lifts_underpaid_player(self):
        cfg = _spicy(scoring_arbitrage_weight=0.3)
        underpaid = mk_bp("Underpaid", "WR", points=150.0, points_fp=100.0)
        fairly_paid = mk_bp("Fair", "WR", points=150.0, points_fp=150.0)
        players = _with_spread([underpaid, fairly_paid])
        board = _board(players)
        ctx = edge.build_context(board, [], round_=8)
        assert edge.bonus(underpaid, ctx, cfg) > edge.bonus(fairly_paid, ctx, cfg)

    def test_reason_surfaced_only_when_weight_set_and_material(self):
        players = _with_spread([mk_bp("Underpaid", "WR", points=150.0, points_fp=100.0)])
        ctx = edge.build_context(_board(players), [], round_=8)

        cfg_on = _spicy(scoring_arbitrage_weight=0.3)
        assert any("pays" in r for r in edge.reasons(players[0], ctx, cfg_on))

        cfg_off = _spicy(scoring_arbitrage_weight=0.0)
        assert not any("pays" in r or "docks" in r for r in edge.reasons(players[0], ctx, cfg_off))


class TestBonusIsOffByDefault:
    def test_stock_config_is_an_exact_noop(self):
        # The single most important property: with default weights the edge
        # layer must contribute exactly zero, so the board behaves as a plain
        # value-over-replacement board and every pre-existing test holds.
        cfg = Config()
        players = [
            mk_bp("Stacky", "WR", team="BUF", adp=60.0, rank=10, adp_spread=9.0, upside=95.0),
            mk_bp("My Qb", "QB", team="BUF"),
        ]
        board = _board(players)
        ctx = edge.build_context(board, [players[1]], round_=14)
        assert edge.bonus(players[0], ctx, cfg) == 0.0
        assert edge.reasons(players[0], ctx, cfg) == []

    def test_spicy_config_is_not_a_noop(self):
        cfg = _spicy()
        players = [
            mk_bp("Stacky", "WR", team="BUF", adp=60.0, rank=10, adp_spread=9.0, upside=95.0),
            mk_bp("My Qb", "QB", team="BUF"),
        ]
        board = _board(_with_spread(players))
        ctx = edge.build_context(board, [players[1]], round_=14)
        assert edge.bonus(players[0], ctx, cfg) > 0.0

    def test_below_replacement_players_get_nothing(self):
        # A player 150 points below replacement is not a real option, so
        # "high upside" is not a reason to draft him — deep bench players have
        # the widest ADP disagreement precisely because nobody cares.
        cfg = _spicy()
        junk = mk_bp("Junk", "WR", team="BUF", points=-150.0, adp_spread=99.0, upside=99.0)
        qb = mk_bp("My Qb", "QB", team="BUF")
        board = _board(_with_spread([junk, qb]))
        ctx = edge.build_context(board, [qb], round_=14)
        assert edge.bonus(junk, ctx, cfg) == 0.0

    def test_flat_field_still_gets_a_bonus(self):
        # When every option scores the same, the spread is zero and a strictly
        # proportional bonus would vanish — silencing the edge layer in exactly
        # the late rounds where it is the only thing with an opinion left.
        # The floor keeps it alive, small: upside breaks a tie between equals.
        cfg = _spicy()
        players = [mk_bp(f"Same{i}", "WR", points=50.0, adp_spread=float(i)) for i in range(6)]
        ctx = edge.build_context(_board(players), [], round_=14)
        assert ctx.scale == pytest.approx(1.0)
        assert edge.bonus(players[-1], ctx, cfg) > 0.0

    def test_bonus_stays_proportionate_to_what_is_at_stake(self):
        # The same player and config must earn far more when the pick actually
        # matters than when it barely does. This is the property that stops a
        # bonus calibrated for round 1 from taking over round 12.
        cfg = _spicy()
        target = mk_bp("Target", "WR", points=100.0, adp_spread=9.0, upside=90.0)
        flat = [target] + [mk_bp(f"Same{i}", "WR", points=100.0, adp_spread=1.0) for i in range(8)]
        wide = [target] + [
            mk_bp(f"Down{i}", "WR", points=100.0 - i * 12.0, adp_spread=1.0) for i in range(8)
        ]
        b_flat = edge.bonus(target, edge.build_context(_board(flat), [], 14), cfg)
        b_wide = edge.bonus(target, edge.build_context(_board(wide), [], 14), cfg)
        assert b_wide > b_flat * 10


class TestBonusRoundDependence:
    def _setup(self):
        players = [
            mk_bp("Boomer", "WR", team="LAR", adp=80.0, rank=80, adp_spread=9.0, upside=90.0),
            mk_bp("Steady", "WR", team="LAR", adp=80.0, rank=80, adp_spread=1.0, upside=None),
        ]
        return _board(_with_spread(players)), players

    def test_upside_is_ignored_early_and_paid_late(self):
        cfg = _spicy()
        board, players = self._setup()
        early = edge.bonus(players[0], edge.build_context(board, [], 1), cfg)
        late = edge.bonus(players[0], edge.build_context(board, [], 12), cfg)
        assert early == pytest.approx(0.0, abs=1e-9)
        assert late > 0.0

    def test_high_variance_player_overtakes_steady_one_late(self):
        # The whole point of the ramp: identical on paper in round 1, and the
        # boom/bust player is clearly preferred once the ramp opens up.
        cfg = _spicy()
        board, players = self._setup()
        ctx_early = edge.build_context(board, [], 1)
        ctx_late = edge.build_context(board, [], 12)
        assert edge.bonus(players[0], ctx_early, cfg) == pytest.approx(
            edge.bonus(players[1], ctx_early, cfg), abs=1e-9
        )
        assert edge.bonus(players[0], ctx_late, cfg) > edge.bonus(players[1], ctx_late, cfg)


class TestReasons:
    def test_intel_note_comes_first(self):
        cfg = _spicy()
        bp = mk_bp("X", "WR", team="LAR", intel_note="took over the slot in camp")
        ctx = edge.build_context(_board([bp]), [], 10)
        assert edge.reasons(bp, ctx, cfg)[0] == "took over the slot in camp"

    def test_stack_reason_names_the_team(self):
        cfg = _spicy()
        qb = mk_bp("My Qb", "QB", team="BUF")
        wr = mk_bp("Their Wr", "WR", team="BUF")
        ctx = edge.build_context(_board([qb, wr]), [qb], 10)
        assert any("BUF" in r and "stacks" in r for r in edge.reasons(wr, ctx, cfg))

    def test_disabled_terms_stay_silent(self):
        # A term with zero weight must not explain itself — otherwise the UI
        # claims credit for something that did not affect the ranking.
        cfg = _spicy(stack_bonus=0.0)
        qb = mk_bp("My Qb", "QB", team="BUF")
        wr = mk_bp("Their Wr", "WR", team="BUF")
        ctx = edge.build_context(_board([qb, wr]), [qb], 10)
        assert not any("stacks" in r for r in edge.reasons(wr, ctx, cfg))


# --- Whole-draft behaviour -------------------------------------------------
#
# The unit tests above prove each term computes what it claims. These prove the
# terms actually change what you would draft, which is the only reason any of
# it exists.

import random  # noqa: E402

from ffbot.board import load_board  # noqa: E402
from ffbot.draft import DraftState, recommend  # noqa: E402

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


# Per-position point ranges roughly matching real PPR scoring. Uniform ranges
# across all positions would let a kicker out-project a WR1, and the simulation
# would draft kickers in round 1 — which tells you nothing about the edge terms.
_POINT_RANGE = {
    "QB": (200.0, 380.0),
    "RB": (30.0, 340.0),
    "WR": (30.0, 340.0),
    "TE": (20.0, 250.0),
    "K": (90.0, 160.0),
    "DEF": (60.0, 140.0),
}


def _write_board_csv(tmp_path, rng):
    """A board with real ADP spread, so the volatility term has something to bite on."""
    path = tmp_path / "board.csv"
    lines = ["Player,Team,POS,BYE,FPTS,AVG,ESPN,Sleeper,CBS\n"]
    teams = ["BUF", "CIN", "LAR", "KC", "SF", "DET"]
    for pos, n in {"QB": 24, "RB": 60, "WR": 72, "TE": 30, "K": 14, "DEF": 14}.items():
        lo, hi = _POINT_RANGE[pos]
        for i in range(n):
            adp = round(rng.uniform(1, 240), 1)
            jitter = rng.uniform(0, 30)
            sites = [max(1.0, adp + rng.uniform(-jitter, jitter)) for _ in range(3)]
            lines.append(
                f"{pos}{i},{teams[i % len(teams)]},{pos},{rng.randint(5, 14)},"
                f"{round(rng.uniform(lo, hi), 1)},{adp},"
                f"{sites[0]:.1f},{sites[1]:.1f},{sites[2]:.1f}\n"
            )
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _simulate(board, cfg, rounds=15):
    """My picks follow the top recommendation; opponents draft by ADP.

    Opponents must NOT consume my recommendations: `recommend()` applies my
    caps, targets, and forced-fill against *my* roster, and routing all twelve
    teams through it once had opponents forced onto defenses until the pool
    ran dry. Real opponents behave like the market, which is what ADP is.
    """
    state = DraftState(
        board=board, num_teams=12, my_slot=4, rounds=rounds,
        roster_positions=STANDARD_LAYOUT,
    )
    my_picks = set(state.my_picks())
    by_adp = sorted(
        (bp for bp in board.players if bp.adp is not None), key=lambda b: (b.adp, b.name)
    )
    cursor = 0
    for pick in range(1, rounds * 12 + 1):
        if pick in my_picks:
            recs = recommend(state, cfg, limit=1)
            if not recs:
                break
            state.record(recs[0].player.key, mine=True)
        else:
            while cursor < len(by_adp) and by_adp[cursor].key in state.taken_keys():
                cursor += 1
            if cursor >= len(by_adp):
                break
            state.record(by_adp[cursor].key, mine=False)
    return [bp.key for bp in state.my_roster()]


class TestVanillaVersusSpicy:
    def _boards(self, tmp_path):
        rng = random.Random(11)
        path = _write_board_csv(tmp_path, rng)
        vanilla = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12))
        spicy = _spicy(num_teams=12)
        spicy.roster_positions = STANDARD_LAYOUT
        return (
            load_board([path], STANDARD_LAYOUT, 12, vanilla),
            vanilla,
            load_board([path], STANDARD_LAYOUT, 12, spicy),
            spicy,
        )

    def test_rosters_actually_diverge(self, tmp_path):
        # If these come out identical the weights are decorative.
        v_board, v_cfg, s_board, s_cfg = self._boards(tmp_path)
        assert _simulate(v_board, v_cfg) != _simulate(s_board, s_cfg)

    def test_both_rosters_are_legal_and_complete(self, tmp_path):
        # Being contrarian must not mean drafting an illegal or short roster.
        v_board, v_cfg, s_board, s_cfg = self._boards(tmp_path)
        for board, cfg in ((v_board, v_cfg), (s_board, s_cfg)):
            roster = _simulate(board, cfg)
            assert len(roster) == 15
            assert len(set(roster)) == 15

    def test_variance_terms_do_not_move_the_early_rounds(self, tmp_path):
        # Floor early, ceiling late: with the risk ramp at 0 for rounds 1-2 the
        # variance terms cannot change those picks.
        #
        # Arbitrage is deliberately excluded here rather than ramped, because it
        # is not a risk term — "the market undervalues him" is just as true in
        # round 1 as in round 12, so it legitimately moves early picks and would
        # mask the property being tested.
        rng = random.Random(11)
        path = _write_board_csv(tmp_path, rng)
        vanilla = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12))
        spicy = _spicy(num_teams=12, arbitrage_weight=0.0)
        spicy.roster_positions = STANDARD_LAYOUT

        v_board = load_board([path], STANDARD_LAYOUT, 12, vanilla)
        s_board = load_board([path], STANDARD_LAYOUT, 12, spicy)
        assert _simulate(v_board, vanilla, rounds=2) == _simulate(s_board, spicy, rounds=2)


class TestDepthDecay:
    """`draft._depth_factors` — how fast a backup loses value once covered."""

    def test_disabled_by_default(self):
        # depth_decay defaults to 1.0, so a stock config prices the seventh
        # backup exactly like the first, as it always did.
        from ffbot.draft import _depth_factors

        roster = [mk_bp(f"Te{i}", "TE") for i in range(6)]
        factors = _depth_factors(roster, STANDARD_LAYOUT, Config())
        assert factors["TE"] == 1.0

    def test_no_penalty_until_starters_are_covered(self):
        # The layout starts 1 TE plus a flex that accepts one, so the first two
        # are starters and neither should be discounted.
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5)
        assert _depth_factors([], STANDARD_LAYOUT, cfg).get("TE", 1.0) == 1.0
        one = _depth_factors([mk_bp("Te0", "TE")], STANDARD_LAYOUT, cfg)
        assert one["TE"] == 1.0

    def test_halves_for_each_backup_beyond_starters(self):
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5)
        for already, expected in ((2, 0.5), (3, 0.25), (4, 0.125)):
            roster = [mk_bp(f"Te{i}", "TE") for i in range(already)]
            assert _depth_factors(roster, STANDARD_LAYOUT, cfg)["TE"] == expected

    def test_factor_describes_the_candidate_not_the_current_roster(self):
        # An off-by-one here is silent and costly: measuring the roster as it
        # stands charges the third tight end the second one's rate, and the
        # decay never actually bites.
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5)
        roster = [mk_bp("Te0", "TE"), mk_bp("Te1", "TE")]
        assert _depth_factors(roster, STANDARD_LAYOUT, cfg)["TE"] == 0.5

    def test_positions_are_independent(self):
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5)
        roster = [mk_bp(f"Te{i}", "TE") for i in range(4)] + [mk_bp("Qb0", "QB")]
        factors = _depth_factors(roster, STANDARD_LAYOUT, cfg)
        assert factors["TE"] == 0.125
        assert factors["QB"] == 0.5  # 1 QB slot, so the second QB is already surplus


class TestPositionTargets:
    """Target-aware depth decay plus the deficit-urgency balance term."""

    def test_target_overrides_starters_for_decay(self):
        # Flex makes TE starters = 2, so starter-based surplus never
        # discounts a 2nd TE. A target of 1 is the user saying "really only
        # one" — the 2nd must be decayed.
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5, position_targets={"TE": 1})
        roster = [mk_bp("Te0", "TE")]
        assert _depth_factors(roster, STANDARD_LAYOUT, cfg)["TE"] == 0.5

    def test_target_can_also_loosen_decay(self):
        # RB starters = 3 (2 + flex), so the 4th RB is decayed by default; a
        # target of 5 says depth there is wanted, so decay starts at the 6th.
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5, position_targets={"RB": 5})
        roster = [mk_bp(f"Rb{i}", "RB") for i in range(4)]
        assert _depth_factors(roster, STANDARD_LAYOUT, cfg)["RB"] == 1.0

    def test_no_targets_is_the_old_behaviour(self):
        from ffbot.draft import _depth_factors

        cfg = _spicy(depth_decay=0.5)
        roster = [mk_bp("Te0", "TE")]
        assert _depth_factors(roster, STANDARD_LAYOUT, cfg)["TE"] == 1.0


class TestBalance:
    def _ctx(self, balance, scale=10.0):
        return edge.EdgeContext(round_=8, scale=scale, balance=balance)

    def test_lifts_the_deficit_position(self):
        cfg = _spicy(balance_weight=0.3)
        rb = mk_bp("Some Rb", "RB")
        wr = mk_bp("Some Wr", "WR")
        ctx = self._ctx({"RB": 0.6})
        assert edge.bonus(rb, ctx, cfg) > edge.bonus(wr, ctx, cfg)

    def test_uniform_within_a_position(self):
        # Balance reorders positions against each other, never players within
        # one — both RBs get exactly the same lift.
        cfg = _spicy(balance_weight=0.3)
        good = mk_bp("Good Rb", "RB", points=200.0)
        bad = mk_bp("Bad Rb", "RB", points=50.0)
        ctx = self._ctx({"RB": 0.6})
        assert edge.bonus(good, ctx, cfg) == edge.bonus(bad, ctx, cfg)

    def test_applies_outside_the_contender_pool(self):
        # Late-draft: the position you are short of may have nobody left in
        # the top-25 pool; the deficit must still pull picks toward it.
        cfg = _spicy(balance_weight=0.3)
        rb = mk_bp("Deep Rb", "RB")
        ctx = edge.EdgeContext(
            round_=12, scale=10.0, balance={"RB": 0.8},
            contenders=frozenset({"someone else:WR"}),
        )
        assert edge.bonus(rb, ctx, cfg) > 0.0

    def test_zero_weight_is_a_noop(self):
        cfg = _spicy(balance_weight=0.0)
        rb = mk_bp("Some Rb", "RB")
        ctx = self._ctx({"RB": 1.0})
        assert edge.bonus(rb, ctx, cfg) == 0.0

    def test_kickers_and_defenses_excluded(self):
        cfg = _spicy(balance_weight=0.3)
        k = mk_bp("Some K", "K")
        ctx = self._ctx({"K": 1.0})
        assert edge.bonus(k, ctx, cfg) == 0.0

    def test_urgent_deficit_gets_a_reason(self):
        cfg = _spicy(balance_weight=0.3)
        rb = mk_bp("Some Rb", "RB")
        assert any(
            "running out of picks" in r
            for r in edge.reasons(rb, self._ctx({"RB": 0.6}), cfg)
        )
        # Below the urgency threshold it stays quiet — early-draft deficits
        # apply to every position at once and would be pure noise.
        assert not any(
            "running out" in r for r in edge.reasons(rb, self._ctx({"RB": 0.3}), cfg)
        )


class TestCompositionWithTargets:
    def test_simulated_draft_lands_on_the_configured_shape(self, tmp_path):
        # The whole point of targets: a full sim should produce the shape the
        # user asked for — 1-2 QB, 1-2 TE, 1 K, 1 DEF, RB/WR roughly even.
        rng = random.Random(23)
        path = _write_board_csv(tmp_path, rng)
        cfg = _spicy(
            num_teams=12,
            balance_weight=0.3,
            position_caps={"QB": 2, "TE": 2, "K": 1, "DEF": 1},
            position_targets={"QB": 1, "TE": 1, "RB": 5, "WR": 5},
            export_defer_positions=["K", "DEF"],
        )
        cfg.roster_positions = STANDARD_LAYOUT
        board = load_board([path], STANDARD_LAYOUT, 12, cfg)
        roster = _simulate(board, cfg, rounds=14)

        from collections import Counter
        counts = Counter(k.rsplit(":", 1)[1] for k in roster)
        assert counts["K"] == 1 and counts["DEF"] == 1
        assert 1 <= counts["QB"] <= 2
        assert 1 <= counts["TE"] <= 2
        assert abs(counts["RB"] - counts["WR"]) <= 1


class TestAvailabilityRisk:
    """The one intel signal that subtracts — factual availability only."""

    def _pair(self):
        # Identical players except one carries a researched availability risk.
        healthy = mk_bp("Healthy Guy", "WR", points=100.0, adp=50.0)
        risky = mk_bp("Risky Guy", "WR", points=100.0, adp=50.0, availability_risk=60.0)
        return healthy, risky

    def test_risk_subtracts(self):
        cfg = _spicy(risk_weight=0.35)
        healthy, risky = self._pair()
        board = _board(_with_spread([healthy, risky]))
        ctx = edge.build_context(board, [], round_=6)
        assert edge.bonus(risky, ctx, cfg) < edge.bonus(healthy, ctx, cfg)

    def test_unramped_bites_in_round_one(self):
        # Upside waits for the risk ramp; availability risk must not — a
        # suspension costs the same games whether drafted 1st or 140th.
        cfg = _spicy(risk_weight=0.35)
        healthy, risky = self._pair()
        board = _board(_with_spread([healthy, risky]))
        ctx = edge.build_context(board, [], round_=1)
        assert edge.bonus(risky, ctx, cfg) < edge.bonus(healthy, ctx, cfg)

    def test_zero_weight_is_a_noop(self):
        cfg = _spicy(risk_weight=0.0)
        healthy, risky = self._pair()
        board = _board(_with_spread([healthy, risky]))
        ctx = edge.build_context(board, [], round_=6)
        assert edge.bonus(risky, ctx, cfg) == edge.bonus(healthy, ctx, cfg)

    def test_opinion_flags_still_move_nothing(self):
        # Regression on the factual/speculative line: a vegas-fade flag with
        # no risk score must leave the number untouched.
        cfg = _spicy(risk_weight=0.35)
        plain = mk_bp("Plain", "WR", points=100.0, adp=50.0)
        faded = mk_bp(
            "Faded", "WR", points=100.0, adp=50.0,
            intel_flags=("vegas-fade", "camp-struggles"), intel_note="bad vibes",
        )
        board = _board(_with_spread([plain, faded]))
        ctx = edge.build_context(board, [], round_=6)
        assert edge.bonus(faded, ctx, cfg) == edge.bonus(plain, ctx, cfg)


class TestExportIntel:
    def _players(self):
        return [
            mk_bp("Steady Star", "WR", points=200.0, vor=50.0),
            mk_bp("Upside Guy", "WR", points=195.0, vor=45.0, upside=90.0),
            mk_bp("Suspended Guy", "WR", points=198.0, vor=48.0, availability_risk=70.0),
        ]

    def test_zero_scale_is_pure_vor_order(self):
        from ffbot.board import export_rankings

        cfg = Config(draft=DraftConfig(export_intel_scale=0.0))
        ranked = export_rankings(_board(self._players()), cfg)
        assert [r["name"] for r in ranked] == ["Steady Star", "Suspended Guy", "Upside Guy"]

    def test_scale_promotes_upside_and_demotes_risk(self):
        from ffbot.board import export_rankings

        # 20 pts at full score: Upside Guy 45 + 18 = 63; Suspended 48 - 14 = 34.
        cfg = Config(draft=DraftConfig(export_intel_scale=20.0))
        ranked = export_rankings(_board(self._players()), cfg)
        assert [r["name"] for r in ranked] == ["Upside Guy", "Steady Star", "Suspended Guy"]

    def test_defer_still_buries_kickers(self):
        from ffbot.board import export_rankings

        cfg = Config(
            draft=DraftConfig(
                export_intel_scale=20.0, num_teams=2, rounds=4,
                export_defer_positions=["K"],
            )
        )
        players = self._players() + [mk_bp("Great Kicker", "K", points=160.0, vor=40.0, upside=90.0)]
        ranked = export_rankings(_board(players), cfg)
        # threshold = 2 * (4-2) = 4 non-deferred before kickers, but only 3
        # exist -- the kicker still lands after every skill player.
        assert [r["name"] for r in ranked][-1] == "Great Kicker"
