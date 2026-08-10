"""Runtime configuration, loaded from config.yml with safe defaults.

Everything the agent uses to make a judgment call lives here rather than in
code, so tuning its behaviour mid-season never means editing logic.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .models import BENCH, IR_SLOTS


class ConfigError(ValueError):
    """config.yml (or league.yml) exists but could not be understood."""


def _construct(dataclass_cls, block: str, kwargs: dict[str, Any]):
    """`dataclass_cls(**kwargs)`, turning an unknown-keyword `TypeError` into
    a `ConfigError` that names the valid keys.

    The dataclass constructor's own `TypeError` only ever says "unexpected
    keyword argument 'x'" — it never says what else was available, which is
    the one piece of information actually useful when you mistype a key in
    config.yml.
    """
    try:
        return dataclass_cls(**kwargs)
    except TypeError as exc:
        valid = ", ".join(f.name for f in fields(dataclass_cls))
        raise ConfigError(f"{block}: {exc}. Valid keys: {valid}") from exc


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge `overlay` onto `base`, recursing into nested dicts so a partial
    override (e.g. just `draft: {num_teams: 10}`) doesn't blank out
    everything else under that block. Non-dict values (including lists) are
    replaced wholesale, matching ordinary YAML-merge expectations."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce_block(raw: dict[str, Any], block: str, renames: dict[str, str]) -> dict[str, Any]:
    """Apply `{old_key: new_key}` renames to one config block's raw dict.

    Every config block goes through this, so a future rename is one table
    entry rather than a bespoke migration. Rules, in order:

    - old key present, new absent: use the old value under the new name and
      warn, naming both the new key and why it changed.
    - both present: `ConfigError` — never silently pick one over the other.
    - a key that is neither a current field name nor a known old name is left
      alone; the dataclass constructor's `TypeError` on an unrecognized
      keyword is the final backstop, this function only exists to make the
      *known* renames painless.
    """
    raw = dict(raw)
    for old, new in renames.items():
        if old in raw and new in raw:
            raise ConfigError(
                f"{block}: both '{old}' and '{new}' are set — "
                f"'{old}' was renamed to '{new}'; remove one"
            )
        if old in raw:
            warnings.warn(
                f"{block}: '{old}' has been renamed '{new}' — "
                f"update config.yml (old key still honored for now)",
                DeprecationWarning,
                stacklevel=3,
            )
            raw[new] = raw.pop(old)
    return raw


@dataclass
class ProjectionConfig:
    """How a player's expected output for the week is estimated.

    Not the league's scoring rules — those live in `LeagueScoring` /
    `league.yml`. This block only covers how a *missing* projection gets
    estimated and how injury status discounts one that exists.
    """

    # Yahoo exposes weekly projections inconsistently. When absent we fall
    # back to a blend of season average and recent form; this is the weight
    # on recent form. Dead on every production path today (`recent_points` /
    # `season_avg_points` are never populated outside tests — see
    # `lineup._fallback_points`), kept for the day a live route genuinely
    # produces no projection.
    recency_weight: float = 0.6

    # How many recent weeks count as "recent form".
    recency_window: int = 3

    # Questionable players start, but at a discount, so a healthy player of
    # near-equal projection is preferred.
    questionable_multiplier: float = 0.85

    # Treat Doubtful as Out. Turning this off makes D a discount rather than a
    # hard bench.
    doubtful_is_out: bool = True


# Old name kept as an alias so `from .config import ScoringConfig` still
# works during the migration window; new code should use ProjectionConfig.
ScoringConfig = ProjectionConfig


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

    # "snake" (default, alternating direction each round) or "linear" (same
    # slot order every round — auction-style draft rooms sometimes run this
    # for a straight redraft). Validated in `Config.from_dict`.
    order: str = "snake"

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

    # Weight on the gap between league-scored points and consensus PPR points
    # (`BoardPlayer.points - points_fp`, computed once the board is scored
    # under `league.yml`'s rules — see `ffbot/scoring.py`). ADP is set by the
    # consensus PPR market, so this is the one edge signal that market
    # disagreement can never correct: the market isn't playing your league.
    # 0.0 (the default) is an exact no-op, same as every other edge weight.
    scoring_arbitrage_weight: float = 0.0

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

    def __post_init__(self) -> None:
        if self.order not in ("snake", "linear"):
            raise ConfigError(
                f"config.yml [draft]: order must be 'snake' or 'linear', got {self.order!r}"
            )


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

    # Discount for a game flagged `international: true` in `weekly/week-NN.yml`
    # (see `GameInfo.international`) — unfamiliar time zone, travel, a smaller
    # crowd, whatever "not a typical NFL setting" turns out to mean for a given
    # game. Applies to BOTH teams' offensive skill positions (K/QB/WR/TE/RB;
    # DEF exempt, same reasoning as `weather_multiplier`), as a fraction of
    # that week's decision scale. Deliberately NOT part of `SPICE_PRESETS` —
    # unlike weather/Vegas, there is no real evidence base for how much an
    # international game actually moves output, so raising the spice level
    # alone must never turn this on; it is a hand-set, evidence-weak knob.
    # 0.0 (the default) is an exact no-op.
    venue_disruption_weight: float = 0.0

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

    # How many of the board's best-available free agents to scan per waiver
    # run. 150 comfortably covers every plausible claim; raise it only for an
    # unusually deep league.
    waiver_pool_size: int = 150

    # Floor on how many bench spots must stay classified STREAM regardless of
    # what `hold_margin` says — a taste for roster optionality, not a slot
    # count. 0 (the default) is a no-op; core/stream is otherwise entirely
    # derived, never hardcoded — see `week.classify_roster`.
    min_stream_spots: int = 0

    # Flat season-point bonus added to `hold_margin` for any roster entry
    # flagged `blocking: true` in roster.yml — an explicit, honest admission
    # that this hold is about denying a rival, not about your own lineup.
    # 0.0 (the default) is a no-op; `hold_margin` cannot see denial value on
    # its own, since it is a function of your roster alone.
    blocking_hold_bonus: float = 0.0

    # Weight on denying a contested player to a rival, as a fraction of
    # decision scale — see `week.denial_value`. 0.0 (the default) is an exact
    # no-op; this needs `league_rosters.yml` (imported rival rosters) to be
    # anything but zero regardless of this weight.
    denial_weight: float = 0.0

    # Flat boost added to a rival's `threat` (see `denial.threat`) when that
    # rival is `league.yml`'s `my_opponent` this week — a head-to-head
    # opponent's need is worth more than "some rival's" bubble-distance
    # alone captures, since denying them has a direct effect on your own
    # matchup. 0.0 (the default) is an exact no-op; also needs `my_opponent`
    # set to be anything but zero.
    denial_opponent_boost: float = 0.0

    # How many playoff seeds away from your own a rival can be and still get
    # a proximity boost toward maximum `threat`, once the season has reached
    # its playoff push (the final few weeks of `regular_season_weeks`) — a
    # team fighting you directly for a seed is dangerous regardless of its
    # distance from the bubble itself. 0 (the default) disables this; also
    # needs your own seed known (a `teams:` entry matching `my_team`) to be
    # anything but zero.
    denial_seed_window: int = 0

    # Under a rolling-priority waiver league (`league.yml`'s `waiver_type:
    # rolling`, not FAAB), how expensive spending your current priority is,
    # as a fraction of decision scale at priority 1 (most expensive) fading
    # to ~0 at the bottom of the list. See `week.claim_cost`.
    priority_value: float = 0.3

    # Refuse a pure-denial claim (rostering someone you'd never start,
    # solely to keep a rival from getting him — see `denial.py`) when your
    # own rolling priority is this good or better. A denial hold has no
    # lineup value of its own to justify spending a genuinely valuable
    # priority position the way a real add does. 0 (the default) = no
    # restriction; 3 refuses spending your top-3 priority on denial alone.
    denial_priority_floor: int = 0

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


# --- League scoring (league.yml) --------------------------------------------
#
# What the league actually pays for a stat, as distinct from everything
# above — none of which is about scoring rules at all. Lives in its own file
# (see `LeagueScoring.load`) rather than a config.yml block: it's a fact
# about the league set once, not something to "tune freely mid-season," and
# it wants to be machine-regenerated wholesale from Yahoo's settings once the
# API lands, which isn't safe to do to a block inside a hand-narrated file.
#
# A missing/unset league (`Config.league is None`) is an exact no-op — the
# board keeps FantasyPros' own consensus points untouched. See
# `ffbot/scoring.py` for the math and `ffbot/board.py`'s `apply_league_scoring`
# for where it hooks in.


@dataclass(frozen=True)
class Tier:
    """One step of a step-function scoring ladder (e.g. points allowed):
    `points` applies to any value <= `max`."""

    max: float
    points: float


@dataclass(frozen=True)
class DistanceBand:
    """One band of a distance-scored ladder (e.g. field goals)."""

    min: float
    max: float
    points: float


@dataclass
class PassingScoring:
    yards_per_point: float = 25.0
    td: float = 4.0
    int: float = -2.0
    two_pt: float = 2.0


@dataclass
class RushingScoring:
    yards_per_point: float = 10.0
    td: float = 6.0
    two_pt: float = 2.0


@dataclass
class ReceivingScoring:
    yards_per_point: float = 10.0
    td: float = 6.0
    reception: float = 1.0  # PPR value; 0.5 = half-PPR, 0.0 = standard
    two_pt: float = 2.0
    # Position-specific reception value (e.g. {"TE": 1.5} for TE-premium)
    # overrides `reception` for that position only.
    reception_by_position: dict[str, float] = field(default_factory=dict)


@dataclass
class MiscScoring:
    fumble_lost: float = -2.0
    off_fumble_return_td: float = 0.0


@dataclass
class BonusScoring:
    """Big-play bonuses no FantasyPros export column can express — declared
    so `unmodeled_rules` can warn about them, not because this module can
    apply them. See `ffbot/scoring.py`."""

    pass_completion_40plus: float = 0.0
    rush_td_40plus: float = 0.0
    rec_td_40plus: float = 0.0


@dataclass
class KickingScoring:
    pat_made: float = 1.0
    pat_missed: float = 0.0
    fg_made: float = 3.0  # flat fallback when fg_by_distance is empty
    fg_missed: float = 0.0

    # Distance-tiered FG scoring. Empty = flat `fg_made` for every make.
    fg_by_distance: list[DistanceBand] = field(default_factory=list)

    # League-wide historical share of attempts per band, keyed "min-max"
    # (e.g. "40-49") matching `fg_by_distance`'s own bands. Used to estimate
    # a per-FG expected value when the export carries no distance split —
    # see `scoring._fg_value_per_kick`.
    fg_distance_mix: dict[str, float] = field(default_factory=dict)

    # Distance-tiered miss penalty. No export column carries a distance
    # split on misses at all, so this is declared for `unmodeled_rules` only.
    fg_missed_by_distance: list[DistanceBand] = field(default_factory=list)


@dataclass
class DefenseScoring:
    sack: float = 1.0
    interception: float = 2.0
    fumble_recovery: float = 2.0
    forced_fumble: float = 0.0
    touchdown: float = 6.0
    safety: float = 2.0
    block_kick: float = 0.0
    extra_point_returned: float = 0.0
    three_and_outs: float = 0.0

    # Step-function points-allowed ladder. Empty = not scored at all, which
    # is what every FantasyPros DEF projection assumes by default.
    points_allowed: list[Tier] = field(default_factory=list)

    # Per-game standard deviation of points allowed, for integrating
    # `points_allowed` over a distribution rather than a flat per-game
    # average — see `scoring._points_allowed_per_game`. 0.0 collapses to the
    # simpler point-estimate version.
    points_allowed_stdev: float = 0.0

    yards_allowed: list[Tier] = field(default_factory=list)  # not scored unless set


@dataclass(frozen=True)
class TeamStanding:
    """One rival's curated standings row — small and hand-edited, unlike
    `league_rosters.yml`'s generated roster import. Update it weekly (or
    whenever it drifts); `denial.py` degrades gracefully when a team
    referenced in `league_rosters.yml` has no matching entry here.
    """

    name: str  # must match a team key in league_rosters.yml's teams:
    record: str = ""
    seed: int | None = None
    waiver_priority: int | None = None  # rolling leagues; lower = earlier in line
    eliminated: bool = False  # locked out of waivers in leagues with lock_eliminated_teams


@dataclass
class LeagueScoring:
    """The league's real scoring rules and a few structural facts about it
    that valuation code needs (waiver type, playoff shape, standings).

    Every numeric default here matches a conventional Yahoo redraft league,
    so a `league.yml` only needs to state where it *differs*. Compare
    `LeagueScoring.fantasypros_default()`, which is not this — it is the
    fixed, reverse-engineered set of rules FantasyPros' own exports are
    built under, used only to compute the coverage residual self-check in
    `board.py`.
    """

    name: str = ""
    source: str = ""  # provenance note, e.g. "Yahoo settings page, 2026-08-09"
    games_per_season: int = 17
    regular_season_weeks: int = 14
    playoff_teams: int = 0
    waiver_type: str = "faab"  # "faab" | "rolling"
    lock_eliminated_teams: bool = False

    # Standings — curated, not generated (compare league_rosters.yml, which
    # is). Empty by default; `denial.py`'s functions are all exact no-ops
    # with no teams here, same as every other optional research input.
    week: int | None = None
    my_team: str = ""

    # Your head-to-head opponent THIS week — curated weekly, like `week:`
    # itself. Feeds `season.denial_opponent_boost`: a rival named here is a
    # known, specific threat to you this week, not just "some rival" at
    # whatever bubble-distance their seed implies. "" (the default) is a
    # no-op — `denial.threat` never fires the opponent boost with nothing to
    # match against.
    my_opponent: str = ""

    teams: list[TeamStanding] = field(default_factory=list)

    passing: PassingScoring = field(default_factory=PassingScoring)
    rushing: RushingScoring = field(default_factory=RushingScoring)
    receiving: ReceivingScoring = field(default_factory=ReceivingScoring)
    misc: MiscScoring = field(default_factory=MiscScoring)
    bonuses: BonusScoring = field(default_factory=BonusScoring)
    kicking: KickingScoring = field(default_factory=KickingScoring)
    defense: DefenseScoring = field(default_factory=DefenseScoring)

    @classmethod
    def load(cls, path: str | Path) -> "LeagueScoring | None":
        """Missing file -> None, same contract as `intel.yml`: a league that
        hasn't been transcribed yet leaves the board bit-identical rather
        than failing."""
        p = Path(path)
        if not p.exists():
            return None
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{p}: expected a mapping at the top level")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LeagueScoring":
        kicking_raw = raw.get("kicking") or {}
        defense_raw = raw.get("defense") or {}
        return cls(
            name=str(raw.get("name", "")),
            source=str(raw.get("source", "")),
            games_per_season=int(raw.get("games_per_season", 17)),
            regular_season_weeks=int(raw.get("regular_season_weeks", 14)),
            playoff_teams=int(raw.get("playoff_teams", 0)),
            waiver_type=str(raw.get("waiver_type", "faab")),
            lock_eliminated_teams=bool(raw.get("lock_eliminated_teams", False)),
            week=raw.get("week"),
            my_opponent=str(raw.get("my_opponent", "")),
            my_team=str(raw.get("my_team", "")),
            teams=[
                _construct(TeamStanding, "league.yml [teams]", t)
                for t in raw.get("teams") or []
            ],
            passing=_construct(PassingScoring, "league.yml [passing]", raw.get("passing") or {}),
            rushing=_construct(RushingScoring, "league.yml [rushing]", raw.get("rushing") or {}),
            receiving=_construct(ReceivingScoring, "league.yml [receiving]", raw.get("receiving") or {}),
            misc=_construct(MiscScoring, "league.yml [misc]", raw.get("misc") or {}),
            bonuses=_construct(BonusScoring, "league.yml [bonuses]", raw.get("bonuses") or {}),
            kicking=_construct(
                KickingScoring,
                "league.yml [kicking]",
                {
                    **kicking_raw,
                    "fg_by_distance": [
                        _construct(DistanceBand, "league.yml [kicking.fg_by_distance]", b)
                        for b in kicking_raw.get("fg_by_distance") or []
                    ],
                    "fg_missed_by_distance": [
                        _construct(DistanceBand, "league.yml [kicking.fg_missed_by_distance]", b)
                        for b in kicking_raw.get("fg_missed_by_distance") or []
                    ],
                },
            ),
            defense=_construct(
                DefenseScoring,
                "league.yml [defense]",
                {
                    **defense_raw,
                    "points_allowed": [
                        _construct(Tier, "league.yml [defense.points_allowed]", t)
                        for t in defense_raw.get("points_allowed") or []
                    ],
                    "yards_allowed": [
                        _construct(Tier, "league.yml [defense.yards_allowed]", t)
                        for t in defense_raw.get("yards_allowed") or []
                    ],
                },
            ),
        )

    @classmethod
    def fantasypros_default(cls) -> "LeagueScoring":
        """The scoring FantasyPros' own projection exports are built under,
        reverse-engineered from their published point totals: full PPR,
        4-point passing TDs, -1 INT, flat 3-point FGs, and DEF scored on
        sack/INT/FR/TD/safety only — points allowed contributes nothing.

        Used only as the baseline for the coverage-residual self-check
        (`points_fp - score_statline(stats, fantasypros_default())`, which
        should be ~0 for a correctly-parsed row) — never as a fallback for
        an unset `league.yml`, which stays `None` and leaves the board
        untouched instead.
        """
        return cls(
            passing=PassingScoring(yards_per_point=25, td=4, int=-1, two_pt=0),
            rushing=RushingScoring(yards_per_point=10, td=6, two_pt=0),
            receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0, two_pt=0),
            misc=MiscScoring(fumble_lost=-2, off_fumble_return_td=0),
            bonuses=BonusScoring(),
            kicking=KickingScoring(pat_made=1.0, pat_missed=0.0, fg_made=3.0, fg_missed=0.0),
            defense=DefenseScoring(
                sack=1.0, interception=2.0, fumble_recovery=2.0, forced_fumble=0.0,
                touchdown=6.0, safety=2.0, block_kick=0.0, extra_point_returned=0.0,
                three_and_outs=0.0, points_allowed=[], points_allowed_stdev=0.0, yards_allowed=[],
            ),
        )


def _warn_if_capacity_mismatch(roster_positions: dict[str, int], draft_raw: dict[str, Any]) -> None:
    """Warn (never raise) when `draft.rounds` doesn't match the roster's own
    starter+bench capacity (IR excluded — see `models.roster_capacity`).
    Keeper leagues and deliberate IR-stash strategies legitimately break this
    identity, so it is a nudge, never a validation gate.
    """
    rounds = draft_raw.get("rounds")
    if rounds is None:
        return
    starters = sum(c for slot, c in roster_positions.items() if slot != BENCH and slot not in IR_SLOTS)
    bench = roster_positions.get(BENCH, 0)
    capacity = starters + bench
    if int(rounds) != capacity:
        warnings.warn(
            f"config.yml: draft.rounds ({rounds}) does not match roster_positions' "
            f"starters+bench ({capacity} = {starters} starters + {bench} bench, IR excluded) — "
            "fine for a keeper league or a deliberate IR-stash strategy, otherwise probably a typo",
            stacklevel=3,
        )


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

    # How a missing weekly projection gets estimated — see `ProjectionConfig`.
    # Field renamed from `scoring` (see `ScoringConfig` alias above): this
    # block was never the league's scoring rules, which now live in
    # `LeagueScoring` / `league.yml`. The YAML key `scoring:` still loads
    # here via `_coerce_block`, with a deprecation warning.
    projection: ProjectionConfig = field(default_factory=ProjectionConfig)

    drops: DropPolicyConfig = field(default_factory=DropPolicyConfig)
    faab: FaabConfig = field(default_factory=FaabConfig)
    draft: DraftConfig = field(default_factory=DraftConfig)
    season: SeasonConfig = field(default_factory=SeasonConfig)

    # Where the league's real scoring rules live — see the "League scoring"
    # section above. Empty path or missing file = `league` stays None = every
    # board recomputation in `ffbot/board.py` is an exact no-op.
    league_file: str = ""
    league: "LeagueScoring | None" = None

    @classmethod
    def load(cls, path: str | Path = "config.yml") -> "Config":
        """Load `path`, then merge an optional sibling `config.local.yml` on
        top (deep-merged, not replaced wholesale — a local override of just
        `draft: {num_teams: 10}` must not blank out the rest of `draft:`).

        `config.local.yml` is the GUI's write target (see `ffbot/webapi.py`):
        `config.yml` itself is hand-narrated with comments that a YAML
        round-trip would strip, so the GUI never writes there. Gitignored;
        missing entirely is the common case and changes nothing.
        """
        p = Path(path)
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}
        if p.name != "config.local.yml":
            local_p = p.with_name("config.local.yml")
            if local_p.exists():
                local_raw = yaml.safe_load(local_p.read_text()) or {}
                raw = _deep_merge(raw, local_raw)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        raw = _coerce_block(raw, "config.yml", {"scoring": "projection"})

        roster_positions = dict(raw.get("roster_positions") or cls().roster_positions)
        _warn_if_capacity_mismatch(roster_positions, raw.get("draft") or {})

        league_file = str(raw.get("league_file", "") or "")
        league = LeagueScoring.load(league_file) if league_file else None

        return cls(
            league_id=str(raw.get("league_id", "")),
            team_key=str(raw.get("team_key", "")),
            dry_run=bool(raw.get("dry_run", True)),
            lock_window_minutes=int(raw.get("lock_window_minutes", 45)),
            roster_positions=roster_positions,
            projection=_construct(ProjectionConfig, "config.yml [projection]", raw.get("projection") or {}),
            drops=_construct(DropPolicyConfig, "config.yml [drops]", raw.get("drops") or {}),
            faab=_construct(FaabConfig, "config.yml [faab]", raw.get("faab") or {}),
            draft=_construct(DraftConfig, "config.yml [draft]", raw.get("draft") or {}),
            season=_season_from_dict(raw.get("season") or {}),
            league_file=league_file,
            league=league,
        )
