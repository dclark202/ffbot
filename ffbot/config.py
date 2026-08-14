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
class ProjectionSourceConfig:
    """WHERE this week's raw projection numbers come from — a different
    question from `ProjectionConfig` above, which is about estimating a
    projection that's already missing. This block picks the source in the
    first place.

    `"board"` (the default) is today's exact behavior, unchanged: the frozen
    preseason board divided down to a per-week rate
    (`roster_source.season_board_rows`) — an exact no-op until this is
    switched on, same convention as every other optional input in this
    codebase. `"sleeper"` fetches real, current weekly projections (and a
    genuine rest-of-season total — see `ffbot/projections/__init__.py`) from
    api.sleeper.app, free and unauthenticated. `"csv"` is the pre-existing
    `--proj`/hand-fed-FantasyPros-export route (`week_report.py`'s
    `--proj` flag), left as-is.
    """

    source: str = "board"  # "board" | "sleeper" | "csv"

    # How long a fetched week's cache file is trusted before being
    # refetched — projections move as practice reports/injury news land, so
    # unlike `ffbot/history/fetch.py`'s immutable-source cache this must
    # expire. Only read for `source: sleeper`; see `ffbot/projections/cache.py`.
    cache_ttl_minutes: float = 180.0

    # Where fetched projection files are cached. Empty string = the
    # package's own default (`ffbot/projections/cache.py`'s
    # `DEFAULT_CACHE_DIR`).
    cache_dir: str = ""


@dataclass
class RosterSourceConfig:
    """WHERE the in-season roster's NAMES come from — a different question
    from `ProjectionSourceConfig` above, which only covers this WEEK's
    numbers for names already on the roster. This block picks the identity
    list in the first place.

    `"file"` (the default) is today's exact behavior, unchanged: `roster.yml`'s
    `players:` list. `"sleeper"` fetches your live Sleeper roster
    (`sleeper.league_id`/`roster_id`) instead — real names, live team,
    and live injury status, all in one call. `roster.yml` stays relevant
    even under `"sleeper"`: it's read as an optional per-player FLAG overlay
    (`undroppable`/`keeper_round`/`note`/`blocking` — human judgment Sleeper
    cannot supply), matched onto the live roster by name, never as identity.
    A failed live fetch always falls back to `"file"` for that run, with the
    reason surfaced as an alert — never a crash, same contract
    `ProjectionSourceConfig`'s `"sleeper"` already uses.
    """

    source: str = "file"  # "file" | "sleeper"

    # How long a fetched roster is trusted before being refetched.
    cache_ttl_minutes: float = 30.0


@dataclass
class StandingsSourceConfig:
    """WHERE `league.yml`'s `teams:`/`my_team`/`my_opponent`/`week` standings
    block comes from — the same "source" pattern as `RosterSourceConfig`,
    applied to `denial.py`'s data requirement instead of roster identity.
    Denial (`season.denial_weight`/`denial_opponent_boost`/`denial_seed_window`)
    and `matchup_variance_weight` are all exact no-ops without a populated
    standings block, regardless of their own weight — this is the switch
    that actually feeds them.

    `"file"` (the default) is today's exact behavior: whatever is hand-typed
    into `league.yml`, unchanged. `"sleeper"` derives standings from
    Sleeper's own rosters/users/matchups endpoints (see
    `ffbot.sleeper_standings`) and merges them UNDER whatever `league.yml`
    already states — a hand-curated `teams:` entry (e.g. a corrected seed)
    always wins, the same precedence `weekly/week-NN.yml` has over live
    projections. A failed fetch degrades to `"file"` behavior with a
    surfaced alert, never a crash — same contract as every other live seam.
    """

    source: str = "file"  # "file" | "sleeper"
    cache_ttl_minutes: float = 30.0


@dataclass
class LeagueRostersSourceConfig:
    """WHERE the OTHER 11 teams' rosters come from — the free-agent-pool
    exclusion set `week.waiver_candidates`/`rank_streamers` need so a
    recommendation never suggests adding a player someone else already
    owns. Same "source" pattern as `RosterSourceConfig`/`StandingsSourceConfig`.

    `"file"` (the default) is today's exact behavior: whatever
    `league_rosters.yml` holds, a snapshot `scripts/import_league_rosters.py`
    writes on demand — it goes stale the moment a waiver claim processes
    unless something re-runs that script. `"sleeper"` fetches every
    roster fresh on every `report.load_everything` call
    (`ffbot.league_rosters.fetch_league_rosters`, an exact `player_id` join
    via `client.rosters()`/`client.league_users()` — no fuzzy matching, no
    staleness possible) — needs only `sleeper.league_id`, same as
    `roster_source: sleeper`. A failed fetch falls back to the YAML file
    with a surfaced alert, never a crash, same contract as every other live
    seam here.
    """

    source: str = "file"  # "file" | "sleeper"
    cache_ttl_minutes: float = 30.0


@dataclass
class GameConditionsConfig:
    """Auto-fetched weather + market game conditions, merged UNDER whatever
    `weekly/week-NN.yml` already states by hand — see `ffbot.live.conditions`.
    Exists so an unattended scheduled run (no human running `/gameday`) still
    gets real wind/precip/team-total numbers instead of silently sitting at
    a no-op 1.0x multiplier on `season.weather_weight`/`vegas_weight`.

    `weather_source`/`odds_source` are independent switches, not one toggle —
    weather (Open-Meteo) and odds (Kalshi game totals/spread) are unrelated
    failure domains with unrelated providers, mirroring how B4/B5 already
    treat weather and Vegas as two separate dials on the information axis.
    A fetch failure on either degrades to "no data this run" with a
    surfaced alert, never a crash, and never overwrites a field the human
    already filled in.
    """

    weather_source: str = "off"  # "off" | "open_meteo"
    odds_source: str = "off"  # "off" | "kalshi"
    cache_dir: str = ""
    cache_ttl_minutes: float = 180.0

    # Series tickers Kalshi is allowed to be read from for game-level odds
    # (full-game total/spread). An ALLOWLIST, not a hardcoded set inside the
    # client — Kalshi keeps self-certifying new series (see
    # `ffbot/markets/kalshi.py`'s own scoping notes) and today's tickers are
    # not guaranteed to be tomorrow's.
    kalshi_odds_series: list[str] = field(
        default_factory=lambda: ["KXNFLTOTAL", "KXNFLSPREAD"]
    )


@dataclass
class SleeperConfig:
    """Sleeper league identity and cache behavior — the unauthenticated
    replacement for the old top-level `league_id`/`team_key`, which named a
    Yahoo league/team and needed OAuth to resolve at all. Sleeper's
    `league_id` is a public, sufficient key on its own; `username` is only
    needed once, by `scripts/whoami.py`'s discovery flow (or
    `import_league_rosters.py`'s auto-import route), to look it up in the
    first place.
    """

    # The Sleeper league id (a long numeric string). Discover it with
    # `scripts/whoami.py --username <you>`, or copy it from the league's
    # Sleeper URL (`sleeper.com/leagues/<league_id>/...`).
    league_id: str = ""

    # Your Sleeper username. Only needed for discovery (`whoami.py`) and the
    # rival-roster auto-import — day-to-day runs use `league_id`/`roster_id`
    # directly and never look this up.
    username: str = ""

    # Your roster's id within the league — a small int (e.g. 4), what every
    # Sleeper endpoint uses to mean "my team". `scripts/whoami.py` prints
    # this. Leave unset (`None`) to have it resolved from `username` each
    # run instead — one extra lookup, cached like everything else here.
    roster_id: int | None = None

    # Where fetched Sleeper responses are cached. Empty string = the
    # package's own default (`ffbot/sleeper/cache.py`'s `DEFAULT_CACHE_DIR`).
    cache_dir: str = ""

    # How long a fetched roster list is trusted before being refetched — the
    # one Sleeper endpoint most likely to move mid-week (waivers, trades).
    # Other endpoints use fixed, less-volatile defaults baked directly into
    # `ffbot/sleeper/client.py`, since they change on a much slower clock.
    roster_cache_ttl_minutes: float = 30.0


@dataclass
class DropPolicyConfig:
    """Guardrails on the one irreversible action the agent can take."""

    # Players that may never be dropped, by name (case-insensitive).
    never_drop: list[str] = field(default_factory=list)

    # Anyone rostered in at least this share of leagues is protected — high
    # ownership (from Sleeper's ownership% research endpoint, see
    # `ffbot/sleeper/client.py::ownership`) is the best available proxy for
    # "this is a real asset".
    protect_pct_owned: float = 60.0

    # Players taken this early in the draft are protected.
    protect_draft_rounds: int = 4


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

    # WHERE the frozen board's season-total POINTS come from — "csv" (the
    # default: today's behavior, FantasyPros' points from board_csv,
    # unchanged) or "sleeper" (live Sleeper season projections overlaid on
    # top, winning `points` while board_csv still supplies adp/bye/
    # adp_spread — see CLAUDE.md's "hybrid draft board" design; Sleeper's
    # season endpoint carries points and ADP but no bye weeks and no
    # cross-site ADP spread). A failed live fetch always falls back to
    # "csv"-only behavior with a warning, never a crash mid-draft-prep.
    board_points_source: str = "csv"
    board_points_cache_ttl_minutes: float = 360.0

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
    # `run_window` OPPONENT picks share a position (my own picks are not a
    # signal about what the room is doing -- see `draft.alerts`).
    run_window: int = 10
    run_threshold: int = 5

    # Alerts (`draft.alerts`). A hard ceiling on how many lines one render
    # can show: the panel is only useful while it is usually empty, so when
    # everything fires at once the least actionable ones are dropped rather
    # than shown. Ordered RUN, then WAIT, then BYE.
    alert_limit: int = 6

    # The `WAIT` alert -- "what does passing on this position until my next
    # turn cost?" -- fires when the best player available at a position now
    # projects at least this many season points above the best one likely to
    # still be there at my next turn. A season-points floor rather than a
    # relative one on purpose: the board genuinely drops 20+ points at a
    # position in round 2 and 2 points in round 12, and only the first of
    # those is worth interrupting a pick for. `alert_survival_floor` is what
    # "likely to still be there" means (see `draft.survival`).
    alert_gap_points: float = 12.0
    alert_survival_floor: float = 0.5

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
    # decision scale (like every edge weight). Set by `DRAFT_SPICE_PRESETS`
    # (nonzero from level 2 up -- the "VOR/value" feature that level names).
    # B7's isolation sweep (`scripts/backtest_draft.py`, level-1-vs-level-1
    # + this override alone, train 2021-2023, 30 seeds) found a directionally
    # POSITIVE but not-yet-significant signal at 2x this value (+19.32 season
    # pts, 95% CI [-3.80, +46.97] -- barely includes zero); this shipped
    # value showed an exact no-op in isolation (0 of 90 paired drafts
    # differed) -- neutral-inconclusive, not confirmed either way. 0.0 (the
    # default, and level 1) disables.
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

    # Weight on researched breakout potential from draft/intel.yml. B5 found
    # this dial STRUCTURALLY DEAD in `scripts/backtest_draft.py` for the
    # same reason `volatility_weight`/`upside_lean_weight` were dead
    # weekly before B4's signal-provider seam: `ffbot.history.board.
    # historical_board` (the historical draft board, no `intel.yml`
    # equivalent) never populates `BoardPlayer.upside` -- verified directly
    # (every sampled player's `.upside` is `None`). A LIVE draft board
    # loaded via `ffbot.board.load_board` with a real `draft/intel.yml`
    # DOES populate it, so this is not necessarily dead in production --
    # only unmeasurable by the historical replayer as it stands today. A
    # historical intel-equivalent provider (same seam signals.py filled on
    # the weekly side) is the natural follow-up, not built this session.
    upside_weight: float = 0.0

    # Weight on researched *availability* risk (suspension, PUP, holdout,
    # no-timetable injury) — the one intel signal that subtracts. Unramped:
    # a suspension is as real in round 1 as round 10. Same B5 dead-dial
    # finding as `upside_weight` above -- `historical_board` never
    # populates `BoardPlayer.availability_risk`.
    risk_weight: float = 0.0

    # How strongly the static pre-draft export (board.txt etc.) bakes in
    # upside and risk, in season points at a full 100 score. The live layer
    # scales per pick; a static list can't, so this is a fixed, modest unit.
    # 0.0 = the export stays pure VOR.
    export_intel_scale: float = 0.0

    # Weight on cross-site ADP disagreement as a boom/bust proxy. Same B5
    # dead-dial finding as `upside_weight`/`risk_weight` above --
    # `historical_board` sources ADP from a single provider (FFC), so
    # `BoardPlayer.adp_spread` (which needs multiple merged CSV exports,
    # see `board._merge_csv_rows`) is never populated either.
    volatility_weight: float = 0.0

    # Bonus for completing a QB / pass-catcher stack.
    stack_bonus: float = 0.0

    # Blends `stack_bonus` from a flat constant (0.0, the default -- EXACT
    # current behavior, bit-identical for every existing config including
    # config.yml's stack_bonus: 0.20) toward scaling by the stacked
    # partner's own VOR (1.0 -- a stack with a QB1 correlates real
    # touchdowns far more than a stack with a backup). See
    # `edge.stack_magnitude`.
    stack_magnitude_weight: float = 0.0

    # Penalty for how many of the candidate's own NFL teammates are already
    # rostered, as a fraction of decision scale per teammate -- a portfolio
    # risk (one QB injury/OL collapse/coordinator change hits every
    # same-team player at once) no per-player projection can see. Separate
    # from `stack_bonus` rather than unified with it, so an existing
    # `stack_bonus` config is never silently reinterpreted. Unramped, like
    # `risk_weight`: concentration risk doesn't care what round it is. Blank
    # team (DEF) never concentrates -- same guard `stack_match` uses. Set by
    # `DRAFT_SPICE_PRESETS` (nonzero from level 2 up -- the "anti-over-
    # stacking" feature that level names). B7's isolation sweep at this
    # shipped value found an exact no-op (0/90 paired drafts differed); at
    # 2x this value, +3.48 season pts, 95% CI [-34.91, +37.81] -- both
    # neutral-inconclusive, not confirmed either way. 0.0 (the default, and
    # level 1) is an exact no-op.
    team_concentration_weight: float = 0.0

    # Same idea as `team_concentration_weight`, but counts only teammates at
    # the candidate's OWN position -- a second same-team WR cannibalizes the
    # first one's targets, a narrower and often larger risk than "the whole
    # offense" that a generic team count under-weights. Additive with
    # `team_concentration_weight`, not a replacement for it. Set by
    # `DRAFT_SPICE_PRESETS`, same level-2-up basis as `team_concentration_
    # weight`; B7's isolation sweep found an exact no-op at both this value
    # and 2x it (0/90 paired drafts differed either way) -- neutral-
    # inconclusive. 0.0 (the default, and level 1) is an exact no-op. See
    # `edge.team_concentration_penalty`.
    same_team_position_weight: float = 0.0

    # Penalty for how much a candidate's bye week compounds an EXISTING
    # bye-week hole at their position on my roster -- e.g. a 3rd RB whose
    # bye matches two others already rostered, on a league with only one
    # flex slot to absorb it. Computed via the same optimizer the exact
    # `BYE:` alert in `draft.alerts()` uses (flex-aware, unlike a naive
    # per-position count), memoized once per (position, bye week) pair per
    # `recommend()` call rather than per candidate. Fraction of decision
    # scale, unramped -- a bye collision is roster construction, not a
    # variance bet. Set by `DRAFT_SPICE_PRESETS` (nonzero from level 2 up --
    # the "bye-week awareness" feature that level names). B7's isolation
    # sweep found an exact no-op at both this value and 2x it (0/90 paired
    # drafts differed either way) -- neutral-inconclusive, not confirmed
    # harmful. 0.0 (the default, and level 1) is an exact no-op and skips
    # the extra optimizer calls entirely. See `draft._bye_pressure`.
    bye_collision_weight: float = 0.0

    # RETIRED -- do not raise this above 0.0 without a fresh backtest. B5's
    # `scripts/backtest_draft.py` (train: 2021-2023, 20 seeds/season, 60
    # paired full drafts) isolated this weight alone at its LIVE config.yml
    # value (0.20) against a zero-edge control and found a confirmed loss:
    # -27.0 season pts, 95% CI [-36.4, -22.2] -- excludes zero. Every
    # nonzero value tested trended the same direction (0.05/0.10: -4.1 CI
    # touching zero; 0.20: clearly excludes zero) -- monotonic harm, same
    # shape as `SeasonConfig.game_script_weight`'s retirement. See
    # docs/BACKTEST.md's B5 section.
    arbitrage_weight: float = 0.0

    # Weight on the gap between league-scored points and consensus PPR points
    # (`BoardPlayer.points - points_fp`, computed once the board is scored
    # under `league.yml`'s rules — see `ffbot/scoring.py`). ADP is set by the
    # consensus PPR market, so this is the one edge signal that market
    # disagreement can never correct: the market isn't playing your league.
    # 0.0 (the default) is an exact no-op, same as every other edge weight.
    # B5's isolated sweep (scoring_arbitrage_weight=0.10 alone, 60 paired
    # drafts) never once flipped a recommendation -- an exact-zero measured
    # effect in THIS league's scoring rules, not confirmed harm the way
    # `arbitrage_weight` above got. Left at a modest nonzero value in
    # `DRAFT_SPICE_PRESETS` on the theory that a league with rules further
    # from standard PPR would see this gap matter more; not itself
    # validated positive.
    scoring_arbitrage_weight: float = 0.0

    # Variance tolerance ramps from 0 at `risk_ramp_start` to 1 at
    # `risk_ramp_full`, and scales the upside and volatility terms. Floor
    # early, ceiling late — early picks are most of your roster's points,
    # while a late-round replacement body is worth roughly nothing, so a
    # small chance at a league-winner is the better bet there.
    risk_ramp_start: int = 2
    risk_ramp_full: int = 5

    # Weight on positional demand from teams picking before my next turn --
    # "deny the position an opponent needs" as a tie-break. Fraction of
    # decision scale, unramped (who picks next doesn't depend on variance
    # tolerance), gated by the same contender pool as every other edge term
    # -- it can reorder players already near the top of my list, never
    # promote one I don't otherwise want purely to deny a rival. Set by
    # `DRAFT_SPICE_PRESETS` (nonzero from level 2 up -- the "tactical
    # blocking" feature that level names). B7's isolation sweep found an
    # exact no-op at both this value and 2x it (0/90 paired drafts differed
    # either way) -- neutral-inconclusive; `demand_ahead` needs real rival-
    # roster fidelity this synthetic historical-board sim may not fully
    # replicate. 0.0 (the default, and level 1) is an exact no-op. See
    # `draft.demand_ahead`.
    block_weight: float = 0.0

    # Weight on Kalshi's public season-long NFL markets (default series:
    # "Top Fantasy Rank" -- see `ffbot.markets.kalshi_nfl.draft_signal`),
    # percentile-ranked within position. Fraction of decision scale,
    # unramped -- a market-implied signal is a fact to weigh, not a
    # variance bet to lean into only late, same reasoning as
    # `scoring_arbitrage_weight`. SPICE LEVEL 4 ONLY (see
    # DRAFT_SPICE_PRESETS): Kalshi's NFL markets have zero overlap with
    # this repo's 2021-2024 backtest window (they launched September 2025),
    # so unlike every other edge weight this one ships on zero evidence,
    # the same honest caution `SeasonConfig.kalshi_weight` is held to. 0.0
    # (the default, and every level below 4) is an exact no-op AND skips
    # the fetch entirely -- see `scripts/draft.py`'s `build_state`, the
    # same "don't even ask" pattern `block_weight == 0.0` already uses for
    # `demand_ahead`.
    kalshi_weight: float = 0.0

    # Where researched player intel lives. Missing file = no intel, and every
    # value stays bit-identical to a run without it.
    intel_file: str = "draft/intel.yml"

    # UI / sync.
    recommend_count: int = 12
    # How many rows the web GUI's recommendation panel loads (scrollable,
    # only the first ~10 shown by default) -- distinct from `recommend_count`
    # so the terminal UI's fixed-height table is unaffected.
    gui_recommend_count: int = 20
    sync_poll_seconds: int = 5

    # B5/B7 -- the draft-side analog of `SeasonConfig.spice_level`. `None` (the
    # default) means "every edge weight above is whatever this dataclass
    # already resolved to" -- config.yml's own hand-narrated values, or the
    # bare 0.0 defaults -- an EXACT no-op, so every config.yml written
    # before this field existed keeps behaving bit-identically. Only
    # `_draft_from_dict` (a `draft.spice_level: N` key in config.yml) or
    # `DraftConfig.from_spice_level(N)` ever resolves a preset; a plain
    # `DraftConfig(spice_level=3)` constructor call does NOT auto-apply one
    # -- the same asymmetry `SeasonConfig.spice_level` already has (see
    # tests/test_config.py's `TestDefaults` for that precedent) rather than
    # a new one this field invents.
    spice_level: int | None = None

    def __post_init__(self) -> None:
        if self.order not in ("snake", "linear"):
            raise ConfigError(
                f"config.yml [draft]: order must be 'snake' or 'linear', got {self.order!r}"
            )

    @classmethod
    def from_spice_level(cls, level: int, **overrides) -> "DraftConfig":
        """Build a DraftConfig from the 1-4 dial, with any explicit field in
        `overrides` winning over the preset -- mirrors
        `SeasonConfig.from_spice_level` exactly."""
        if level not in DRAFT_SPICE_PRESETS:
            raise ValueError(
                f"spice_level must be 1-4, got {level} (the scale changed from 1-5 in B7 -- "
                "old 3/4 map to new 3, old 5 maps to new 4; see docs/SPICE.md)"
            )
        fields = dict(DRAFT_SPICE_PRESETS[level])
        fields["spice_level"] = level
        fields.update(overrides)
        return cls(**fields)


# B7 -- the draft-side ladder, rescaled to 4 levels and now also carrying the
# five structural terms (`team_concentration_weight`/`same_team_position_
# weight`/`bye_collision_weight`/`block_weight`/`balance_weight`) that B5
# left permanently out of the ladder. See docs/SPICE.md for the full audit;
# concretely, by level:
#
#   1 Baseline  -- VOR-chalk: `draft.recommend()` with every edge weight at
#     zero (pure value-over-replacement need + bench depth). B7 measured
#     what this buys over blind ADP directly (`--agent-policy adp` vs. a
#     zeroed `recommend()` control, train 2021-2023, 30 seeds): +123.15
#     season pts, 95% CI [+16.10, +309.86] -- excludes zero, the strongest
#     signal this whole audit found. This is why level 1 is VOR-chalk, not
#     literal blind-ADP-following: the assistant is worth using even at its
#     most conservative setting.
#   2 Tactician -- the five structural terms turn on (anti-over-stacking,
#     tactical block/denial, bye-collision awareness, roster-balance
#     urgency) plus `scoring_arbitrage_weight`. B7's isolation sweep
#     (`scripts/backtest_draft.py`, each dial alone vs. a level-1 control,
#     train 2021-2023, 30 seeds) found every one of these an exact no-op or
#     a wide, zero-crossing CI at its shipped value -- see each field's own
#     docstring for the specific numbers. None is confirmed harmful; all
#     are shipped on judgment, matching config.yml's own pre-existing
#     hand-set values for continuity. `upside_weight`/`risk_weight`/
#     `volatility_weight`/`kalshi_weight` stay at 0 -- no outside/variance
#     features yet, matching this level's own "no outside data" definition.
#   3 Sharp     -- `upside_weight`/`risk_weight`/`volatility_weight` turn on
#     at their B5-era judgment values (still structurally UNMEASURABLE by
#     this backtest: `ffbot.history.board.historical_board` never populates
#     `BoardPlayer.upside`/`.availability_risk`/`.adp_spread` -- no
#     `intel.yml` equivalent, single-source ADP). `stack_bonus` also turns
#     on here -- pro-stacking is a deliberate variance play, matching this
#     level's "probabilistic-upside risk taking" definition, not a level-2
#     structural feature. `scoring_arbitrage_weight` reaches its final
#     value (B5 measured an exact zero effect at 0.10 -- kept on theory,
#     not confirmed positive).
#   4 Use at your own risk -- `upside_weight` climbs, `risk_weight` DROPS
#     (non-monotonic by design: higher spice tolerates more availability
#     risk), `volatility_weight`/`stack_bonus` climb further, `risk_ramp_*`
#     moves earlier/faster (1->3 instead of 2->5). `kalshi_weight` turns on
#     here ONLY -- Kalshi's NFL markets postdate this repo's entire
#     backtest window (launched September 2025), so like its weekly
#     counterpart this ships on zero evidence, the defining trait of this
#     level ("every feature, even untested ones").
#
# `arbitrage_weight` stays RETIRED (its own docstring above has the
# numbers) -- excluded at every level, not just left at 0.0 by coincidence.
DRAFT_SPICE_PRESETS: dict[int, dict[str, Any]] = {
    1: dict(  # Baseline -- VOR-chalk, no edge terms, no structural tactics at all.
        scoring_arbitrage_weight=0.0,
        upside_weight=0.0, risk_weight=0.0, volatility_weight=0.0, stack_bonus=0.0, kalshi_weight=0.0,
        team_concentration_weight=0.0, same_team_position_weight=0.0, bye_collision_weight=0.0,
        block_weight=0.0, balance_weight=0.0,
        risk_ramp_start=2, risk_ramp_full=5,
    ),
    2: dict(  # Tactician -- anti-stacking/block/bye/balance turn on. No outside data yet.
        scoring_arbitrage_weight=0.05,
        upside_weight=0.0, risk_weight=0.0, volatility_weight=0.0, stack_bonus=0.0, kalshi_weight=0.0,
        team_concentration_weight=0.06, same_team_position_weight=0.10, bye_collision_weight=0.15,
        block_weight=0.20, balance_weight=0.30,
        risk_ramp_start=2, risk_ramp_full=5,
    ),
    3: dict(  # Sharp -- upside/risk/volatility/stack turn on; probabilistic-upside risk-taking begins.
        scoring_arbitrage_weight=0.10,
        upside_weight=0.30, risk_weight=0.40, volatility_weight=0.20, stack_bonus=0.15, kalshi_weight=0.0,
        team_concentration_weight=0.06, same_team_position_weight=0.10, bye_collision_weight=0.15,
        block_weight=0.20, balance_weight=0.30,
        risk_ramp_start=2, risk_ramp_full=5,
    ),
    4: dict(  # Use at your own risk -- deeper tilt, Kalshi turns on, risk ramp earlier/faster.
        scoring_arbitrage_weight=0.10,
        upside_weight=0.65, risk_weight=0.20, volatility_weight=0.50, stack_bonus=0.30, kalshi_weight=0.15,
        team_concentration_weight=0.06, same_team_position_weight=0.10, bye_collision_weight=0.15,
        block_weight=0.20, balance_weight=0.30,
        risk_ramp_start=1, risk_ramp_full=3,
    ),
}


def _draft_from_dict(raw: dict[str, Any]) -> DraftConfig:
    """Build the draft block: `spice_level` (if present) sets the preset,
    any other key in `raw` overrides that one field on top -- identical
    contract to `_season_from_dict`. No `spice_level` key at all (the
    common case today) falls straight through to the generic `_construct`
    path, so this is a bit-identical no-op for every config.yml written
    before this field existed.
    """
    if "spice_level" not in raw:
        return _construct(DraftConfig, "config.yml [draft]", raw)
    raw = dict(raw)
    level = raw.pop("spice_level")
    try:
        return DraftConfig.from_spice_level(level, **raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"config.yml [draft]: {exc}") from exc


@dataclass
class SeasonConfig:
    """The in-season weekly manager — start/sit, waivers, streaming.

    Mirrors `DraftConfig`'s shape: every weight is a fraction of the decision
    at hand (the projection gap between the real options that week) rather
    than an absolute point total — the same lesson `edge.py` learned the hard
    way, that a fixed bonus is invisible in a blowout-margin week and
    dominant in a coin-flip week unless it scales with the decision itself.

    Unlike the draft, the primary dial is `spice_level` (1-4, see B7's
    docs/SPICE.md), not the dozen-plus raw weights it resolves to. The
    reason is explicit: weather and Vegas signals alone mostly agree with
    consensus on a calm week with no bad forecast and no lopsided game —
    which would make the system read as "just follow the platform" more
    often than not. `volatility_weight` and `upside_lean_weight` exist
    specifically so a genuinely close start/sit call can still go to the
    higher-ceiling player on a placid week, which is what keeps the system
    from collapsing to the safe, boring answer.
    """

    # Where this week's researched intel lives. A missing file degrades to
    # "no status overrides, no weather/vegas adjustment, no notes" — the
    # optimizer still runs on projections alone.
    weekly_intel_file: str = ""  # e.g. "weekly/week-03.yml"; set per run

    # The one dial: 1 (Baseline — blind highest-projected-points, no
    # tactics at all: no VOR-aware waivers, no blocking/denial, no bye
    # planning, no over-stacking awareness) through 4 (Use at your own risk
    # — every feature this repo has, including untested ones like per-player
    # Kalshi odds; deliberately contrarian and higher-variance; excludes
    # only CONFIRMED-harmful weights, not merely unproven ones). 3 (Sharp —
    # every evidence-backed outside feature turned on, still cautious about
    # variance) is the default. Setting this is enough on its own — see
    # `SeasonConfig.from_spice_level` and docs/SPICE.md for the full
    # feature-by-level breakdown and the backtest evidence behind each cell.
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

    # Game-script tilt from the SPREAD (team_total - opp_total), orthogonal
    # to the implied-total tilt above: two players with an identical team
    # total have opposite outlooks as a 10-point favorite vs. a 10-point
    # underdog -- the favorite runs clock (RB volume up, pass volume down),
    # the underdog throws from behind (QB/WR volume up, RB down).
    # `game_script_scale` is the point margin at which the multiplier
    # reaches its full swing; `game_script_weight` is that swing's size, as
    # a fraction of a player's own points. Needs no new data -- team_total
    # and opp_total are already computed everywhere GameInfo is.
    #
    # RETIRED, not just disabled -- do not raise this above 0.0 without a
    # fresh backtest. B6 found every positive weight a confirmed loss
    # (weight 0.30: test delta -1.570, 95% CI [-2.40, -0.74]) and negative
    # weights degrading just as monotonically -- symmetric degradation in
    # both directions, ruling out a sign error. B5 tested the leading
    # hypothesis directly (garbage-time volume keeping a blown-out
    # favorite's WR productive despite the "run more, pass less" theory
    # this term encodes) by zeroing the QB/WR/TE discount via
    # `game_script_underdog_scale` below and sweeping weight x scale on
    # train (2021-2023, 300 rosters/week): holding weight and
    # `favorite_scale` fixed, harm rises monotonically as `underdog_scale`
    # climbs 0 -> 0.5 -> 1 (weight 0.30, favorite_scale=1: train delta
    # +0.126 -> -0.135 -> -1.741) -- confirms the pass-catcher discount is
    # the harmful component, exactly as hypothesized. But dropping it only
    # moves the term from confirmed loss to statistically indistinguishable
    # from zero (favorite_scale=1, underdog_scale=0, weight=0.15: train
    # delta +0.034, 95% CI [-0.43, +0.46] -- crosses zero); no cell in the
    # sweep cleared zero on the positive side. Diagnosis confirmed, fix
    # applied, but "does no harm" is not "worth turning on" -- retiring at
    # 0.0 rather than shipping a noise-floor result. See docs/BACKTEST.md's
    # B5 section for the full sweep.
    game_script_weight: float = 0.0
    game_script_scale: float = 10.0

    # Independent multipliers on the two HALVES of `GAME_SCRIPT_LEAN`
    # (`week.py`) -- `favorite_scale` scales the positive leans (RB, DEF),
    # `underdog_scale` scales the negative ones (QB, WR, TE). Both default
    # to 1.0, reproducing the original single-lean-table behavior exactly
    # (a bit-identical no-op at those defaults, same as `game_script_weight
    # == 0.0` already is regardless of these). Kept, even though
    # `game_script_weight` above is retired, because they're what made the
    # retirement sweep possible at all (`--grid game_script_underdog_scale=0`
    # instead of a source edit) and cost nothing to leave in place -- a
    # future session revisiting this needs them again either way.
    game_script_favorite_scale: float = 1.0
    game_script_underdog_scale: float = 1.0

    # Boom/bust lean: on a close start/sit call, prefer the researched
    # higher-variance player. Drawn from the same `adp_spread`-style
    # cross-source disagreement idea as the draft's volatility term, applied
    # weekly (projection disagreement across sources, when available, else
    # researched boom/bust notes).
    volatility_weight: float = 0.0

    # How much researched `upside` (breakout/spike-week potential, distinct
    # from availability risk) can tip a close call toward the flagged player.
    upside_lean_weight: float = 0.0

    # How much recent usage/opportunity TREND (target share / air-yards
    # share, via WOPR) can tip a close call, same additive-fraction-of-scale
    # shape as volatility/upside_lean -- see `week.usage_score` and
    # `ffbot.history.signals.usage_form`, the historical provider that
    # populates it. Reachable live too: a `usage_trend: 0-100` key under a
    # player's `weekly/week-NN.yml` entry (see `week._parse_player_entry`).
    # 0.0 is a no-op.
    usage_weight: float = 0.0

    # How much recent SCORING momentum (points, not opportunity) vs. season
    # average can tip a close call -- the direct "hot player stays hot"
    # question, tested against `usage_weight` above as its role-based
    # counterpart. See `week.momentum_score` and
    # `ffbot.history.signals.scoring_form`; reachable live via a `momentum:
    # 0-100` key in `weekly/week-NN.yml`. 0.0 is a no-op. B5's finding on
    # whether this or `usage_weight` carries more signal is in
    # docs/BACKTEST.md.
    momentum_weight: float = 0.0

    # How much a player's ROLE trending up faster than their PRODUCTION (or
    # vice versa) can tip a close call -- see `week.divergence_score` and
    # `ffbot.history.signals.usage_divergence`; reachable live via a
    # `divergence: 0-100` key in `weekly/week-NN.yml`. 0.0 is a no-op.
    divergence_weight: float = 0.0

    # How much Kalshi's public NFL prop markets disagreeing with the
    # shipped projection (see `week.kalshi_score` and
    # `ffbot.markets.kalshi_nfl`) can tip a close call -- SPICE LEVEL 4
    # ONLY (see SPICE_PRESETS), never turned on by any lower level, on the
    # same "untested, not confirmed-harmful" basis `venue_disruption_weight`
    # is held to: Kalshi's NFL player-prop markets launched September 2025,
    # zero overlap with this repo's 2021-2024 backtest window, so there is
    # no train/test evidence at any level, positive or negative. Reachable
    # live via a `kalshi: 0-100` key in `weekly/week-NN.yml` too. 0.0 (the
    # default, and every level below 4) is an exact no-op AND skips the
    # fetch entirely -- see `ffbot.report.load_everything`'s guard, the same
    # "don't even ask" pattern `draft.demand_ahead`'s `block_weight == 0.0`
    # check uses.
    kalshi_weight: float = 0.0

    # Conditions volatility_weight/upside_lean_weight on how big an
    # underdog (favors variance) or favorite (favors floor) this week's
    # matchup makes you -- see `week.matchup_lean`/`_variance_multiplier`.
    # 0.0 (the default) is an exact no-op: the multiplier stays 1.0
    # regardless of the computed lean. Needs `league.yml`'s `my_opponent`
    # plus `league_rosters.yml` to produce a nonzero lean in the first
    # place; absent either, this stays a no-op even when nonzero.
    matchup_variance_weight: float = 0.0

    # Discount for a game flagged `international: true` in `weekly/week-NN.yml`
    # (see `GameInfo.international`) — unfamiliar time zone, travel, a smaller
    # crowd, whatever "not a typical NFL setting" turns out to mean for a given
    # game. Applies to BOTH teams' offensive skill positions (K/QB/WR/TE/RB;
    # DEF exempt, same reasoning as `weather_multiplier`), as a fraction of
    # that week's decision scale. Unlike weather/Vegas, there is still no real
    # evidence base for how much an international game actually moves output
    # — INCONCLUSIVE, not confirmed-harmful or confirmed-helpful, no train/
    # test season has ever isolated it. B7 (see docs/SPICE.md) ships it at
    # level 4 ONLY on that basis: "use at your own risk" is explicitly defined
    # as "every feature, even untested ones, excluding only confirmed-harmful
    # weights" — the same reasoning `kalshi_weight` below is held to. 0.0 (the
    # default, and every level below 4) is an exact no-op.
    venue_disruption_weight: float = 0.0

    # Which positions the weekly manager scans for a streaming upgrade —
    # K/DEF (the classic streamable spots) by default. Not spice-laddered
    # (a plain field, present at every level unchanged) — this is a roster-
    # shape fact, not a strategy dial. The GUI folds these results straight
    # into its "add/drop" recommendations rather than asking for positions
    # up front; `scripts/week_report.py --stream` still lets the CLI
    # override this per run.
    stream_positions: list[str] = field(default_factory=lambda: ["K", "DEF"])

    # Streaming K/DEF: blend fraction between season-long floor value (0.0)
    # and this week's pure matchup value (1.0) when ranking streaming
    # candidates — a streamer's whole point is that this week's matchup
    # dominates their season-long track record.
    streaming_weight: float = 0.5

    # Rest-of-season value blended into this week's number for waiver
    # ranking, so a one-week hot streak doesn't outrank a real weekly starter.
    # 0.0 = pure this-week value; 1.0 = pure ROS value.
    ros_blend: float = 0.5

    # How `week.waiver_candidates` ranks and pairs drops. "marginal" (the
    # default, and every level >= 2) is the VOR-aware machinery this module
    # has always used: `hold_margin`/`drop_cost` (replacement-subtracted
    # season-long lineup value, via `draft._depth_factors`) rank candidates
    # and choose a paired drop. "points" is the deliberately naive B7 level-1
    # mode -- rank purely by this week's raw projected points (live points
    # where covered, else `bp.points` prorated over the weeks remaining,
    # bye-zeroed), pair the drop by lowest projected points among droppable
    # players. `policy.can_drop`'s safety guardrails apply either way -- see
    # `week.waiver_candidates`'s own docstring for exactly what "points" mode
    # skips (replacement subtraction, `hold_margin`, `ros_blend`, the
    # `gain <= 0` filter). Set by `SPICE_PRESETS`, not hand-tuned.
    waiver_value_mode: str = "marginal"

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
    # that this hold is about denying a rival, not about your own lineup. Set
    # by `SPICE_PRESETS` (nonzero from level 2 up, the "tactical blocking"
    # feature B7's level-2 semantics call for) — unmeasured by any backtest
    # (`ffbot.backtest.season` never sets `blocking: true` on a synthetic
    # roster), so this is a judgment-set flat value, not a validated one. 0.0
    # (the default, and level 1) is a no-op; `hold_margin` cannot see denial
    # value on its own, since it is a function of your roster alone.
    blocking_hold_bonus: float = 0.0

    # Weight on denying a contested player to a rival, as a fraction of
    # decision scale — see `week.denial_value`. Set by `SPICE_PRESETS`
    # (nonzero from level 2 up, same "tactical...denial" feature as
    # `blocking_hold_bonus` above) — this needs `league_rosters.yml`
    # (imported rival rosters) to be anything but zero regardless of this
    # weight, and is unmeasured by any backtest (judgment-set). 0.0 (the
    # default, and level 1) is an exact no-op.
    denial_weight: float = 0.0

    # Flat boost added to a rival's `threat` (see `denial.threat`) when that
    # rival is `league.yml`'s `my_opponent` this week — a head-to-head
    # opponent's need is worth more than "some rival's" bubble-distance
    # alone captures, since denying them has a direct effect on your own
    # matchup. Set by `SPICE_PRESETS`, same level-2-up/judgment-set basis as
    # `denial_weight`. 0.0 (the default, and level 1) is an exact no-op;
    # also needs `my_opponent` set to be anything but zero.
    denial_opponent_boost: float = 0.0

    # How many playoff seeds away from your own a rival can be and still get
    # a proximity boost toward maximum `threat`, once the season has reached
    # its playoff push (the final few weeks of `regular_season_weeks`) — a
    # team fighting you directly for a seed is dangerous regardless of its
    # distance from the bubble itself. Set by `SPICE_PRESETS`, same
    # level-2-up/judgment-set basis as `denial_weight`. 0 (the default, and
    # level 1) disables this; also needs your own seed known (a `teams:`
    # entry matching `my_team`) to be anything but zero.
    denial_seed_window: int = 0

    # Waivers are always rolling-priority (no FAAB path exists). How
    # expensive spending your current priority is, as a fraction of
    # decision scale at priority 1 (most expensive) fading
    # to ~0 at the bottom of the list. See `week.claim_cost`. Set by
    # `SPICE_PRESETS`, same level-2-up/judgment-set basis as `denial_weight`
    # above — level 1's naive "points" waiver mode skips this economic
    # reasoning entirely, same as it skips VOR. 0.0 (the default, and level
    # 1) means every claim costs nothing regardless of priority.
    priority_value: float = 0.3

    # Refuse a pure-denial claim (rostering someone you'd never start,
    # solely to keep a rival from getting him — see `denial.py`) when your
    # own rolling priority is this good or better. A denial hold has no
    # lineup value of its own to justify spending a genuinely valuable
    # priority position the way a real add does. Set by `SPICE_PRESETS`,
    # same level-2-up/judgment-set basis as `denial_weight`. 0 (the default,
    # and level 1) = no restriction; 3 refuses spending your top-3 priority
    # on denial alone.
    denial_priority_floor: int = 0

    @classmethod
    def from_spice_level(cls, level: int, **overrides) -> "SeasonConfig":
        """Build a SeasonConfig from the 1-4 dial, with any explicit field
        in `overrides` winning over the preset — the power-user escape hatch
        for tuning one signal without losing the rest of the level's shape.
        """
        if level not in SPICE_PRESETS:
            raise ValueError(
                f"spice_level must be 1-4, got {level} (the scale changed from 1-5 in B7 -- "
                "old 3/4 map to new 3, old 5 maps to new 4; see docs/SPICE.md)"
            )
        fields = dict(SPICE_PRESETS[level])
        fields["spice_level"] = level
        fields.update(overrides)
        return cls(**fields)


# B7 re-derivation: rescaled from 5 levels to 4, with the ladder now
# organized around what a level FEELS like to a user rather than a pure
# information/variance split (see docs/SPICE.md for the full audit, the
# feature-by-level matrix, and every backtest number this ladder rests on):
#
#   1 Baseline  -- blind highest-projected-points. No VOR-aware waivers (see
#     `waiver_value_mode`), no blocking/denial, no bye-collision awareness,
#     no over-stacking awareness, no outside data at all. What picking the
#     platform's own top players, with no strategy, looks like.
#   2 Tactician -- VOR/value-aware waivers, tactical blocking/denial, bye
#     awareness turn on. Deliberately NO outside data yet (weather/Vegas/
#     trend/Kalshi all stay at 0) -- matches this level's own definition.
#     These structural dials (`denial_weight` and its siblings,
#     `blocking_hold_bonus`, `priority_value`) have no backtest evidence
#     either way -- `ffbot.backtest.season` never exercises `blocking: true`
#     or denial, and there's no synthetic-roster equivalent -- so they're
#     judgment-set flat values from levels 2-4, not tuned per level.
#   3 Sharp     -- every EVIDENCE-BACKED outside feature turns on: this is
#     the old validated level 3 unchanged (weather/vegas/usage/momentum/
#     divergence at the exact values that cleared statistical significance
#     on both train (2021-2023, +0.392 pts, 95% CI [+0.11,+0.68]) and the
#     one held-out look at 2024 (+0.487, CI [+0.11,+0.88]) -- still the only
#     spice-ladder cell in this project's history to hold up out of sample.
#     A small, matched variance lean (volatility_weight = upside_lean_weight
#     = 0.05) rides along, also unchanged from the old validated cell.
#   4 Use at your own risk -- every feature this repo has, including
#     untested ones (Kalshi player props, venue_disruption_weight),
#     deliberately contrarian/high-variance, EXCLUDES ONLY CONFIRMED-HARMFUL
#     weights (never merely unproven ones). The info axis holds at its old
#     level-4 values (weather=0.38, vegas=0.32, usage=0.20, momentum=0.20,
#     divergence=0.10). volatility_weight/upside_lean_weight are B7's one
#     genuinely re-tuned pair: a 3x3 grid sweep (train 2021-2023, 400
#     rosters/week, all four signals) found the matched (0.60, 0.60) point
#     CONFIRMED negative (train delta -0.794, 95% CI [-1.39,-0.19], excludes
#     zero) -- so level 4 stops one notch short, at the LARGEST matched pair
#     whose train CI still includes zero: (0.45, 0.45), delta -0.311, CI
#     [-0.84, +0.21]. Negative point estimate, not confirmed harmful -- the
#     "use at your own risk" line this level is named for.
#     `matchup_variance_weight`/`kalshi_weight`/`venue_disruption_weight` are
#     all judgment-set at level 4: none is measurable by the lineup-only
#     replayer (`scripts/backtest_tune.py`'s own `LINEUP_INERT_FIELDS`
#     documents why for the first), and Kalshi has zero backtest-window
#     overlap by construction (launched Sept 2025).
#
# Level 1 is EXACTLY the control: every weight here is 0.0, which makes
# `week.adjusted_players` a bit-identical no-op (every multiplier collapses
# to 1.0, `spice_bonus` to 0.0) -- see
# `tests/test_config.py::TestSpiceLevelOne::test_is_bit_identical_to_control`.
# `waiver_value_mode` is the one non-float field: "points" (naive, level 1
# only) vs "marginal" (VOR-aware, levels 2-4) -- see that field's own
# docstring.
SPICE_PRESETS: dict[int, dict[str, Any]] = {
    1: dict(  # Baseline -- blind highest-projected-points, no tactics, no outside data at all.
        weather_weight=0.0, vegas_weight=0.0,
        volatility_weight=0.0, upside_lean_weight=0.0, matchup_variance_weight=0.0,
        usage_weight=0.0, momentum_weight=0.0, divergence_weight=0.0, kalshi_weight=0.0,
        venue_disruption_weight=0.0,
        streaming_weight=0.0, waiver_value_mode="points",
        blocking_hold_bonus=0.0, denial_weight=0.0, denial_opponent_boost=0.0,
        denial_seed_window=0, denial_priority_floor=0, priority_value=0.0,
    ),
    2: dict(  # Tactician -- VOR-aware waivers, blocking/denial/bye awareness. No outside data yet.
        weather_weight=0.0, vegas_weight=0.0,
        volatility_weight=0.0, upside_lean_weight=0.0, matchup_variance_weight=0.0,
        usage_weight=0.0, momentum_weight=0.0, divergence_weight=0.0, kalshi_weight=0.0,
        venue_disruption_weight=0.0,
        streaming_weight=0.65, waiver_value_mode="marginal",
        blocking_hold_bonus=1.5, denial_weight=0.15, denial_opponent_boost=0.15,
        denial_seed_window=2, denial_priority_floor=3, priority_value=0.3,
    ),
    3: dict(  # Sharp -- every evidence-backed outside feature; the one cell ever validated out-of-sample.
        weather_weight=0.25, vegas_weight=0.20,
        volatility_weight=0.05, upside_lean_weight=0.05, matchup_variance_weight=0.0,
        usage_weight=0.15, momentum_weight=0.15, divergence_weight=0.05, kalshi_weight=0.0,
        venue_disruption_weight=0.0,
        streaming_weight=0.80, waiver_value_mode="marginal",
        blocking_hold_bonus=1.5, denial_weight=0.15, denial_opponent_boost=0.15,
        denial_seed_window=2, denial_priority_floor=3, priority_value=0.3,
    ),
    4: dict(  # Use at your own risk -- every feature, including untested; excludes only confirmed-harmful.
        weather_weight=0.38, vegas_weight=0.32,
        volatility_weight=0.45, upside_lean_weight=0.45, matchup_variance_weight=0.60,
        usage_weight=0.20, momentum_weight=0.20, divergence_weight=0.10, kalshi_weight=0.15,
        venue_disruption_weight=0.10,
        streaming_weight=0.95, waiver_value_mode="marginal",
        blocking_hold_bonus=1.5, denial_weight=0.15, denial_opponent_boost=0.15,
        denial_seed_window=2, denial_priority_floor=3, priority_value=0.3,
    ),
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
    that valuation code needs (playoff shape, standings). Waivers are always
    rolling-priority — see `SeasonConfig.priority_value` — there is no
    FAAB path (removed; this repo's own league never used it).

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
        than failing. An empty/falsy `path` is the same no-op, checked
        before `Path(path)` since `Path("")` resolves to the cwd (`.`),
        which `.exists()` reports True for -- see `intel.load_intel`'s
        identical guard for the bug this avoids."""
        if not path:
            return None
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
        waiver_type = raw.get("waiver_type")
        if waiver_type is not None and str(waiver_type) != "rolling":
            warnings.warn(
                f"league.yml: 'waiver_type: {waiver_type}' is no longer read — "
                "FAAB support has been removed and every league is treated as "
                "rolling-priority (see SeasonConfig.priority_value). Remove "
                "this key.",
                DeprecationWarning,
                stacklevel=3,
            )
        return cls(
            name=str(raw.get("name", "")),
            source=str(raw.get("source", "")),
            games_per_season=int(raw.get("games_per_season", 17)),
            regular_season_weeks=int(raw.get("regular_season_weeks", 14)),
            playoff_teams=int(raw.get("playoff_teams", 0)),
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
class NotifyConfig:
    """Outbound push for `scripts/autorun.py`'s unattended runs — a fired
    trigger (Tuesday pre-waiver, 2h-pre-kickoff) that produces an actionable
    recommendation (a real lineup move, or a waiver candidate worth an
    actual `CLAIM`) sends a push notification, since the whole point of an
    unattended run is that nobody is watching the terminal when it fires.

    `"off"` (the default) is an exact no-op — `ffbot.notify.send` never even
    builds a request. `"ntfy"` posts to a free, no-signup push topic
    (https://ntfy.sh — install the app, pick a private random topic name,
    subscribe to it there, then set `ntfy_topic` in config.local.yml; a
    public server means the topic name IS the secret, so never commit a
    real one to config.yml). `"toast"` fires a local Windows notification
    (PowerShell) instead — no phone, no external service, but only seen if
    you're at the machine when it fires. `"both"` fans out to each.

    A delivery failure on either channel degrades to a printed stderr
    warning, never a crash — same contract as every other live seam in this
    repo, just outbound instead of inbound (see `ffbot/notify.py`).
    """

    channel: str = "off"  # "off" | "ntfy" | "toast" | "both"
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""

    # A CLAIM-worthy waiver candidate only triggers a notification when its
    # `net` (season points, same number `waivers.candidates[].net` already
    # ranks by) is at least this — a HOLD-priority-only week (every
    # candidate's claim cost exceeds its value) should stay quiet, not buzz
    # your phone for something not worth spending priority on.
    min_waiver_net: float = 0.0


@dataclass
class Config:
    # Sleeper league identity/cache behavior — see `SleeperConfig`. Replaces
    # the old top-level `league_id`/`team_key` (Yahoo concepts requiring
    # OAuth to resolve); `Config.from_dict` still honors those two keys as a
    # deprecated fallback into `sleeper.league_id` for one migration window.
    sleeper: SleeperConfig = field(default_factory=SleeperConfig)

    # When true, every action is computed and logged but never sent live.
    # Sleeper's public API has no write capability at all (see
    # docs/INSEASON.md) — this stays meaningful only for a hypothetical
    # future write path, and defaults on since none exists today.
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

    # WHERE weekly projection numbers come from — see `ProjectionSourceConfig`.
    # Named distinctly from `projection` above (not `projections`, its
    # obvious-but-dangerous plural) precisely because the two are easy to
    # confuse: one is HOW a missing projection gets estimated, the other is
    # WHERE the real numbers come from in the first place.
    projection_source: ProjectionSourceConfig = field(default_factory=ProjectionSourceConfig)

    # WHERE the roster's NAMES come from — see `RosterSourceConfig`.
    roster_source: RosterSourceConfig = field(default_factory=RosterSourceConfig)

    # WHERE league.yml's standings block comes from — see `StandingsSourceConfig`.
    standings_source: StandingsSourceConfig = field(default_factory=StandingsSourceConfig)

    # WHERE the other 11 teams' rosters come from — see `LeagueRostersSourceConfig`.
    league_rosters_source: LeagueRostersSourceConfig = field(default_factory=LeagueRostersSourceConfig)

    # Auto-fetched weather/odds, merged under weekly/week-NN.yml — see
    # `GameConditionsConfig`.
    game_conditions: GameConditionsConfig = field(default_factory=GameConditionsConfig)

    drops: DropPolicyConfig = field(default_factory=DropPolicyConfig)
    draft: DraftConfig = field(default_factory=DraftConfig)
    season: SeasonConfig = field(default_factory=SeasonConfig)

    # Outbound notifications for scripts/autorun.py's unattended runs — see
    # `NotifyConfig`.
    notify: NotifyConfig = field(default_factory=NotifyConfig)

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

        sleeper_raw = dict(raw.get("sleeper") or {})
        if "league_id" not in sleeper_raw and raw.get("league_id"):
            warnings.warn(
                "config.yml: top-level 'league_id' has moved under "
                "'sleeper: {league_id: ...}' as part of the Yahoo->Sleeper "
                "migration — update config.yml (old key still honored for now)",
                DeprecationWarning,
                stacklevel=3,
            )
            sleeper_raw["league_id"] = raw["league_id"]
        if raw.get("team_key"):
            warnings.warn(
                "config.yml: top-level 'team_key' is a Yahoo concept and no "
                "longer used — Sleeper identifies your team by 'sleeper: "
                "{roster_id: ...}' (auto-resolved from 'username' if unset)",
                DeprecationWarning,
                stacklevel=3,
            )

        return cls(
            sleeper=_construct(SleeperConfig, "config.yml [sleeper]", sleeper_raw),
            dry_run=bool(raw.get("dry_run", True)),
            lock_window_minutes=int(raw.get("lock_window_minutes", 45)),
            roster_positions=roster_positions,
            projection=_construct(ProjectionConfig, "config.yml [projection]", raw.get("projection") or {}),
            projection_source=_construct(ProjectionSourceConfig, "config.yml [projection_source]", raw.get("projection_source") or {}),
            roster_source=_construct(RosterSourceConfig, "config.yml [roster_source]", raw.get("roster_source") or {}),
            standings_source=_construct(StandingsSourceConfig, "config.yml [standings_source]", raw.get("standings_source") or {}),
            league_rosters_source=_construct(LeagueRostersSourceConfig, "config.yml [league_rosters_source]", raw.get("league_rosters_source") or {}),
            game_conditions=_construct(GameConditionsConfig, "config.yml [game_conditions]", raw.get("game_conditions") or {}),
            drops=_construct(DropPolicyConfig, "config.yml [drops]", raw.get("drops") or {}),
            draft=_draft_from_dict(raw.get("draft") or {}),
            season=_season_from_dict(raw.get("season") or {}),
            league_file=league_file,
            league=league,
            notify=_construct(NotifyConfig, "config.yml [notify]", raw.get("notify") or {}),
        )
