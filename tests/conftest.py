from __future__ import annotations

import itertools

import pytest

from ffbot.config import Config
from ffbot.models import Player

_ids = itertools.count(1)


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
    """A conventional Yahoo redraft layout."""
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
