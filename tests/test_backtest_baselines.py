from __future__ import annotations

import random

from ffbot.backtest.baselines import (
    _greedy_consensus_lineup,
    _random_legal_lineup,
    build_baselines,
    key_by_player_id,
)
from ffbot.config import Config
from ffbot.history.index import WeekSnapshot
from tests.conftest import mk

_STANDARD = {"QB": 1, "WR": 2, "RB": 1, "TE": 1, "K": 1, "DEF": 1, "BN": 3, "IR": 1}


class TestKeyByPlayerId:
    def test_maps_enumeration_order_to_pool_key(self):
        rows = [{"key": "a:WR"}, {"key": "b:RB"}, {"key": "c:QB"}]
        assert key_by_player_id(rows) == {1: "a:WR", 2: "b:RB", 3: "c:QB"}


class TestGreedyConsensusLineup:
    def test_fills_dedicated_slots_with_highest_projected_player(self):
        players = [
            mk("Best WR", "WR", proj=20.0),
            mk("Worst WR", "WR", proj=5.0),
            mk("QB1", "QB", proj=15.0),
        ]
        roster_positions = {"QB": 1, "WR": 2, "BN": 2}
        assignments, bench = _greedy_consensus_lineup(players, roster_positions)
        started = {p.name for _slot, p in assignments}
        assert started == {"Best WR", "Worst WR", "QB1"}  # both WR slots fill; only 2 WRs exist

    def test_dedicated_slot_claims_the_top_player_before_flex_does(self):
        players = [mk("Star", "WR", proj=30.0), mk("Filler", "RB", proj=5.0)]
        roster_positions = {"WR": 1, "W/R/T": 1, "BN": 1}
        assignments, _bench = _greedy_consensus_lineup(players, roster_positions)
        by_slot = dict((slot, p.name) for slot, p in assignments)
        assert by_slot["WR"] == "Star"  # NOT claimed by the flex slot first
        assert by_slot["W/R/T"] == "Filler"

    def test_out_status_player_never_started(self):
        players = [mk("Hurt Guy", "WR", proj=99.0, status="O"), mk("Healthy", "WR", proj=1.0)]
        roster_positions = {"WR": 1, "BN": 1}
        assignments, bench = _greedy_consensus_lineup(players, roster_positions)
        started = {p.name for _slot, p in assignments}
        assert started == {"Healthy"}
        assert any(p.name == "Hurt Guy" for p in bench)

    def test_unfillable_slot_when_pool_exhausted(self):
        players = [mk("Only WR", "WR", proj=10.0)]
        roster_positions = {"WR": 2, "BN": 0}
        assignments, _bench = _greedy_consensus_lineup(players, roster_positions)
        assert len(assignments) == 1  # second WR slot has nobody left to fill it


class TestRandomLegalLineup:
    def test_every_assignment_is_slot_legal(self):
        players = [mk("A", "WR", proj=10.0), mk("B", "RB", proj=8.0), mk("C", "QB", proj=20.0)]
        roster_positions = {"QB": 1, "WR": 1, "RB": 1, "BN": 0}
        assignments, _bench = _random_legal_lineup(players, roster_positions, random.Random(1))
        from ffbot.models import slot_accepts

        for slot, p in assignments:
            assert slot_accepts(slot, p)

    def test_can_start_an_out_status_player_by_design(self):
        # Only one eligible player exists for the single WR slot, and it is
        # OUT -- a random_legal lineup must still be able to start them,
        # since this baseline deliberately ignores availability (see its
        # docstring) to serve as a pure structural floor.
        players = [mk("Hurt Guy", "WR", proj=1.0, status="O")]
        roster_positions = {"WR": 1, "BN": 0}
        assignments, _bench = _random_legal_lineup(players, roster_positions, random.Random(1))
        assert [p.name for _slot, p in assignments] == ["Hurt Guy"]

    def test_deterministic_given_seed(self):
        players = [mk(f"P{i}", "WR", proj=float(i)) for i in range(6)]
        roster_positions = {"WR": 2, "BN": 4}
        a1, b1 = _random_legal_lineup(players, roster_positions, random.Random(5))
        a2, b2 = _random_legal_lineup(players, roster_positions, random.Random(5))
        assert [p.name for _s, p in a1] == [p.name for _s, p in a2]


class TestBuildBaselines:
    def _roster_rows(self) -> list[dict]:
        return [
            {"key": "qb1:QB", "name": "QB One", "position": "QB", "team": "MIA"},
            {"key": "wr1:WR", "name": "WR One", "position": "WR", "team": "MIA"},
            {"key": "wr2:WR", "name": "WR Two", "position": "WR", "team": "MIA"},
            {"key": "rb1:RB", "name": "RB One", "position": "RB", "team": "MIA"},
            {"key": "te1:TE", "name": "TE One", "position": "TE", "team": "MIA"},
            {"key": "k1:K", "name": "K One", "position": "K", "team": "MIA"},
            {"key": "mia:DEF", "name": "MIA", "position": "DEF", "team": "MIA"},
        ]

    def test_all_five_baselines_present(self):
        cfg = Config()
        rows = self._roster_rows()
        projections = {row["key"]: 10.0 for row in rows}
        actuals = {row["key"]: 12.0 for row in rows}
        snapshot = WeekSnapshot(season=2023, week=5)
        baselines = build_baselines(rows, projections, actuals, snapshot, cfg, week=5, seed=1)
        assert set(baselines) == {"oracle", "control", "agent", "consensus", "random_legal"}

    def test_agent_equals_control_when_every_spice_weight_is_zero(self):
        # A bare Config() has spice_level=3 but every *_weight field at its
        # dataclass default of 0.0 -- adjusted_players must then be an exact
        # no-op, so `agent` and `control` should be bit-identical lineups.
        cfg = Config()
        rows = self._roster_rows()
        projections = {row["key"]: float(10 + i) for i, row in enumerate(rows)}
        actuals = {row["key"]: 5.0 for row in rows}
        snapshot = WeekSnapshot(season=2023, week=5)
        baselines = build_baselines(rows, projections, actuals, snapshot, cfg, week=5, seed=1)
        control_ids = [p.player_id for _s, p in baselines["control"].assignments]
        agent_ids = [p.player_id for _s, p in baselines["agent"].assignments]
        assert control_ids == agent_ids

    def test_oracle_uses_realized_points_not_projections(self):
        cfg = Config()
        rows = self._roster_rows()
        # Projections say WR One is much better; actuals say WR Two is --
        # oracle must follow actuals for VALUATION even though the roster
        # composition (who's eligible) is identical either way.
        projections = {row["key"]: 10.0 for row in rows}
        projections["wr1:WR"] = 50.0
        actuals = {row["key"]: 10.0 for row in rows}
        actuals["wr2:WR"] = 50.0
        snapshot = WeekSnapshot(season=2023, week=5)
        baselines = build_baselines(rows, projections, actuals, snapshot, cfg, week=5, seed=1)
        key_by_id = key_by_player_id(rows)
        oracle_keys = {key_by_id[p.player_id] for _s, p in baselines["oracle"].assignments}
        assert "wr2:WR" in oracle_keys
