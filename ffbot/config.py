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
class DraftConfig:
    """Everything the draft assistant needs to run with no network.

    Yahoo randomizes draft position, so `my_slot` is usually unknown until
    draft day — override it at runtime (`scripts/draft.py --slot N`) rather
    than editing this file mid-draft.
    """

    # League shape.
    num_teams: int = 12
    my_slot: int = 1
    rounds: int = 15

    # Keeper leagues or traded picks break the arithmetic snake progression.
    # When non-empty this list of pick numbers wins over `my_slot`/`rounds`.
    my_picks: list[int] = field(default_factory=list)

    # Pre-draft CSV exports (FantasyPros rankings/ADP/projections). Multiple
    # files are merged by normalized player name.
    board_csv: list[str] = field(default_factory=list)

    # Valuation. `replacement_depth` is a multiplier on the optimizer-derived
    # starter counts (1.0 = pure derivation, no manual tuning). `depth_weight`
    # is what a player who won't crack your starters is still worth — bench
    # depth, bye cover, injury insurance.
    replacement_depth: float = 1.0
    depth_weight: float = 0.35

    # Tiering. A new tier starts when the point gap to the next player
    # exceeds max(tier_min_gap, tier_gap_multiplier * that position's median
    # gap). Frozen at board load — never recomputed mid-draft.
    tier_gap_multiplier: float = 2.0
    tier_min_gap: float = 5.0

    # ADP survival. sigma = stdev from the CSV if present, else
    # max(adp_sigma_floor, adp_sigma_scale * adp) — uncertainty grows with
    # how late a player typically goes.
    adp_sigma_floor: float = 6.0
    adp_sigma_scale: float = 0.22

    # Positional-run alert: flag when at least `run_threshold` of the last
    # `run_window` picks share a position.
    run_window: int = 10
    run_threshold: int = 5

    # Name matching. Fuzzy matches below `fuzzy_threshold`, or that don't
    # beat the runner-up by `fuzzy_margin`, are left unmatched rather than
    # silently guessed. `aliases` maps a board display name straight to a
    # Yahoo display name for cases the cascade can't resolve (e.g.
    # nicknames like "Hollywood Brown" -> "Marquise Brown").
    fuzzy_threshold: float = 0.88
    fuzzy_margin: float = 0.04
    aliases: dict[str, str] = field(default_factory=dict)

    # Export. These positions are pushed to the end of the exported board so
    # Yahoo's autopick can't burn a mid-round pick on a kicker.
    export_defer_positions: list[str] = field(default_factory=lambda: ["K", "DEF"])

    # UI / sync.
    recommend_count: int = 12
    sync_poll_seconds: int = 5


@dataclass
class Config:
    league_id: str = ""
    team_key: str = ""

    # When true, every action is computed and logged but never sent to Yahoo.
    dry_run: bool = True

    # Act on a player only once their game is within this many minutes. Keeps
    # the frequent Actions ticks idempotent and cheap.
    lock_window_minutes: int = 45

    # League's starting-slot layout, e.g. {"QB": 1, "WR": 2, ..., "BN": 6}.
    # scripts/whoami.py prints this shape directly from Yahoo; the draft
    # assistant needs it before that call is available, so it also lives
    # here as a config default.
    roster_positions: dict[str, int] = field(
        default_factory=lambda: {
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
    )

    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    drops: DropPolicyConfig = field(default_factory=DropPolicyConfig)
    faab: FaabConfig = field(default_factory=FaabConfig)
    draft: DraftConfig = field(default_factory=DraftConfig)

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
            roster_positions=dict(raw.get("roster_positions") or cls().roster_positions),
            scoring=ScoringConfig(**(raw.get("scoring") or {})),
            drops=DropPolicyConfig(**(raw.get("drops") or {})),
            faab=FaabConfig(**(raw.get("faab") or {})),
            draft=DraftConfig(**(raw.get("draft") or {})),
        )
