from __future__ import annotations

import itertools

import pytest

from ffbot.board import BoardPlayer
from ffbot.config import Config
from ffbot.models import Player

_ids = itertools.count(1)


def mk_bp(name: str, position: str, points: float = 100.0, **kw) -> BoardPlayer:
    """Terse BoardPlayer factory. Keyword args override any field.

    Shared so that adding a field to `BoardPlayer` — which is frozen and has
    no defaults — is a one-line change here rather than an edit to every test
    module that builds one.
    """
    fields = dict(
        key=f"{name.lower()}:{position}",
        name=name,
        position=position,
        team="",
        bye_week=None,
        points=points,
        adp=None,
        adp_stdev=None,
        adp_spread=None,
        platform_id=None,
        tier=1,
        vor=points,
        rank=0,
        availability_risk=None,
        # Mirrors `points` by default, matching what `board.load_board`
        # always produces on a run with no league.yml (points_fp == points,
        # points_source == "consensus") — so `edge.scoring_edge` is exactly
        # 0.0 on any fixture that doesn't deliberately set these to test
        # league scoring, not merely a fixture that forgot to.
        points_fp=points,
        points_source="consensus",
        points_flags=(),
    )
    fields.update(kw)
    return BoardPlayer(**fields)


def mk(
    name: str,
    positions: str | list[str],
    slot: str = "BN",
    proj: float | None = None,
    **kw,
) -> Player:
    """Terse Player factory for fixtures. `positions` may be 'WR' or 'WR,RB'."""
    if isinstance(positions, str):
        positions = [p.strip() for p in positions.split(",")]
    return Player(
        player_id=next(_ids),
        name=name,
        eligible_positions=positions,
        selected_position=slot,
        projected_points=proj,
        **kw,
    )


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def standard_league() -> dict[str, int]:
    """A conventional redraft layout."""
    return {
        "QB": 1,
        "WR": 2,
        "RB": 2,
        "TE": 1,
        "W/R/T": 1,
        "K": 1,
        "DEF": 1,
        "BN": 6,
        "IR": 1,
    }
