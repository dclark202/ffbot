from __future__ import annotations

import pytest

from ffbot.board import (
    _finalize_board,
    apply_predictiveness_shrinkage,
    apply_rank_calibration,
    load_rank_curve,
)
from ffbot.config import Config, DraftConfig
from ffbot.history.calibration import _monotone, curve_value

LAYOUT = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "BN": 6}


def _rows(points_by_pos: dict[str, list[float]]) -> list[dict]:
    out = []
    for pos, points in points_by_pos.items():
        for i, pts in enumerate(points):
            out.append(
                {"name": f"{pos}{i}", "team": "XXX", "position": pos, "bye": 7, "points": pts,
                 "adp": float(i + 1), "adp_stdev": None, "adp_spread": None}
            )
    return out


class TestMonotone:
    def test_already_decreasing_is_unchanged(self):
        assert _monotone([10.0, 8.0, 5.0]) == [10.0, 8.0, 5.0]

    def test_an_upward_blip_is_clamped_not_dropped(self):
        # A freak season at one rank must not make the curve say the 3rd-best
        # player outscores the 2nd -- that would let `apply_rank_calibration`
        # invert two players purely from fitting noise.
        assert _monotone([10.0, 5.0, 9.0, 3.0]) == [10.0, 5.0, 5.0, 3.0]

    def test_empty(self):
        assert _monotone([]) == []


class TestCurveValue:
    def test_within_range_is_the_fitted_value(self):
        assert curve_value([30.0, 20.0, 10.0], 2) == 20.0

    def test_past_the_end_decays_along_the_final_slope(self):
        # Not flat: a flat tail prices rank 61 and rank 200 identically,
        # re-creating the "everyone past the cut is worth the same" collapse
        # this repo already fixed once in the bench-depth term.
        assert curve_value([30.0, 20.0, 10.0], 4) == pytest.approx(0.0)
        assert curve_value([30.0, 25.0, 20.0], 5) == pytest.approx(10.0)

    def test_never_negative(self):
        assert curve_value([10.0, 5.0], 20) == 0.0

    def test_empty_curve_raises(self):
        with pytest.raises(ValueError):
            curve_value([], 1)


class TestLeakageGuard:
    """Fitting on the season being graded is the single most likely way to
    fake a good result here -- the natural fitting window and the natural
    grading window are the same four seasons -- so it raises rather than
    warns, matching `ecr_projections`' existing precedent."""

    def test_overlapping_fit_season_raises(self):
        from ffbot.history.calibration import rank_points_curve

        with pytest.raises(ValueError, match="look-ahead leakage"):
            rank_points_curve(fit_seasons=(2021, 2022), exclude_season=2022)

    def test_weekly_curve_enforces_the_same_rule(self):
        from ffbot.history.calibration import weekly_rank_points_curve

        with pytest.raises(ValueError, match="look-ahead leakage"):
            weekly_rank_points_curve(fit_seasons=(2023,), exclude_season=2023)

    def test_empty_fit_set_raises_rather_than_returning_nothing(self):
        from ffbot.history.calibration import rank_points_curve

        with pytest.raises(ValueError):
            rank_points_curve(fit_seasons=(), exclude_season=None)


class TestApplyRankCalibration:
    CURVE = {"QB": [400.0, 300.0, 200.0], "RB": [350.0, 250.0, 150.0]}

    def test_zero_blend_is_an_exact_no_op(self):
        rows = _rows({"QB": [300.0, 290.0, 280.0]})
        before = [r["points"] for r in rows]
        apply_rank_calibration(rows, self.CURVE, 0.0)
        assert [r["points"] for r in rows] == before

    def test_empty_curve_is_an_exact_no_op(self):
        rows = _rows({"QB": [300.0, 290.0, 280.0]})
        before = [r["points"] for r in rows]
        apply_rank_calibration(rows, {}, 1.0)
        assert [r["points"] for r in rows] == before

    def test_full_blend_takes_the_curve_value_at_each_rank(self):
        rows = _rows({"QB": [300.0, 290.0, 280.0]})
        apply_rank_calibration(rows, self.CURVE, 1.0)
        assert [r["points"] for r in rows] == [400.0, 300.0, 200.0]

    def test_partial_blend_interpolates(self):
        rows = _rows({"QB": [300.0, 290.0, 280.0]})
        apply_rank_calibration(rows, self.CURVE, 0.5)
        assert [r["points"] for r in rows] == pytest.approx([350.0, 295.0, 240.0])

    def test_never_reorders_players_within_a_position(self):
        # The guarantee that makes this safe: it re-prices the GAPS between
        # players, never who outranks whom.
        rows = _rows({"QB": [300.0, 299.0, 298.0, 297.0]})
        original = [r["name"] for r in sorted(rows, key=lambda r: -r["points"])]
        apply_rank_calibration(rows, {"QB": [400.0, 300.0, 200.0, 100.0]}, 1.0)
        assert [r["name"] for r in sorted(rows, key=lambda r: -r["points"])] == original

    def test_positions_absent_from_the_curve_are_untouched(self):
        rows = _rows({"QB": [300.0], "K": [120.0]})
        apply_rank_calibration(rows, self.CURVE, 1.0)
        k = next(r for r in rows if r["position"] == "K")
        assert k["points"] == 120.0

    def test_rows_beyond_the_curve_still_get_a_value(self):
        rows = _rows({"QB": [300.0, 290.0, 280.0, 270.0, 260.0]})
        apply_rank_calibration(rows, self.CURVE, 1.0)
        assert all(r["points"] is not None for r in rows)
        assert all(r["points"] >= 0.0 for r in rows)


class TestBoardIntegration:
    """Calibration must run BEFORE replacement level and tiers, or the board
    ends up with points on one scale and replacement/tiers on another --
    the same split-brain `apply_league_scoring` documents."""

    def _cfg(self, tmp_path, blend: float):
        import json

        curve_path = tmp_path / "curves.json"
        curve_path.write_text(
            json.dumps({"curve": {
                "QB": [400.0 - 20.0 * i for i in range(30)],
                "RB": [350.0 - 10.0 * i for i in range(30)],
                "WR": [340.0 - 9.0 * i for i in range(30)],
                "TE": [240.0 - 8.0 * i for i in range(30)],
            }}),
            encoding="utf-8",
        )
        return Config(
            roster_positions=LAYOUT,
            draft=DraftConfig(
                num_teams=12, rank_calibration=str(curve_path), rank_calibration_blend=blend
            ),
        )

    def _rows_for_board(self):
        return _rows({
            "QB": [300.0 - 2.0 * i for i in range(30)],
            "RB": [290.0 - 3.0 * i for i in range(30)],
            "WR": [285.0 - 3.0 * i for i in range(30)],
            "TE": [230.0 - 4.0 * i for i in range(30)],
        })

    def test_replacement_level_reflects_the_calibrated_points(self, tmp_path):
        cfg = self._cfg(tmp_path, 1.0)
        board = _finalize_board(self._rows_for_board(), LAYOUT, 12, cfg)
        # Every player's points come from the curve, so replacement must be
        # a curve value too -- not a leftover from the uncalibrated scale.
        qb_points = sorted((bp.points for bp in board.players if bp.position == "QB"), reverse=True)
        assert board.replacement["QB"] in qb_points

    def test_vor_is_consistent_with_calibrated_points(self, tmp_path):
        cfg = self._cfg(tmp_path, 1.0)
        board = _finalize_board(self._rows_for_board(), LAYOUT, 12, cfg)
        for bp in board.players:
            assert bp.vor == pytest.approx(bp.points - board.replacement[bp.position])

    def test_zero_blend_board_is_identical_to_no_calibration(self, tmp_path):
        off = Config(roster_positions=LAYOUT, draft=DraftConfig(num_teams=12))
        blended = self._cfg(tmp_path, 0.0)
        a = _finalize_board(self._rows_for_board(), LAYOUT, 12, off)
        b = _finalize_board(self._rows_for_board(), LAYOUT, 12, blended)
        assert [(p.key, p.points, p.vor, p.rank) for p in a.players] == [
            (p.key, p.points, p.vor, p.rank) for p in b.players
        ]

    def test_calibration_widens_the_qb_spread(self, tmp_path):
        # The actual point of the feature: QB1's edge over replacement grows,
        # because the projections' QB curve is too flat.
        off = Config(roster_positions=LAYOUT, draft=DraftConfig(num_teams=12))
        on = self._cfg(tmp_path, 1.0)
        a = _finalize_board(self._rows_for_board(), LAYOUT, 12, off)
        b = _finalize_board(self._rows_for_board(), LAYOUT, 12, on)
        best_qb_a = max(p.vor for p in a.players if p.position == "QB")
        best_qb_b = max(p.vor for p in b.players if p.position == "QB")
        assert best_qb_b > best_qb_a


class TestLoadRankCurve:
    def test_missing_file_degrades_to_empty(self, tmp_path):
        curve, prov = load_rank_curve(tmp_path / "nope.json")
        assert curve == {} and prov == {}

    def test_reads_curve_and_keeps_provenance_separate(self, tmp_path):
        import json

        p = tmp_path / "c.json"
        p.write_text(
            json.dumps({"curve": {"QB": [1.0, 2.0]}, "fit_seasons": [2021], "weekly_curve": {"QB": [9.0]}}),
            encoding="utf-8",
        )
        curve, prov = load_rank_curve(p)
        assert curve == {"QB": [1.0, 2.0]}
        assert prov["fit_seasons"] == [2021]
        # The weekly curve rides along in provenance -- that is how
        # `report.load_everything` reaches it for the in-season path.
        assert prov["weekly_curve"] == {"QB": [9.0]}


class TestPredictivenessShrinkage:
    """`predictiveness` measures how much of a position's projected spread
    survives contact with reality; shrinking by it is what lets K/DEF sink
    on their own arithmetic instead of being suppressed by a round gate."""

    FACTORS = {"QB": 0.4, "K": 0.2, "WR": 0.42}

    def _rows(self):
        return _rows({"K": [160.0, 140.0, 120.0, 100.0], "WR": [300.0, 250.0, 200.0, 150.0]})

    def test_zero_blend_is_an_exact_no_op(self):
        rows = self._rows()
        before = [r["points"] for r in rows]
        apply_predictiveness_shrinkage(rows, self.FACTORS, 0.0)
        assert [r["points"] for r in rows] == before

    def test_empty_factors_is_an_exact_no_op(self):
        rows = self._rows()
        before = [r["points"] for r in rows]
        apply_predictiveness_shrinkage(rows, {}, 1.0)
        assert [r["points"] for r in rows] == before

    def test_spread_shrinks_by_the_factor_about_the_mean(self):
        rows = self._rows()
        apply_predictiveness_shrinkage(rows, self.FACTORS, 1.0)
        ks = sorted((r["points"] for r in rows if r["position"] == "K"), reverse=True)
        # mean 130, factor 0.2 -> 130 + 0.2*(x-130)
        assert ks == pytest.approx([136.0, 132.0, 128.0, 124.0])

    def test_a_low_signal_position_shrinks_harder_than_a_high_one(self):
        rows = self._rows()
        before = {r["name"]: r["points"] for r in rows}
        apply_predictiveness_shrinkage(rows, self.FACTORS, 1.0)
        after = {r["name"]: r["points"] for r in rows}
        k_kept = (after["K0"] - 130.0) / (before["K0"] - 130.0)
        wr_kept = (after["WR0"] - 225.0) / (before["WR0"] - 225.0)
        assert k_kept < wr_kept

    def test_never_reorders_within_a_position(self):
        # Within a position only. ACROSS positions the order is expected to
        # change -- re-pricing one position's spread relative to another's
        # is the entire point of the feature.
        rows = self._rows()
        before = {
            pos: [r["name"] for r in sorted(
                (x for x in rows if x["position"] == pos), key=lambda r: -r["points"]
            )]
            for pos in ("K", "WR")
        }
        apply_predictiveness_shrinkage(rows, self.FACTORS, 1.0)
        for pos, order in before.items():
            after = [r["name"] for r in sorted(
                (x for x in rows if x["position"] == pos), key=lambda r: -r["points"]
            )]
            assert after == order, pos

    def test_positions_without_a_factor_are_untouched(self):
        rows = _rows({"K": [160.0, 100.0], "TE": [200.0, 100.0]})
        apply_predictiveness_shrinkage(rows, {"K": 0.2}, 1.0)
        tes = [r["points"] for r in rows if r["position"] == "TE"]
        assert tes == [200.0, 100.0]

    def test_leakage_guard_raises(self):
        from ffbot.history.calibration import predictiveness

        with pytest.raises(ValueError, match="look-ahead leakage"):
            predictiveness(fit_seasons=(2022, 2023), exclude_season=2023, cfg=Config())


class TestBoardCarriesPredictiveness:
    def test_apply_intel_preserves_every_board_field(self):
        """Regression: `apply_intel` hand-listed Board fields and silently
        dropped `scoring_residual` / `bench_replacement` / `predictiveness`
        on the way through, so any feature reading them did nothing at all
        on a board loaded via `load_board_from_config`."""
        from ffbot.board import Board
        from ffbot.intel import IntelEntry, apply_intel

        board = Board(
            players=[],
            by_key={},
            replacement={"QB": 1.0},
            starters_per_pos={"QB": 1},
            scoring_residual={"QB": 0.5},
            bench_replacement={"QB": 0.25},
            predictiveness={"QB": 0.4},
        )
        out = apply_intel(board, {"someone": IntelEntry(name="Someone", upside=50.0)})
        assert out.scoring_residual == {"QB": 0.5}
        assert out.bench_replacement == {"QB": 0.25}
        assert out.predictiveness == {"QB": 0.4}
