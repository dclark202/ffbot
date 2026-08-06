"""Runtime configuration, loaded from config.yml with safe defaults.

Everything the agent uses to make a judgment call lives here rather than in
code, so tuning its behaviour mid-season never means editing logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ScoringConfig:
    """How a player's expected output for the week is estimated."""

    # Yahoo exposes weekly projections inconsistently. When absent we fall back
    # to a blend of season average and recent form; this is the weight on
    # recent form.
    recency_weight: float = 0.6

    # How many recent weeks count as "recent form".
    recency_window: int = 3

    # Questionable players start, but at a discount, so a healthy player of
    # near-equal projection is preferred.
    questionable_multiplier: float = 0.85

    # Treat Doubtful as Out. Turning this off makes D a discount rather than a
    # hard bench.
    doubtful_is_out: bool = True


@dataclass
class DropPolicyConfig:
    """Guardrails on the one irreversible action the agent can take."""

    # Players that may never be dropped, by name (case-insensitive).
    never_drop: list[str] = field(default_factory=list)

    # Anyone rostered in at least this share of Yahoo leagues is protected —
    # high ownership is the best available proxy for "this is a real asset".
    protect_pct_owned: float = 60.0

    # Players taken this early in the draft are protected.
    protect_draft_rounds: int = 4


@dataclass
class FaabConfig:
    """Bidding limits for waiver claims."""

    # Never spend more than this share of the remaining budget on one claim.
    max_bid_pct: float = 0.35

    # Always keep this much budget back for the playoff run.
    min_reserve: int = 5

    # Do not bid at all on a player owned in fewer leagues than this — avoids
    # burning budget on noise.
    min_pct_owned_to_bid: float = 2.0


@dataclass
class Config:
    league_id: str = ""
    team_key: str = ""

    # When true, every action is computed and logged but never sent to Yahoo.
    dry_run: bool = True

    # Act on a player only once their game is within this many minutes. Keeps
    # the frequent Actions ticks idempotent and cheap.
    lock_window_minutes: int = 45

    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    drops: DropPolicyConfig = field(default_factory=DropPolicyConfig)
    faab: FaabConfig = field(default_factory=FaabConfig)

    @classmethod
    def load(cls, path: str | Path = "config.yml") -> "Config":
        p = Path(path)
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        return cls(
            league_id=str(raw.get("league_id", "")),
            team_key=str(raw.get("team_key", "")),
            dry_run=bool(raw.get("dry_run", True)),
            lock_window_minutes=int(raw.get("lock_window_minutes", 45)),
            scoring=ScoringConfig(**(raw.get("scoring") or {})),
            drops=DropPolicyConfig(**(raw.get("drops") or {})),
            faab=FaabConfig(**(raw.get("faab") or {})),
        )
