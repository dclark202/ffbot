"""Translate Sleeper's flat `scoring_settings` dict (from `SleeperClient.league()`)
into `league.yml`'s nested shape (`LeagueScoring.from_dict`'s input) — the
one-time bootstrap this repo's setup previously required by hand (transcribe
every number off Sleeper's League Settings page, as `league.yml`'s own
docstring still says). Same boundary discipline as `ffbot/sleeper/models.py`:
Sleeper's key spellings are translated once, here, never carried downstream.

Sleeper scores points-per-yard (`pass_yd: 0.04`); this repo's `league.yml`
scores yards-per-point (`yards_per_point: 25`) — the reciprocal, taken by
`_yards_per_point`. Every other numeric field is a direct points-per-event
value on both sides and copies straight across.

Every key Sleeper reports gets a place in the output, whatever its value —
including `0`. A `0` is real information (Sleeper explicitly tracks the
category and this league scores it at zero), never treated as "absent" or
"use the default" the way a missing key would be. This matters twice over:
a `0` inside a step-function ladder (`fg_by_distance`, `points_allowed`) is a
load-bearing band boundary, not a gap to skip — dropping it would silently
let the NEXT band's threshold swallow that range. And a `0` on an ordinary
field (e.g. `forced_fumble: 0`) is exactly the kind of fact a hand-
transcribed `league.yml` is prone to silently drift from Sleeper's own
settings on; writing it explicitly, every time, is what makes Sleeper's live
`scoring_settings` the definitive, re-verifiable source rather than a
one-time reference a human copied from and then owns forever.

Coverage of `ffbot/scoring.py`'s MODELED fields is still necessarily
incomplete — Sleeper's scoring vocabulary is larger than what this repo's
optimizer can compute from a FantasyPros export (see `league.yml`'s own "not
modeled" section for the shape of that gap). Per this repo's existing
"never silently drop" convention (`ffbot/names.py`, `ffbot/intel.py`), every
key this function can't translate into a modeled field still lands
somewhere: `league_dict["sleeper_unmapped"]` (written into `league.yml`
itself, ignored by `LeagueScoring.from_dict` but preserved as documentation
of a real Sleeper rule with nowhere to attach yet — "could theoretically be
used" once this repo models it), and the same keys come back as the
`unmapped` return value so a caller can also print them.
"""

from __future__ import annotations

import warnings
from typing import Any, Mapping, Sequence

# Sleeper's scoring key -> this repo's [section][field] destination, for the
# straightforward one-key-to-one-field cases (a handful of keys need the
# yards-per-point reciprocal or list-building logic below and are handled
# separately, not through this table).
_DIRECT_MAP: dict[str, tuple[str, str]] = {
    "pass_td": ("passing", "td"),
    "pass_int": ("passing", "int"),
    "pass_2pt": ("passing", "two_pt"),
    "rush_td": ("rushing", "td"),
    "rush_2pt": ("rushing", "two_pt"),
    "rec": ("receiving", "reception"),
    "rec_td": ("receiving", "td"),
    "rec_2pt": ("receiving", "two_pt"),
    "fum_lost": ("misc", "fumble_lost"),
    "fum_rec_td": ("misc", "off_fumble_return_td"),
    # This spelling never occurs in a real Sleeper payload (the real key is
    # `pass_cmp_40p`, mapped below) -- kept only so an already-written
    # league.yml using it directly stays valid; not something this importer
    # itself will ever emit.
    "pass_completion_40plus": ("bonuses", "pass_completion_40plus"),
    "pass_cmp_40p": ("bonuses", "pass_completion_40plus"),
    # Per-PLAY 40+ yard bonuses (TD or not) -- distinct from the TD-distance
    # bonuses below; see BonusScoring's own docstring for the split.
    "rush_40p": ("bonuses", "rush_40plus"),
    "rec_40p": ("bonuses", "rec_40plus"),
    # TD-distance bonuses -- declared so `scoring.unmodeled_rules` can warn
    # about them by name; no export (live or CSV) carries which TD was long.
    "pass_td_40p": ("bonuses", "pass_td_40plus"),
    "pass_td_50p": ("bonuses", "pass_td_50plus"),
    "rush_td_50p": ("bonuses", "rush_td_50plus"),
    "rec_td_50p": ("bonuses", "rec_td_50plus"),
    # No live-projected stat backs this at all (verified: Sleeper's weekly
    # DEF feed carries no three-and-out field) -- mapped anyway so the real
    # value lands in the modeled field for `scoring.unmodeled_rules` (which
    # already warns on `defense.three_and_outs`) instead of sitting silently
    # in `sleeper_unmapped`.
    "def_3_and_out": ("defense", "three_and_outs"),
    "xpm": ("kicking", "pat_made"),
    "xpmiss": ("kicking", "pat_missed"),
    "sack": ("defense", "sack"),
    "int": ("defense", "interception"),  # defensive INT; NOT pass_int (thrown)
    "fum_rec": ("defense", "fumble_recovery"),
    "ff": ("defense", "forced_fumble"),
    "safe": ("defense", "safety"),
    "blk_kick": ("defense", "block_kick"),
    "def_st_td": ("defense", "touchdown"),
    "def_td": ("defense", "touchdown"),
    # A kick/punt-return TD -- a DIFFERENT rule from def_st_td/def_td above,
    # not a third spelling of the same one: this league's own live settings
    # set def_td and st_td to distinct point values, so they're scored as
    # two separate, additive categories (see DefenseScoring.special_teams_td).
    "st_td": ("defense", "special_teams_td"),
}

# Sleeper's yards keys pair with this repo's yards_per_point reciprocal.
_YARDS_MAP: dict[str, tuple[str, str]] = {
    "pass_yd": ("passing", "yards_per_point"),
    "rush_yd": ("rushing", "yards_per_point"),
    "rec_yd": ("receiving", "yards_per_point"),
}

# 40+-yard TD bonus keys, keyed to league.yml's bonuses.* fields — Sleeper's
# `rush_td_40p`/`rec_td_40p` ("+N over the base TD value") map directly onto
# this repo's own "+N over base" convention for the same bonus.
_BIG_PLAY_BONUS_MAP: dict[str, tuple[str, str]] = {
    "rush_td_40p": ("bonuses", "rush_td_40plus"),
    "rec_td_40p": ("bonuses", "rec_td_40plus"),
}

# Field-goal distance bands, Sleeper key prefix -> (min, max) yardage. Two
# vocabularies exist for the 50+ range: some leagues use the finer
# fgm_50_59/fgm_60p split, others the older flat fgm_50p bucket -- see
# _FG_MAKE_FALLBACK_50PLUS below for how the two are reconciled when a
# league's raw scoring_settings somehow carries both.
_FG_MAKE_BANDS: list[tuple[str, float, float]] = [
    ("fgm_0_19", 0, 19),
    ("fgm_20_29", 20, 29),
    ("fgm_30_39", 30, 39),
    ("fgm_40_49", 40, 49),
    ("fgm_50_59", 50, 59),
    ("fgm_60p", 60, 99),
]
_FG_MAKE_FALLBACK_50PLUS: tuple[str, float, float] = ("fgm_50p", 50, 99)

_FG_MISS_BANDS: list[tuple[str, float, float]] = [
    ("fgmiss_0_19", 0, 19),
    ("fgmiss_20_29", 20, 29),
    ("fgmiss_30_39", 30, 39),
    ("fgmiss_40_49", 40, 49),
    ("fgmiss_50_59", 50, 59),
    ("fgmiss_60p", 60, 99),
]
_FG_MISS_FALLBACK_50PLUS: tuple[str, float, float] = ("fgmiss_50p", 50, 99)

# Points-allowed ladder. Sleeper's bands are half-open like league.yml's own
# Tier list (`points <= max`); the top band's max is left effectively
# unbounded, matching `league.example.yml`'s own `{max: 999, ...}` idiom.
_POINTS_ALLOWED_BANDS: list[tuple[str, float]] = [
    ("pts_allow_0", 0),
    ("pts_allow_1_6", 6),
    ("pts_allow_7_13", 13),
    ("pts_allow_14_20", 20),
    ("pts_allow_21_27", 27),
    ("pts_allow_28_34", 34),
    ("pts_allow_35p", 999),
]

# Estimates `league.example.yml` already ships as defaults for facts Sleeper
# doesn't expose at all (a league-wide FG-distance mix, the points-allowed
# per-game spread used to integrate that ladder). Not scoring RULES — just
# the same starting values this repo has always shipped, carried forward
# with a comment flagging them as unverified for a specific league.
_UNEXPOSED_DEFAULTS = {
    "defense": {
        "points_allowed_stdev": 9.5,
    },
}

# The coarse mix's "50-99" share (0.15), split across the finer 50-59/60+
# bands roughly in proportion to attempt frequency at each distance -- a
# 60+ yard attempt is far rarer than a 50-59 one. Same "unverified estimate,
# carried forward" status as the coarse figure itself.
_FG_DISTANCE_MIX_COARSE = {"0-19": 0.06, "20-29": 0.24, "30-39": 0.28, "40-49": 0.27, "50-99": 0.15}
_FG_DISTANCE_MIX_FINE = {"0-19": 0.06, "20-29": 0.24, "30-39": 0.28, "40-49": 0.27, "50-59": 0.13, "60-99": 0.02}


def _fg_distance_mix_for(fg_bands: list[dict]) -> dict[str, float]:
    """`fg_distance_mix` default aligned to whichever `fg_by_distance` shape
    `fg_bands` actually has -- `ffbot.scoring._fg_value_per_kick` looks the
    mix up by exact `"min-max"` key, so a coarse "50-99" mix key is silently
    unused (falls back to a flat per-band average) against a league that
    built the finer 50-59/60+ split, and vice versa."""
    keys = {f"{int(b['min'])}-{int(b['max'])}" for b in fg_bands}
    if "50-59" in keys or "60-99" in keys:
        return dict(_FG_DISTANCE_MIX_FINE)
    return dict(_FG_DISTANCE_MIX_COARSE)


def _merge_fg_fallback_50plus(
    bands: list[dict],
    remaining: dict,
    fallback: tuple[str, float, float],
    finer_keys_label: str,
    *,
    negate: bool = False,
) -> None:
    """Reconcile a distance-banded FG ladder's finer 50-59/60+ split against
    the older flat "everything 50+" fallback key (`fgm_50p`/`fgmiss_50p`) --
    see `_FG_MAKE_FALLBACK_50PLUS`. If a league's raw `scoring_settings`
    somehow carries both (never observed live, but not something to
    silently mis-price either way), the finer split wins and the fallback
    key is dropped with a warning rather than double-scored."""
    key, lo, hi = fallback
    if key not in remaining:
        return
    has_finer_50plus = any(b["min"] >= 50 for b in bands)
    if has_finer_50plus:
        warnings.warn(
            f"league scoring: both a finer 50+ FG band split ({finer_keys_label}) and "
            f"the flat '{key}' are present -- the finer split wins, '{key}' is dropped "
            "as redundant (not scored twice).",
            stacklevel=3,
        )
        remaining.pop(key)
        return
    value = remaining.pop(key)
    bands.append({"min": lo, "max": hi, "points": -abs(value) if negate else value})


def _yards_per_point(points_per_yard: Any, default: float) -> float:
    """Sleeper stores points-per-yard (e.g. `0.04`); `league.yml` stores the
    reciprocal (`25`). `0` has no clean reciprocal (dividing by it), so this
    one field alone still falls back to the conventional default when
    Sleeper's rate is exactly zero — the raw `0` itself is still preserved
    for a reader by being present in the source Sleeper payload, just not
    representable as a yards-per-point number."""
    try:
        value = float(points_per_yard)
    except (TypeError, ValueError):
        return default
    return round(1.0 / value, 4) if value else default


def league_dict_from_sleeper_scoring(
    scoring: Mapping[str, Any],
    *,
    name: str = "",
    source: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """`(league_dict, unmapped_keys)` — `league_dict` is ready for
    `LeagueScoring.from_dict` (or a direct `yaml.safe_dump` into
    `league.yml`). Every key in `scoring` lands somewhere: a recognized key
    is written into its modeled field (even at `0` — see this module's
    docstring), and everything else lands in `league_dict["sleeper_unmapped"]`
    as a preserved, human-readable placeholder — nothing is silently
    dropped, whatever its value. `unmapped_keys` is the same set, as a
    sorted list, so a caller can also print it directly.

    `name`/`source` are provenance strings only (`league.yml`'s `name:`/
    `source:` fields) — this function makes no live calls itself, callers
    supply them from whatever fetched `scoring` in the first place.
    """
    remaining = dict(scoring)  # every key stays until it's placed somewhere
    # below — including 0-valued ones. A `0` is real information (Sleeper
    # tracks the category; this league scores it at zero), never treated as
    # "absent."

    out: dict[str, dict[str, Any]] = {
        "passing": {}, "rushing": {}, "receiving": {}, "misc": {},
        "bonuses": {}, "kicking": {}, "defense": {},
    }

    fg_bands = [
        {"min": lo, "max": hi, "points": remaining.pop(key)}
        for key, lo, hi in _FG_MAKE_BANDS if key in remaining
    ]
    _merge_fg_fallback_50plus(fg_bands, remaining, _FG_MAKE_FALLBACK_50PLUS, "fgm_50_59/fgm_60p")
    if fg_bands:
        out["kicking"]["fg_by_distance"] = fg_bands
    # Unconditional, like every other _UNEXPOSED_DEFAULTS entry -- present
    # even with no fg_by_distance at all, so it's ready the moment one is
    # hand-added later. Aligned to whichever band shape (coarse vs. fine
    # 50+) fg_bands actually has.
    out["kicking"].setdefault("fg_distance_mix", _fg_distance_mix_for(fg_bands))

    fg_miss_bands = [
        {"min": lo, "max": hi, "points": -abs(remaining.pop(key))}
        for key, lo, hi in _FG_MISS_BANDS if key in remaining
    ]
    _merge_fg_fallback_50plus(
        fg_miss_bands, remaining, _FG_MISS_FALLBACK_50PLUS, "fgmiss_50_59/fgmiss_60p", negate=True,
    )
    if fg_miss_bands:
        out["kicking"]["fg_missed_by_distance"] = fg_miss_bands

    pts_allowed = [
        {"max": hi, "points": remaining.pop(key)}
        for key, hi in _POINTS_ALLOWED_BANDS if key in remaining
    ]
    if pts_allowed:
        out["defense"]["points_allowed"] = pts_allowed

    for key, (section, field_) in _DIRECT_MAP.items():
        if key in remaining:
            out[section][field_] = remaining.pop(key)

    for key, (section, field_) in _YARDS_MAP.items():
        if key in remaining:
            out[section][field_] = _yards_per_point(remaining.pop(key), default=25.0 if section == "passing" else 10.0)

    for key, (section, field_) in _BIG_PLAY_BONUS_MAP.items():
        if key in remaining:
            out[section][field_] = remaining.pop(key)

    if not fg_miss_bands and "fgmiss" in remaining:
        out["kicking"]["fg_missed"] = -abs(remaining.pop("fgmiss"))

    # TE-premium reception bonus: Sleeper scores it as an ADD-ON over the
    # base `rec` value; league.yml's reception_by_position wants the ABSOLUTE
    # per-catch value for that position.
    if "bonus_rec_te" in remaining:
        base_reception = out["receiving"].get("reception", 1.0)
        out["receiving"]["reception_by_position"] = {"TE": base_reception + remaining.pop("bonus_rec_te")}

    for section, fields in _UNEXPOSED_DEFAULTS.items():
        for field_, value in fields.items():
            out[section].setdefault(field_, value)

    league_dict: dict[str, Any] = {"name": name, "source": source}
    league_dict.update({k: v for k, v in out.items() if v})

    unmapped = dict(sorted(remaining.items()))
    if unmapped:
        league_dict["sleeper_unmapped"] = unmapped
    return league_dict, sorted(unmapped)


# Carried-forward estimates (see _UNEXPOSED_DEFAULTS / _fg_distance_mix_for)
# -- never derived from live scoring_settings, so comparing them in
# `scoring_drift` would only ever compare this importer's own estimate
# against itself and could never signal a real commissioner change.
_DRIFT_IGNORED_FIELDS: dict[str, set[str]] = {
    "kicking": {"fg_distance_mix"},
    "defense": {"points_allowed_stdev"},
}


def _normalize_scoring_value(value: Any) -> Any:
    """Order-independent, hashable form of a modeled-field value, for
    equality comparison in `scoring_drift` — band ladders (lists of dicts)
    would otherwise report spurious drift on list ordering alone."""
    if isinstance(value, list):
        normalized = [
            tuple(sorted(item.items())) if isinstance(item, dict) else item
            for item in value
        ]
        return tuple(sorted(normalized, key=repr))
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def _flatten_scoring_sections(d: Mapping[str, Any]) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for section in ("passing", "rushing", "receiving", "misc", "bonuses", "kicking", "defense"):
        block = d.get(section) or {}
        if not isinstance(block, dict):
            continue
        ignored = _DRIFT_IGNORED_FIELDS.get(section, set())
        for field_, value in block.items():
            if field_ in ignored:
                continue
            out[(section, field_)] = _normalize_scoring_value(value)
    return out


def scoring_drift(league_yaml_raw: Mapping[str, Any], live_scoring: Mapping[str, Any]) -> list[str]:
    """Human-readable drift lines between the checked-in `league.yml`
    (`league_yaml_raw` — its raw parsed YAML, NOT a `LeagueScoring`
    instance) and what Sleeper's live `scoring_settings` say right now,
    regenerated fresh through `league_dict_from_sleeper_scoring`.

    Two kinds of drift reported: (1) a MODELED field whose value differs —
    almost always a real commissioner mid-season rule change, since these
    are otherwise fixed for a season; (2) a scoring key Sleeper reports live
    that `league_yaml_raw` doesn't already carry in its own
    `sleeper_unmapped` block — either a brand-new Sleeper scoring category,
    or a rule this importer has since learned to map but the checked-in
    file predates.

    Carried-forward estimates with no live source at all (`fg_distance_mix`,
    `points_allowed_stdev` — see `_UNEXPOSED_DEFAULTS`) are excluded
    entirely — see `_DRIFT_IGNORED_FIELDS`.

    Returns `[]` when nothing has drifted — the common case, meant to be
    checked with `if lines:` before surfacing anything. Never fetches
    anything itself; `live_scoring` is the caller's own already-fetched
    `client.league(...)["scoring_settings"]`, same "every live call is the
    caller's responsibility, this module just translates" contract
    `league_dict_from_sleeper_scoring` already has.
    """
    live_dict, live_unmapped = league_dict_from_sleeper_scoring(live_scoring)

    saved = _flatten_scoring_sections(league_yaml_raw)
    live = _flatten_scoring_sections(live_dict)

    lines: list[str] = []
    for key in sorted(set(saved) | set(live)):
        section, field_ = key
        old = saved.get(key)
        new = live.get(key)
        if old == new:
            continue
        if key not in saved:
            lines.append(f"{section}.{field_}: not in league.yml — Sleeper now scores it at {new!r}")
        elif key not in live:
            lines.append(f"{section}.{field_}: league.yml has {old!r} — Sleeper's live settings no longer report it")
        else:
            lines.append(f"{section}.{field_}: league.yml says {old!r} — Sleeper's live settings now say {new!r}")

    saved_unmapped = set((league_yaml_raw.get("sleeper_unmapped") or {}).keys())
    new_unmapped = sorted(set(live_unmapped) - saved_unmapped)
    if new_unmapped:
        lines.append(
            "Sleeper scoring key(s) not seen when league.yml was last generated: "
            + ", ".join(new_unmapped) + " — re-run scripts/init_league.py --force to review"
        )
    return lines


def roster_positions_from_sleeper(
    positions: Sequence[str], reserve_slots: int = 0
) -> dict[str, int]:
    """Sleeper's `roster_positions` is a flat list with one entry per slot
    (`["QB","RB","RB","WR","WR","FLEX","BN","BN",...]`); `config.yml`'s
    `roster_positions` is the counted-dict shape the optimizer actually
    reads. Slot NAMES pass through unchanged — Sleeper's own flex spellings
    (`FLEX`/`SUPER_FLEX`/...) are already registered in
    `ffbot.models.SLOT_ELIGIBILITY`, so no translation is needed here, only
    counting (see this package's module docstring on that point).

    `reserve_slots` is Sleeper's separate `settings.reserve_slots` (IR count)
    — not present in the `roster_positions` list itself, so it's a distinct
    parameter rather than another list entry to count.
    """
    counts: dict[str, int] = {}
    for slot in positions:
        counts[slot] = counts.get(slot, 0) + 1
    if reserve_slots:
        counts["IR"] = reserve_slots
    return counts
