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

    # How fast bench depth loses value once a position is already covered.
    # Each backup beyond your starters is worth this fraction of the previous
    # one, so 0.5 means the second backup TE is worth half the first and the
    # fifth is worth almost nothing.
    #
    # 1.0 (the default) disables the decay and is the original behaviour:
    # every backup priced the same as the first. That is what makes the
    # optimizer draft seven tight ends, because once your starters are full
    # `need` is zero for everyone and the ranking collapses onto raw VOR —
    # which favours whichever position the market happens to undervalue.
    depth_decay: float = 1.0

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

    # Hard ceiling on how many of a position to roster. Once you are at the
    # cap, that position stops being recommended entirely.
    #
    # This encodes a judgement the value model cannot reach on its own: `need`
    # correctly falls to zero once your starters are covered, but so does
    # everyone else's, and the ranking then falls back to raw VOR — which piles
    # into whichever position the market undervalues. A seventh tight end can
    # never be started under any circumstance, and no amount of value-over-
    # replacement makes him useful.
    #
    # Empty (the default) means no caps.
    position_caps: dict[str, int] = field(default_factory=dict)

    # Desired roster shape, softer than a cap: how many of each position the
    # final roster should ideally hold. Two effects. (1) The bench-depth term
    # decays past the target rather than past the starter count — which is
    # what lets a flex league prefer exactly 1 TE even though the flex slot
    # technically makes a 2nd TE "startable". (2) Positions still short of
    # target get a growing boost as remaining picks run out (`balance_weight`).
    # Empty (the default) disables both.
    position_targets: dict[str, int] = field(default_factory=dict)

    # Weight on the deficit-urgency boost, as a fraction of the pick's
    # decision scale (like every edge weight). 0.0 disables.
    balance_weight: float = 0.0

    # --- Edge / contrarian terms (see ffbot/edge.py) ------------------------
    #
    # All default to 0.0, which makes the edge layer an exact no-op and keeps
    # the optimizer's behaviour identical to a pure value-over-replacement
    # board. The aggressive values live in config.yml, so "how contrarian is
    # it" is one number to turn mid-draft rather than a code change.
    #
    # These are fractions of `edge.decision_scale` — the value at stake in the
    # current pick — not absolute points. A fixed point bonus cannot work for
    # both ends of a draft: round-1 gaps between the best options are over a
    # hundred points and round-12 gaps are two or three, so any constant is
    # either invisible early or the whole ranking late.

    # Weight on researched breakout potential from draft/intel.yml.
    upside_weight: float = 0.0

    # Weight on researched *availability* risk (suspension, PUP, holdout,
    # no-timetable injury) — the one intel signal that subtracts. Unramped:
    # a suspension is as real in round 1 as round 10.
    risk_weight: float = 0.0

    # How strongly the static pre-draft export (board.txt etc.) bakes in
    # upside and risk, in season points at a full 100 score. The live layer
    # scales per pick; a static list can't, so this is a fixed, modest unit.
    # 0.0 = the export stays pure VOR.
    export_intel_scale: float = 0.0

    # Weight on cross-site ADP disagreement as a boom/bust proxy.
    volatility_weight: float = 0.0

    # Bonus for completing a QB / pass-catcher stack.
    stack_bonus: float = 0.0

    # Weight on ADP surplus (how far past our rank the market lets them fall).
    arbitrage_weight: float = 0.0

    # Variance tolerance ramps from 0 at `risk_ramp_start` to 1 at
    # `risk_ramp_full`, and scales the upside and volatility terms. Floor
    # early, ceiling late — early picks are most of your roster's points,
    # while a late-round replacement body is worth roughly nothing, so a
    # small chance at a league-winner is the better bet there.
    risk_ramp_start: int = 2
    risk_ramp_full: int = 5

    # Where researched player intel lives. Missing file = no intel, and every
    # value stays bit-identical to a run without it.
    intel_file: str = "draft/intel.yml"

    # UI / sync.
    recommend_count: int = 12
    sync_poll_seconds: int = 5


@dataclass
class SeasonConfig:
    """The in-season weekly manager — start/sit, waivers, streaming.

    Mirrors `DraftConfig`'s shape: every weight is a fraction of the decision
    at hand (the projection gap between the real options that week) rather
    than an absolute point total — the same lesson `edge.py` learned the hard
    way, that a fixed bonus is invisible in a blowout-margin week and
    dominant in a coin-flip week unless it scales with the decision itself.

    Unlike the draft, the primary dial is `spice_level` (1-5), not five raw
    weights the user has to hand-tune. The reason is explicit: weather and
    Vegas signals alone mostly agree with consensus on a calm week with no
    bad forecast and no lopsided game — which would make the system read as
    "just follow Yahoo" more often than not. `volatility_weight` and
    `upside_lean_weight` exist specifically so a genuinely close start/sit
    call can still go to the higher-ceiling player on a placid week, which is
    what keeps the system from collapsing to the safe, boring answer.
    """

    # Where this week's researched intel lives. A missing file degrades to
    # "no status overrides, no weather/vegas adjustment, no notes" — the
    # optimizer still runs on projections alone.
    weekly_intel_file: str = ""  # e.g. "weekly/week-03.yml"; set per run

    # The one dial: 1 (Chalk — nearly pure consensus/projection, deviates
    # only when something is drastic) through 5 (Chaos — actively hunts
    # boom/bust plays and will bench a name-brand floor player for real
    # upside even outside a coin-flip). 3 (Balanced) is the default. Setting
    # this is enough on its own — see `SeasonConfig.from_spice_level`.
    spice_level: int = 3

    # --- Derived weights (set by spice_level; hand-edit only to override a
    # single signal without touching the rest — see `from_spice_level`) ----

    # Weather: how much a bad-weather multiplier can discount a player's
    # weekly score, as a fraction of that week's decision gap.
    # `wind_threshold_mph` / `precip_threshold_pct` are where the discount
    # starts applying at all — below threshold is a genuine no-op, not a
    # small effect, because a 6mph breeze is not weather. Kickers and
    # pass-heavy positions (QB/WR/TE) take the full discount; RBs take
    # `rb_weather_relief` of it, since bad weather typically shifts game
    # plans toward the run rather than killing offense outright.
    weather_weight: float = 0.0
    wind_threshold_mph: float = 15.0
    precip_threshold_pct: float = 50.0
    rb_weather_relief: float = 0.5

    # Vegas implied-total tilt: teams projected to score heavily get a
    # ceiling lift for their offensive skill positions; teams projected to
    # allow few points get the same lift for DEF. Note the inversion — a
    # defense's own team's implied total is irrelevant to it; the
    # OPPONENT's is what matters.
    vegas_weight: float = 0.0

    # Boom/bust lean: on a close start/sit call, prefer the researched
    # higher-variance player. Drawn from the same `adp_spread`-style
    # cross-source disagreement idea as the draft's volatility term, applied
    # weekly (projection disagreement across sources, when available, else
    # researched boom/bust notes).
    volatility_weight: float = 0.0

    # How much researched `upside` (breakout/spike-week potential, distinct
    # from availability risk) can tip a close call toward the flagged player.
    upside_lean_weight: float = 0.0

    # Streaming K/DEF: blend fraction between season-long floor value (0.0)
    # and this week's pure matchup value (1.0) when ranking streaming
    # candidates — a streamer's whole point is that this week's matchup
    # dominates their season-long track record.
    streaming_weight: float = 0.5

    # Rest-of-season value blended into this week's number for waiver
    # ranking, so a one-week hot streak doesn't outrank a real weekly starter.
    # 0.0 = pure this-week value; 1.0 = pure ROS value.
    ros_blend: float = 0.5

    # How many top waiver/streaming candidates to show per position.
    recommend_count: int = 5

    @classmethod
    def from_spice_level(cls, level: int, **overrides) -> "SeasonConfig":
        """Build a SeasonConfig from the 1-5 dial, with any explicit field
        in `overrides` winning over the preset — the power-user escape hatch
        for tuning one signal without losing the rest of the level's shape.
        """
        if level not in SPICE_PRESETS:
            raise ValueError(f"spice_level must be 1-5, got {level}")
        fields = dict(SPICE_PRESETS[level])
        fields["spice_level"] = level
        fields.update(overrides)
        return cls(**fields)


# Explicit ladder rather than a single scaling formula: the signals don't all
# want to move at the same rate. Streaming is bounded [0, 1] (it's a blend,
# not an open weight) and moves gently; volatility/upside_lean are what
# actually keep the system from reading as "just Yahoo's rankings" on a calm
# week, so they climb the fastest.
SPICE_PRESETS: dict[int, dict[str, float]] = {
    1: dict(weather_weight=0.08, vegas_weight=0.06, volatility_weight=0.05,
            upside_lean_weight=0.05, streaming_weight=0.50),
    2: dict(weather_weight=0.15, vegas_weight=0.12, volatility_weight=0.12,
            upside_lean_weight=0.12, streaming_weight=0.65),
    3: dict(weather_weight=0.25, vegas_weight=0.20, volatility_weight=0.22,
            upside_lean_weight=0.22, streaming_weight=0.80),
    4: dict(weather_weight=0.38, vegas_weight=0.32, volatility_weight=0.38,
            upside_lean_weight=0.38, streaming_weight=0.90),
    5: dict(weather_weight=0.55, vegas_weight=0.48, volatility_weight=0.60,
            upside_lean_weight=0.60, streaming_weight=0.95),
}


def _season_from_dict(raw: dict[str, Any]) -> SeasonConfig:
    """Build the season block: spice_level sets the preset, any other key
    present in the raw yaml overrides that one signal on top of it. This is
    what lets `spice_level: 4` alone be a complete, sensible config, while
    still allowing `weather_weight: 0.0` next to it to mean "level 4, but
    don't touch me about the weather" without hand-copying the other four
    numbers.
    """
    level = int(raw.get("spice_level", 3))
    overrides = {k: v for k, v in raw.items() if k != "spice_level"}
    return SeasonConfig.from_spice_level(level, **overrides)


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
    season: SeasonConfig = field(default_factory=SeasonConfig)

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
            season=_season_from_dict(raw.get("season") or {}),
        )
