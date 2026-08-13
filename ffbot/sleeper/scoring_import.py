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
    "pass_completion_40plus": ("bonuses", "pass_completion_40plus"),
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

# Field-goal distance bands, Sleeper key prefix -> (min, max) yardage.
_FG_MAKE_BANDS: list[tuple[str, float, float]] = [
    ("fgm_0_19", 0, 19),
    ("fgm_20_29", 20, 29),
    ("fgm_30_39", 30, 39),
    ("fgm_40_49", 40, 49),
    ("fgm_50p", 50, 99),
]
_FG_MISS_BANDS: list[tuple[str, float, float]] = [
    ("fgmiss_0_19", 0, 19),
    ("fgmiss_20_29", 20, 29),
    ("fgmiss_30_39", 30, 39),
    ("fgmiss_40_49", 40, 49),
    ("fgmiss_50p", 50, 99),
]

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
    "kicking": {
        "fg_distance_mix": {
            "0-19": 0.06, "20-29": 0.24, "30-39": 0.28, "40-49": 0.27, "50-99": 0.15,
        },
    },
    "defense": {
        "points_allowed_stdev": 9.5,
    },
}


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
    if fg_bands:
        out["kicking"]["fg_by_distance"] = fg_bands

    fg_miss_bands = [
        {"min": lo, "max": hi, "points": -abs(remaining.pop(key))}
        for key, lo, hi in _FG_MISS_BANDS if key in remaining
    ]
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
