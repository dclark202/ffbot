"""The in-season weekly manager — the runtime analog of `ffbot.edge`.

Where `edge.py` answers "who should I draft," this answers "who should I
start, add, or drop this week." Same governing rule as the draft's intel
layer, because it is the right rule twice: **what is verifiable moves the
number, what is speculative stays a note.** An official status designation,
a confirmed forecast — these multiply into the projection. A beat writer's
read on a game plan is a note in the WHY column, never a silent thumb on the
scale.

Everything here is a pure function of already-loaded data — no network calls,
matching the pattern `board.py` and `draft.py` already use. Fetching weekly
projections, weather, Vegas lines, and researched notes is the job of
whatever populates a `weekly/week-NN.yml` file (today, the `/gameday` Claude
command; a live Yahoo pull is M3). This module never cares which one did it.

The weights below are, like `edge.py`, fractions of a *decision scale* rather
than absolute points — the gap between a team's best and roughly its
bench-boundary player that week — so a spice bonus calibrated for a
blowout-projection week cannot accidentally swamp a coin-flip week, or vice
versa. See `decision_scale`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

import yaml

from .board import Board, BoardPlayer, to_player
from .config import Config, SeasonConfig
from .league_rosters import LeagueRosters
from .lineup import LineupPlan, optimize
from .models import (
    BENCH,
    IR_SLOTS,
    STATUS_IR_ELIGIBLE,
    STATUS_OUT,
    Player,
    ir_slots,
    roster_capacity,
)
from .names import defense_key, normalize_name, search_scored

# Used when no games carry a real Vegas total to average — a plausible
# league-wide implied total, not a load-bearing constant. Whenever any real
# totals are present in the loaded week, the actual mean is used instead.
_DEFAULT_LEAGUE_AVG_TOTAL = 22.0

_MIN_DECISION_SCALE = 1.0
_DECISION_BENCH_DEPTH = 6  # how far into the roster the "gap" is measured


# --- Stadiums -----------------------------------------------------------


@dataclass(frozen=True)
class StadiumInfo:
    dome: bool
    lat: float | None = None
    lon: float | None = None


def load_stadiums(path: str | Path = "data/stadiums.yml") -> dict[str, StadiumInfo]:
    """Team abbreviation -> roof/location info. Missing file = empty map,
    which makes every game "unknown roof" and therefore weather-neutral
    (see `is_dome_game`) rather than a crash.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, StadiumInfo] = {}
    for team, info in raw.items():
        if not isinstance(info, dict):
            continue
        out[str(team).upper()] = StadiumInfo(
            dome=bool(info.get("dome", False)),
            lat=info.get("lat"),
            lon=info.get("lon"),
        )
    return out


# --- Weekly intel schema -------------------------------------------------


@dataclass(frozen=True)
class WeeklyPlayerIntel:
    """One player's researched weekly picture."""

    name: str  # as written in the file, for error messages
    status: str = ""  # Yahoo-style code override; "" = no override, use the board's
    note: str = ""  # plain-English "why", shown in the brief
    risk: float | None = None  # 0-100 availability risk, same contract as draft intel
    upside: float | None = None  # 0-100 spike-week potential this week specifically
    volatility: float | None = None  # 0-100 explicit boom/bust rating
    usage_trend: float | None = None  # 0-100 recent target/air-yards share trend (see usage_score)
    momentum: float | None = None  # 0-100 recent SCORING trend vs. season avg (see momentum_score)
    divergence: float | None = None  # 0-100 role trend outrunning production, or vice versa (see divergence_score)
    # 0-100 market-vs-projection divergence rank, within position (see
    # kalshi_score) -- how much more (or less) bullish Kalshi's public NFL
    # prop markets are on this player than the shipped projection already
    # is. Populated live by ffbot.live's spice-level-4-only Kalshi wiring,
    # or hand-writable in weekly/week-NN.yml like every other 0-100 field.
    kalshi: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameInfo:
    """One team's game this week, as researched — never assumed from a
    typical weekday slot. See docs/INSEASON.md: Saturday games and
    international early kickoffs make a fixed schedule wrong often enough to
    matter.
    """

    opponent: str
    kickoff_et: str = ""  # ISO datetime string, e.g. "2026-09-14T13:00"
    home: bool = True
    wind_mph: float | None = None
    precip_pct: float | None = None
    team_total: float | None = None  # this team's Vegas implied total
    opp_total: float | None = None  # opponent's Vegas implied total

    # Richer weather, from `ffbot.history.openmeteo` (historical replay) or a
    # future live researched source -- deliberately separate from
    # `wind_mph`/`precip_pct` above rather than replacing them, since those
    # two are what a LIVE `weekly/week-NN.yml` forecast can carry and these
    # three are only ever populated from an actual observation. `precip_mm`
    # is real accumulated precipitation, unlike `precip_pct`'s probability.
    temp_f: float | None = None
    wind_gust_mph: float | None = None
    precip_mm: float | None = None

    # A neutral-site/international venue key into `data/stadiums.yml` (e.g.
    # "LONDON_TOT"), overriding the usual home/away-derived stadium lookup.
    # "" (the default) means "derive from home/opponent," same as before
    # this field existed.
    venue: str = ""

    # Playing outside a typical NFL setting (international sites, unfamiliar
    # time zones/travel) — a real but genuinely weak signal, distinct from
    # the venue's roof. See `SeasonConfig.venue_disruption_weight`.
    international: bool = False


class WeeklyIntelError(ValueError):
    """The weekly intel file exists but could not be understood."""


@dataclass
class WeeklyIntel:
    week: int | None = None
    generated: str = ""
    source_notes: str = ""
    players: dict[str, WeeklyPlayerIntel] = field(default_factory=dict)  # normalized name -> entry
    games: dict[str, GameInfo] = field(default_factory=dict)  # team abbr -> this week's game


def _score_field(name: str, entry_label: str, raw: dict) -> float | None:
    val = raw.get(name)
    if val is None:
        return None
    try:
        val = float(val)
    except (TypeError, ValueError):
        raise WeeklyIntelError(f"{entry_label!r}: {name} must be a number, got {val!r}") from None
    if not 0.0 <= val <= 100.0:
        raise WeeklyIntelError(f"{entry_label!r}: {name} must be 0-100, got {val}")
    return val


def _parse_player_entry(name: str, raw) -> WeeklyPlayerIntel:
    if raw is None:
        return WeeklyPlayerIntel(name=name)
    if isinstance(raw, str):
        return WeeklyPlayerIntel(name=name, note=raw.strip())
    if not isinstance(raw, dict):
        raise WeeklyIntelError(f"{name!r}: expected a mapping or a string, got {type(raw).__name__}")

    flags = raw.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    if not isinstance(flags, list):
        raise WeeklyIntelError(f"{name!r}: flags must be a list, got {type(flags).__name__}")

    note = " ".join(str(raw.get("note") or "").split())
    status = str(raw.get("status") or "").strip().upper()

    return WeeklyPlayerIntel(
        name=name,
        status=status,
        note=note,
        risk=_score_field("risk", name, raw),
        upside=_score_field("upside", name, raw),
        volatility=_score_field("volatility", name, raw),
        usage_trend=_score_field("usage_trend", name, raw),
        momentum=_score_field("momentum", name, raw),
        divergence=_score_field("divergence", name, raw),
        kalshi=_score_field("kalshi", name, raw),
        flags=tuple(str(f) for f in flags),
    )


def _parse_game_entry(team: str, raw: dict) -> GameInfo:
    if not isinstance(raw, dict) or "opponent" not in raw:
        raise WeeklyIntelError(f"games.{team!r}: needs at least an 'opponent'")
    return GameInfo(
        opponent=str(raw["opponent"]).upper(),
        kickoff_et=str(raw.get("kickoff_et") or ""),
        home=bool(raw.get("home", True)),
        wind_mph=raw.get("wind_mph"),
        precip_pct=raw.get("precip_pct"),
        team_total=raw.get("team_total"),
        opp_total=raw.get("opp_total"),
        venue=str(raw.get("venue") or "").strip().upper(),
        international=bool(raw.get("international", False)),
    )


def load_weekly_intel(path: str | Path) -> WeeklyIntel:
    """Parse a `weekly/week-NN.yml` file. A missing file returns an empty,
    inert `WeeklyIntel` — every adjustment in this module treats "no entry"
    as "no change," so a week with no research still runs on projections and
    status alone rather than failing.
    """
    p = Path(path)
    if not p.exists():
        return WeeklyIntel()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise WeeklyIntelError(f"{p}: expected a mapping at the top level")

    players_raw = raw.get("players") or {}
    if not isinstance(players_raw, dict):
        raise WeeklyIntelError(f"{p}: 'players' must be a mapping of name -> intel")
    players = {
        normalize_name(str(name)): _parse_player_entry(str(name), body)
        for name, body in players_raw.items()
    }

    games_raw = raw.get("games") or {}
    if not isinstance(games_raw, dict):
        raise WeeklyIntelError(f"{p}: 'games' must be a mapping of team -> game info")
    games = {str(team).upper(): _parse_game_entry(str(team), body) for team, body in games_raw.items()}

    return WeeklyIntel(
        week=raw.get("week"),
        generated=str(raw.get("generated") or ""),
        source_notes=str(raw.get("source_notes") or ""),
        players=players,
        games=games,
    )


def unmatched_player_warnings(players: Sequence[Player], weekly: WeeklyIntel) -> list[str]:
    """Weekly intel entries that matched nobody on the roster.

    A silent miss here is the dangerous case: research about "your" player
    that never actually reaches the optimizer because the name didn't quite
    match reads, on the surface, exactly like a week with no news at all.
    """
    rostered = {normalize_name(p.name) for p in players}
    out = []
    for key, entry in weekly.players.items():
        if key in rostered:
            continue
        hits = search_scored(entry.name, players)
        hint = f" — did you mean {hits[0][1].name!r}?" if hits else ""
        out.append(f"{entry.name!r} in weekly intel matched nobody on the roster{hint}")
    return out


# --- Status ---------------------------------------------------------------


def apply_status_overrides(players: Sequence[Player], weekly: WeeklyIntel) -> list[Player]:
    """Weekly intel's `status` wins over whatever the roster source supplied.

    This is the manual-route equivalent of Yahoo's live status field: without
    API access, a designation only reaches the optimizer if research put it
    in `weekly/week-NN.yml`. A blank status in the entry means "no override,"
    not "healthy" — it leaves whatever the player already had.
    """
    out = []
    for p in players:
        entry = weekly.players.get(normalize_name(p.name))
        if entry and entry.status:
            out.append(replace(p, status=entry.status))
        else:
            out.append(p)
    return out


# --- Weather ----------------------------------------------------------------


def is_dome_game(team: str, game: GameInfo | None, stadiums: dict[str, StadiumInfo]) -> bool:
    """Whether the STADIUM for this team's game this week is enclosed.

    `game.venue` wins when set — a neutral-site/international game (see
    `data/stadiums.yml`'s dedicated venue rows) is played nowhere near either
    team's home stadium, so deriving the lookup from home/opponent would
    check the wrong building entirely. Absent that override, depends on who
    is hosting, not on the player's own team — an away game at a dome is
    weather-neutral even for a team whose home stadium is outdoors. Unknown
    roof (missing stadium data) is treated as neutral rather than penalized,
    since a missing lookup is a data gap, not evidence of bad weather.
    """
    if game is None:
        return True
    venue_team = game.venue if game.venue else (team if game.home else game.opponent)
    info = stadiums.get(venue_team)
    return info.dome if info else True


def weather_severity(game: GameInfo | None, cfg: SeasonConfig) -> float:
    """0..1 how bad the forecast is, independent of position.

    Below both thresholds this is exactly 0.0, not a small positive number —
    a 6mph breeze is not weather, and treating it as a tiny discount would
    make every single player quietly underrated for no real reason.

    `wind_mph`/`precip_pct` of `None` (never researched, live) and of `0.0`
    (researched and calm) are deliberately collapsed to the same 0.0
    severity — same fail-open convention as `is_dome_game`'s "a missing
    lookup is a data gap, not evidence of bad weather." This is a real
    caveat for backtest measurement, though, not just live decisions: a
    good chunk of `games.csv`'s outdoor games have a blank `wind` column
    (missing data, not confirmed calm), and averaging those in as 0mph
    would understate the true calm-weather baseline `weather_severity` is
    calibrated against. Diagnostics reading historical wind (e.g.
    `scripts/backtest_weather.py`) must exclude unknown-wind rows from the
    calm bucket rather than folding them in via this same collapse.

    The wind ramp reaches full severity at THREE TIMES the threshold, not
    two — see `scripts/backtest_weather.py`'s diagnostic (docs/BACKTEST.md,
    milestone B4): realized-vs-projected point ratios at 15-20mph wind, the
    best-populated above-threshold bucket (n=152-468 across QB/RB/WR/TE,
    2021-2024), were only ~6-15% below the calm-weather baseline — the
    previous 2x-threshold ramp implied roughly double that discount at the
    same wind speed for any given `weather_weight`. Buckets above 20mph were
    too thin a sample (n<70, often <30) to calibrate a steeper ramp against,
    so this stays a deliberately gentle, conservative curve rather than
    extrapolating past what the data actually supports. Doesn't touch
    `wind_threshold_mph` itself (the "is this weather at all" cutoff) or
    `weather_weight == 0.0`'s exact-no-op contract.
    """
    if game is None:
        return 0.0
    wind = game.wind_mph or 0.0
    precip = game.precip_pct or 0.0

    wind_sev = 0.0
    if wind > cfg.wind_threshold_mph:
        span = 2.0 * cfg.wind_threshold_mph  # full severity at 3x threshold
        wind_sev = min(1.0, (wind - cfg.wind_threshold_mph) / span)

    precip_sev = 0.0
    if precip > cfg.precip_threshold_pct:
        span = max(1.0, 100.0 - cfg.precip_threshold_pct)
        precip_sev = min(1.0, (precip - cfg.precip_threshold_pct) / span)

    return max(wind_sev, precip_sev)


def weather_multiplier(
    position: str, team: str, game: GameInfo | None, cfg: SeasonConfig, stadiums: dict[str, StadiumInfo]
) -> float:
    """Weekly score multiplier from weather, in [1 - weather_weight, 1.0].

    Dome games and DEF are always exactly 1.0. RB takes `rb_weather_relief`
    of the discount everyone else takes, since bad weather typically shifts a
    game plan toward the run rather than suppressing offense outright.
    """
    if cfg.weather_weight == 0.0 or position == "DEF":
        return 1.0
    if is_dome_game(team, game, stadiums):
        return 1.0
    severity = weather_severity(game, cfg)
    if severity <= 0.0:
        return 1.0

    discount = cfg.weather_weight * severity
    if position == "RB":
        discount *= cfg.rb_weather_relief
    return max(0.0, 1.0 - discount)


def venue_disruption_multiplier(position: str, game: GameInfo | None, cfg: SeasonConfig) -> float:
    """Weekly score multiplier for playing outside a typical NFL setting, in
    [1 - venue_disruption_weight, 1.0]. Applies to both teams alike (an
    international game disrupts the home team's routine too, not just the
    traveling team) and, like weather, exempts DEF.
    """
    if cfg.venue_disruption_weight == 0.0 or position == "DEF":
        return 1.0
    if game is None or not game.international:
        return 1.0
    return max(0.0, 1.0 - cfg.venue_disruption_weight)


# --- Vegas ------------------------------------------------------------------


def league_avg_total(weekly: WeeklyIntel) -> float:
    """Mean implied team total across every game with a real number, so the
    tilt is calibrated to this week's actual slate rather than a guessed
    constant. Falls back to `_DEFAULT_LEAGUE_AVG_TOTAL` only when nothing at
    all was researched.
    """
    totals = [g.team_total for g in weekly.games.values() if g.team_total is not None]
    return statistics.fmean(totals) if totals else _DEFAULT_LEAGUE_AVG_TOTAL


def vegas_multiplier(position: str, team: str, weekly: WeeklyIntel, cfg: SeasonConfig) -> float:
    """Weekly score multiplier from the Vegas environment, centered on 1.0.

    Offensive positions scale with their OWN team's implied total — a
    high-scoring environment lifts the ceiling for everyone in it. DEF scales
    with the OPPONENT's implied total, inverted — a defense benefits when the
    team across from it is projected to score little, and its own offense's
    total is irrelevant to its own scoring. Floored at 0.5 so no environment,
    however extreme, can zero out or invert a player's value outright.
    """
    if cfg.vegas_weight == 0.0:
        return 1.0
    game = weekly.games.get(team)
    if game is None:
        return 1.0

    avg = league_avg_total(weekly)
    if avg <= 0:
        return 1.0

    if position == "DEF":
        total = game.opp_total
        if total is None:
            return 1.0
        delta = (avg - total) / avg
    else:
        total = game.team_total
        if total is None:
            return 1.0
        delta = (total - avg) / avg

    return max(0.5, 1.0 + cfg.vegas_weight * delta)


# Per-position lean on GAME SCRIPT (favored vs. underdog), independent of the
# implied-total tilt above -- two players with an identical team total have
# opposite outlooks at -10 vs. +10: the favorite runs clock (more RB volume,
# less pass volume), the underdog throws from behind (more QB/WR volume, less
# RB). +1.0 = fully favors being the favorite, -1.0 = fully favors being the
# underdog, 0.0 = no lean either way. K excluded (kicking volume doesn't
# track game script the way rushing/passing volume does).
GAME_SCRIPT_LEAN: dict[str, float] = {
    "RB": 1.0,
    "QB": -0.5,
    "WR": -1.0,
    "TE": -0.5,
    "DEF": 0.5,
    "K": 0.0,
}


def game_script_multiplier(position: str, team: str, game: GameInfo | None, cfg: SeasonConfig) -> float:
    """Weekly score multiplier from game script (the SPREAD), centered on 1.0.

    Needs no new data: `team_total`/`opp_total` are already computed
    everywhere `GameInfo` is (live and historical alike) — this is simply a
    dimension of the same numbers `vegas_multiplier` reads that nothing
    consumed before. `game_script_scale` is the point margin at which the
    lean reaches full strength; beyond it the multiplier stops moving
    rather than diverging without bound.

    `lean` is `GAME_SCRIPT_LEAN[position]`, independently scaled by
    `game_script_favorite_scale` (positive leans: RB, DEF) or
    `game_script_underdog_scale` (negative leans: QB, WR, TE) — both
    default 1.0, an exact no-op reproducing the original single-table
    behavior. This is what makes "drop the pass-catcher discount, keep
    only the RB lean" a config sweep instead of a source edit.

    `cfg.game_script_weight` defaults to 0.0 and should stay there — see
    its docstring in `ffbot/config.py` for the backtest finding and
    docs/BACKTEST.md's B5 section for the full writeup.
    """
    if cfg.game_script_weight == 0.0:
        return 1.0
    if game is None or game.team_total is None or game.opp_total is None:
        return 1.0
    lean = GAME_SCRIPT_LEAN.get(position, 0.0)
    if lean == 0.0:
        return 1.0
    lean *= cfg.game_script_favorite_scale if lean > 0.0 else cfg.game_script_underdog_scale
    if lean == 0.0:
        return 1.0

    margin = game.team_total - game.opp_total  # positive = favored
    scale = max(1.0, cfg.game_script_scale)
    normalized = max(-1.0, min(1.0, margin / scale))
    return max(0.0, 1.0 + cfg.game_script_weight * lean * normalized)


# --- Spice: volatility + upside lean ----------------------------------------


def volatility_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 researched boom/bust rating, or 0.0 when nothing was claimed."""
    if entry is None or entry.volatility is None:
        return 0.0
    return max(0.0, min(100.0, entry.volatility)) / 100.0


def upside_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 researched spike-week potential, or 0.0 when nothing was claimed."""
    if entry is None or entry.upside is None:
        return 0.0
    return max(0.0, min(100.0, entry.upside)) / 100.0


def usage_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 recent target/air-yards share TREND (see
    `ffbot.history.signals.usage_form`), or 0.0 when nothing was claimed —
    same contract as `volatility_score`/`upside_score`."""
    if entry is None or entry.usage_trend is None:
        return 0.0
    return max(0.0, min(100.0, entry.usage_trend)) / 100.0


def momentum_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 recent SCORING trend vs. season average (see
    `ffbot.history.signals.scoring_form`), or 0.0 when nothing was claimed —
    same contract as `usage_score`. Deliberately a separate field from
    `usage_trend`: a points streak and a role/opportunity trend are
    different measurements (see `scoring_form`'s docstring on why), not two
    names for the same thing."""
    if entry is None or entry.momentum is None:
        return 0.0
    return max(0.0, min(100.0, entry.momentum)) / 100.0


def divergence_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 role-trend-minus-production-trend (see
    `ffbot.history.signals.usage_divergence`), or 0.0 when nothing was
    claimed — same contract as `usage_score`/`momentum_score`."""
    if entry is None or entry.divergence is None:
        return 0.0
    return max(0.0, min(100.0, entry.divergence)) / 100.0


def kalshi_score(entry: WeeklyPlayerIntel | None) -> float:
    """0..1 market-vs-projection divergence rank (see the module-level note
    on `WeeklyPlayerIntel.kalshi`), or 0.0 when nothing was claimed — same
    contract as `usage_score`/`momentum_score`/`divergence_score`. Priced
    like a trend fact (`kalshi_weight`, spice-level-4-only), never lean-
    scaled: a market read is a fact about the player this week, not a
    variance bet the way `volatility_weight`/`upside_lean_weight` are."""
    if entry is None or entry.kalshi is None:
        return 0.0
    return max(0.0, min(100.0, entry.kalshi)) / 100.0


_MIN_MATCHUP_VARIANCE_MULTIPLIER = 0.0
_MAX_MATCHUP_VARIANCE_MULTIPLIER = 2.0


def matchup_lean(my_total: float, opp_total: float) -> float:
    """-1..1: how much of an underdog (-1) or favorite (+1) this week's
    matchup makes you, from season-scale optimal-lineup strength on both
    rosters — a heuristic proxy for a real per-week margin, since no Vegas
    line exists for a fantasy-vs-fantasy matchup. 0.0 (no lean) whenever
    neither total is positive — nothing to compare.
    """
    if my_total <= 0.0 and opp_total <= 0.0:
        return 0.0
    denom = max(my_total, opp_total, 1.0)
    return max(-1.0, min(1.0, (my_total - opp_total) / denom))


def _variance_multiplier(lean: float, cfg: SeasonConfig) -> float:
    """Scales volatility_weight/upside_lean_weight by how big an underdog
    (favors variance -- amplify) or favorite (favors floor -- dampen) this
    week's matchup makes you. `matchup_variance_weight == 0.0` (the
    default) or `lean == 0.0` (matchup unknown) both leave this at exactly
    1.0 — an unconditioned, exact no-op in either case.
    """
    if cfg.matchup_variance_weight == 0.0 or lean == 0.0:
        return 1.0
    return max(
        _MIN_MATCHUP_VARIANCE_MULTIPLIER,
        min(_MAX_MATCHUP_VARIANCE_MULTIPLIER, 1.0 - cfg.matchup_variance_weight * lean),
    )


def team_projected_total(
    roster_keys: Sequence[str], board: Board, roster_positions: dict[str, int], cfg: Config
) -> float:
    """Season-scale optimal-lineup total for a set of board keys — the same
    computation `draft.need`'s valuation is built on, reused here to build a
    comparable number for an OPPONENT roster (which the draft module's own
    functions are never hand `league_rosters.yml` names for; see
    `denial.rival_roster_keys`, the same pattern this borrows)."""
    players = [to_player(board.by_key[k], uid) for uid, k in enumerate(roster_keys, start=1) if k in board.by_key]
    plan = optimize(players, roster_positions, None, cfg)
    return sum(p.projected_points for _, p in plan.assignments)


def decision_scale(players: Sequence[Player]) -> float:
    """How much value separates this roster's real options this week.

    The gap between the best projected player and roughly the bench-depth-th
    (default 6th-from-top) player. This is the unit spice weights are
    expressed in — the same reasoning as `edge.decision_scale`: a flat bonus
    calibrated for a week where every option is close would swamp a week
    where the roster's real order is obvious, and vice versa. Floored so a
    roster with almost no spread (bye-week-depleted, say) still lets a small,
    proportionate spice effect through rather than going to exactly zero.
    """
    pts = sorted((p.projected_points or 0.0) for p in players)
    if not pts:
        return _MIN_DECISION_SCALE
    if len(pts) == 1:
        return max(pts[0], _MIN_DECISION_SCALE)
    idx = max(0, len(pts) - 1 - _DECISION_BENCH_DEPTH)
    spread = pts[-1] - pts[idx]
    return max(spread, _MIN_DECISION_SCALE)


def spice_bonus(
    player: Player, weekly: WeeklyIntel, cfg: SeasonConfig, scale: float, lean: float = 0.0
) -> float:
    """Additive points adjustment from volatility + upside lean + usage
    trend + scoring momentum + usage/production divergence.

    This exists specifically so a genuinely close start/sit call can still go
    to the higher-ceiling player on a week with no notable weather or Vegas
    story — without it, a calm week collapses to "whatever the projection
    already said," which reads as just re-deriving consensus.

    `lean` (from `matchup_lean`, default 0.0 = unknown/no-op) scales the
    volatility/upside_lean pieces via `_variance_multiplier` — a big
    underdog wants ceiling and correlation, a big favorite wants floor.
    `usage_weight`/`momentum_weight`/`divergence_weight` are deliberately
    NOT lean-scaled: a real trend is a fact about the player, not a
    variance bet, so each applies the same whether you're up or down.
    """
    entry = weekly.players.get(normalize_name(player.name))
    variance_mult = _variance_multiplier(lean, cfg)
    fraction = variance_mult * (
        cfg.volatility_weight * volatility_score(entry) + cfg.upside_lean_weight * upside_score(entry)
    )
    fraction += cfg.usage_weight * usage_score(entry)
    fraction += cfg.momentum_weight * momentum_score(entry)
    fraction += cfg.divergence_weight * divergence_score(entry)
    fraction += cfg.kalshi_weight * kalshi_score(entry)
    return fraction * scale


def _momentum_multiplier(entry: WeeklyPlayerIntel | None, cfg: SeasonConfig) -> float:
    """Multiplicative bump to a THIS-WEEK point estimate from usage/momentum/
    divergence trend — the `rank_streamers`/`waiver_candidates` analog of
    `spice_bonus`'s additive term. Multiplicative rather than additive
    because those two callers have no per-decision `week.decision_scale` to
    anchor an additive fraction against the way `adjusted_players` does
    (`rank_streamers` already builds its `matchup_value` as a product of
    multipliers, not a base-plus-bonus, for the same reason). `entry is
    None` or every weight at 0.0 (the default) is an exact 1.0 no-op,
    floored at 0.0 so a stacked negative weight can't invert a point
    estimate's sign. Deliberately excludes `volatility_weight`/
    `upside_lean_weight` — those are matchup-lean-conditioned variance bets
    for a START/SIT decision between players already on the roster, not a
    fact about whether a free agent is worth adding; the trend signals here
    (usage/momentum/divergence) are.
    """
    if entry is None:
        return 1.0
    bonus = cfg.usage_weight * usage_score(entry) + cfg.momentum_weight * momentum_score(entry)
    bonus += cfg.divergence_weight * divergence_score(entry)
    bonus += cfg.kalshi_weight * kalshi_score(entry)
    return max(0.0, 1.0 + bonus)


# --- Assembling adjusted players ---------------------------------------


def adjusted_players(
    players: Sequence[Player],
    weekly: WeeklyIntel,
    cfg: SeasonConfig,
    stadiums: dict[str, StadiumInfo] | None = None,
    lean: float = 0.0,
) -> list[Player]:
    """The full weekly transform: status override, then weather x vegas x
    game-script x venue-disruption multipliers, then the additive spice
    bonus — feed the result straight into `lineup.optimize()` unchanged,
    exactly as the draft path feeds `edge`-adjusted candidates into the
    same optimizer.

    `lean` is this week's `matchup_lean` (default 0.0 = unknown/no-op) —
    see `spice_bonus`.
    """
    stadiums = stadiums if stadiums is not None else {}
    with_status = apply_status_overrides(players, weekly)
    scale = decision_scale(with_status)

    out: list[Player] = []
    for p in with_status:
        if p.projected_points is None:
            out.append(p)
            continue
        points = p.projected_points
        pos = _primary_position(p)
        team = _resolve_team(pos, p.team, p.name)
        game = weekly.games.get(team)
        points *= weather_multiplier(pos, team, game, cfg, stadiums)
        points *= vegas_multiplier(pos, team, weekly, cfg)
        points *= game_script_multiplier(pos, team, game, cfg)
        points *= venue_disruption_multiplier(pos, game, cfg)
        points += spice_bonus(p, weekly, cfg, scale, lean)
        out.append(replace(p, projected_points=points))
    return out


def _primary_position(player: Player) -> str:
    return player.eligible_positions[0] if player.eligible_positions else ""


def _resolve_team(position: str, team: str, name: str) -> str:
    """The abbreviation to key weather/vegas/games lookups on.

    Defenses routinely carry a blank or full-city-name `team` field rather
    than a clean abbreviation — `names.py` documents this same mess for the
    draft path ("FantasyPros writes 'Ravens D/ST'; Yahoo writes the city").
    `data/stadiums.yml` and a researched `games:` section are both keyed by
    abbreviation, so without this resolution a defense's matchup silently
    fails to be found even when it was genuinely researched — which is
    exactly what happened the first time this was run end to end.
    """
    if position != "DEF":
        return team
    return defense_key(name, team) or team


def same_game_conflicts(plan: LineupPlan, weekly: WeeklyIntel) -> list[str]:
    """Warn when your own started DEF and a started offensive player are on
    opposite sides of the same game — the DEF's whole job is stopping the
    offense you also started, so this is directly self-defeating regardless
    of anyone's projection. A warning, not a valuation penalty: whether the
    correlation is worth avoiding is a variance judgment (see
    `matchup_lean`/`_variance_multiplier`), not a fact this function can
    settle on its own.
    """
    def_starters: list[tuple[str, Player]] = []
    offense_by_team: dict[str, list[Player]] = defaultdict(list)
    for _slot, p in plan.assignments:
        pos = _primary_position(p)
        team = _resolve_team(pos, p.team, p.name)
        if not team:
            continue
        if pos == "DEF":
            def_starters.append((team, p))
        else:
            offense_by_team[team].append(p)

    out: list[str] = []
    for def_team, def_player in def_starters:
        game = weekly.games.get(def_team)
        if game is None or not game.opponent:
            continue
        conflicting = offense_by_team.get(game.opponent)
        if conflicting:
            names = ", ".join(sorted(p.name for p in conflicting))
            out.append(f"{def_player.name} (DEF) is started against your own {names}")
    return out


# --- The brief ---------------------------------------------------------


@dataclass(frozen=True)
class PlayerNote:
    player_id: int
    name: str
    note: str
    flags: tuple[str, ...] = ()


@dataclass
class WeekBrief:
    week: int
    lineup: LineupPlan
    notes: list[PlayerNote] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    unmatched_warnings: list[str] = field(default_factory=list)


def _this_week_matchup_lean(
    roster: Sequence[Player],
    roster_positions: dict[str, int],
    cfg: Config,
    board: Board | None,
    league_rosters: "LeagueRosters | None",
) -> float:
    """0.0 (no-op) unless `board`, `league_rosters`, `league.yml`'s
    `my_opponent`, and that opponent's roster are ALL available — see
    `matchup_lean`'s own docstring for what the number means."""
    if board is None or league_rosters is None or cfg.league is None or not cfg.league.my_opponent:
        return 0.0
    opponent_names = league_rosters.teams.get(cfg.league.my_opponent)
    if not opponent_names:
        return 0.0

    from .denial import rival_roster_keys  # local import: avoids a cycle, same convention as denial.py's own

    my_keys, _missing = roster_board_keys(roster, board)
    opp_keys = rival_roster_keys(opponent_names, board)
    my_total = team_projected_total(my_keys, board, roster_positions, cfg)
    opp_total = team_projected_total(opp_keys, board, roster_positions, cfg)
    return matchup_lean(my_total, opp_total)


def build_week_brief(
    roster: Sequence[Player],
    roster_positions: dict[str, int],
    week: int,
    cfg: Config,
    weekly: WeeklyIntel | None = None,
    stadiums: dict[str, StadiumInfo] | None = None,
    board: Board | None = None,
    league_rosters: "LeagueRosters | None" = None,
) -> WeekBrief:
    """The end-to-end weekly call: adjust the roster, run the same exact
    optimizer the draft path already proved deterministic, and package the
    result with the notes and alerts a human needs to act on it.

    `board`/`league_rosters` are optional and used only to compute this
    week's `matchup_lean` for `SeasonConfig.matchup_variance_weight` —
    omitted (the default), the lean stays 0.0 and every existing call site
    stays bit-identical to before this parameter existed.
    """
    weekly = weekly if weekly is not None else WeeklyIntel()
    unmatched = unmatched_player_warnings(roster, weekly)
    lean = _this_week_matchup_lean(roster, roster_positions, cfg, board, league_rosters)

    adjusted = adjusted_players(roster, weekly, cfg.season, stadiums, lean)
    plan = optimize(adjusted, roster_positions, week, cfg)

    notes: list[PlayerNote] = []
    for p in roster:
        entry = weekly.players.get(normalize_name(p.name))
        if entry and (entry.note or entry.flags):
            notes.append(PlayerNote(p.player_id, p.name, entry.note, entry.flags))

    alerts = list(unmatched)
    if weekly.week is not None and weekly.week != week:
        alerts.append(
            f"weekly intel file is for week {weekly.week}, but this report is for week {week} — "
            "probably a stale file; re-run /intel-refresh"
        )
    # `adjusted`, not `roster`: a status override supplied only by weekly
    # intel (never present on the roster-source `Player` itself) already
    # reached scoring via `apply_status_overrides`, but checking `roster`
    # here would miss it — `adjusted`'s players carry the same post-override
    # `.status` while `selected_position` is untouched by any of this
    # module's transforms, so the "was this a starter going into this run"
    # comparison still means what it always did.
    for p in adjusted:
        if p.status in STATUS_OUT and p.selected_position not in (BENCH,):
            alerts.append(f"{p.name} is {p.status or 'OUT'} and was benched")
    alerts.extend(same_game_conflicts(plan, weekly))

    return WeekBrief(week=week, lineup=plan, notes=notes, alerts=alerts, unmatched_warnings=unmatched)


# --- Streaming (K/DEF by matchup) -------------------------------------


@dataclass(frozen=True)
class StreamCandidate:
    name: str
    position: str
    team: str
    weekly_value: float
    reason: str


def rank_streamers(
    pool: Sequence[BoardPlayer],
    position: str,
    weekly: WeeklyIntel,
    cfg: SeasonConfig,
    limit: int | None = None,
    week: int | None = None,
    stadiums: dict[str, StadiumInfo] | None = None,
) -> list[StreamCandidate]:
    """Rank free-agent K/DEF by this week's matchup rather than season-long
    value — a streamer's entire point is that the matchup dominates their
    track record. `streaming_weight` blends season floor (0.0) with pure
    matchup value (1.0); at the default preset this leans heavily toward
    matchup, which is the point of streaming at all.

    `week` excludes anyone on bye that week outright — a streamer who can't
    play this week isn't a candidate at all, not merely a discounted one.

    Matchup value also carries `weather_multiplier`, not just Vegas — a
    kicker's weekly value is exactly the kind of matchup-dominated read this
    function exists for, and `weather_weight`'s own config comment already
    documents K taking the full discount alongside QB/WR/TE. Missing
    `stadiums` (default `{}`) is the same fail-open no-op as everywhere else
    this dict is threaded through — every game reads as weather-neutral.

    Also carries `_momentum_multiplier` (usage/momentum/divergence trend) —
    note `usage_form`/`usage_divergence` are RB/WR/TE-only, so for K/DEF
    (this function's only real callers) only `scoring_form`'s `momentum`
    can ever be nonzero here; the other two are structurally always a
    no-op for a streamer regardless of weight.
    """
    limit = limit if limit is not None else cfg.recommend_count
    stadiums = stadiums if stadiums is not None else {}
    candidates = [
        bp for bp in pool
        if bp.position == position and not (week is not None and bp.bye_week == week)
    ]

    scored: list[tuple[float, StreamCandidate]] = []
    for bp in candidates:
        team = _resolve_team(position, bp.team, bp.name)
        game = weekly.games.get(team)
        entry = weekly.players.get(normalize_name(bp.name))
        vegas_mult = vegas_multiplier(position, team, weekly, cfg)
        weather_mult = weather_multiplier(position, team, game, cfg, stadiums)
        script_mult = game_script_multiplier(position, team, game, cfg)
        momentum_mult = _momentum_multiplier(entry, cfg)
        matchup_value = bp.points * vegas_mult * weather_mult * script_mult * momentum_mult
        floor_value = bp.points
        blended = cfg.streaming_weight * matchup_value + (1 - cfg.streaming_weight) * floor_value

        reason = f"vs {game.opponent}" if game else "matchup unresearched — season value only"
        if game and game.opp_total is not None:
            reason += f" (opp implied {game.opp_total:.1f})"

        scored.append((blended, StreamCandidate(bp.name, bp.position, bp.team, blended, reason)))

    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:limit]]


# --- Waivers -------------------------------------------------------------


@dataclass(frozen=True)
class WaiverCandidate:
    add_name: str
    position: str
    value: float  # gross gain: replacement-adjusted marginal lineup value
    net: float  # value - drop_cost - claim_cost -- the actual ranking key
    drop_name: str | None
    drop_reason: str
    max_bid: int  # FAAB leagues; 0 under a rolling-priority league
    claim_note: str  # "" under FAAB; a priority-cost verdict under rolling
    reason: str


def roster_board_keys(roster: Sequence[Player], pool: Board) -> tuple[list[str], list[str]]:
    """Each rostered player's key on the season-long board, or the name in
    `missing` when nothing matches — the lookup `waiver_candidates`'
    season-scale comparison (`draft._season_score` and friends) builds on;
    see that function's own docstring for why matching the board's scale
    matters. Factored out so callers that need board *keys* don't have to
    rebuild synthetic `Player`s to get them.
    """
    keys: list[str] = []
    missing: list[str] = []
    for p in roster:
        key = f"{normalize_name(p.name)}:{_primary_position(p)}"
        if key in pool.by_key:
            keys.append(key)
        else:
            missing.append(p.name)
    return keys, missing


# --- Roster capacity and core vs. streaming ---------------------------------
#
# The user's own framing: a bench spot isn't storage, it's an option. Its
# value is what you'd get from it if you didn't hold this player — which,
# given a waiver wire, is streaming. `board.replacement[pos]` is already, by
# construction, "the best player at pos who doesn't crack the aggregate
# optimal lineup across every team in the league" — the best guy reliably
# sitting on the wire — so no new machinery is needed to answer "is this
# player worth more than what I could rotate through his spot instead,"
# only a re-application of `draft.need`'s own marginal-lineup-value trick.
# Core/stream is a DERIVED classification, never a hardcoded slot count.


@dataclass(frozen=True)
class RosterSpace:
    capacity: int  # starters + bench, IR excluded (see models.roster_capacity)
    occupied: int  # rostered players NOT parked on IR
    ir_parked: int  # rostered players currently in an IR slot
    open_spots: int  # capacity - occupied, floored at 0
    must_drop: int  # drops one add of a full roster would require (0 or 1)


def roster_space(roster: Sequence[Player], roster_positions: dict[str, int]) -> RosterSpace:
    """How much room is actually on the roster right now.

    IR is excluded from `occupied`/`capacity` on purpose — an IR-parked
    player isn't using a usable roster spot (`lineup.optimize` already holds
    them in place regardless), so freeing up IR players a claim would then
    need doesn't cost anything here.
    """
    capacity = roster_capacity(roster_positions)
    ir_parked = sum(1 for p in roster if p.selected_position in IR_SLOTS)
    occupied = len(roster) - ir_parked
    open_spots = max(0, capacity - occupied)
    return RosterSpace(
        capacity=capacity, occupied=occupied, ir_parked=ir_parked,
        open_spots=open_spots, must_drop=0 if open_spots > 0 else 1,
    )


def streaming_baseline(position: str, roster_keys: Sequence[str], board: Board, cfg: Config) -> float:
    """Marginal season-long lineup value of the best free agent at
    `position` — what a bench spot is worth as a rotating streamer at that
    position. Exactly `draft.need`'s own `marginal_repl` term, reused rather
    than reimplemented, so it automatically reflects this league's shape,
    size, and (once a `league.yml` exists) its scoring.
    """
    from .draft import _replacement_board_player, _season_score

    repl_points = board.replacement.get(position)
    if repl_points is None:
        return 0.0
    base = _season_score(board, roster_keys, None, cfg)
    repl = _replacement_board_player(position, repl_points)
    return _season_score(board, roster_keys, repl, cfg) - base


def best_streaming_baseline(roster_keys: Sequence[str], board: Board, cfg: Config) -> float:
    """The best a bench spot could be worth as *any* position's streamer —
    the number `hold_margin` compares every player's hold value against.
    Computed once per roster, not once per player: it does not depend on
    which player is being evaluated.
    """
    positions = {bp.position for bp in board.players}
    return max(
        (streaming_baseline(pos, roster_keys, board, cfg) for pos in positions),
        default=0.0,
    )


def drop_cost(player_key: str, roster_keys: Sequence[str], board: Board, cfg: Config) -> float:
    """What holding `player_key` is worth: the season-long optimal-lineup
    points he adds, plus bench depth for a backup who won't crack the
    starting lineup at all.

    `optimize(roster) - optimize(roster \\ {player})` alone is exactly 0 for
    every bench player under a single evaluation — nobody not starting can
    ever show a marginal lineup gain — which is why `week.py` historically
    fell back to sorting bench players by raw projected points instead. The
    fix reuses the codebase's own tuned answer to "what is a player who
    won't crack your starters still worth" — `draft._depth_factors`'
    VOR-based, decaying bench-depth term — rather than inventing a new one.
    """
    from .draft import _depth_factors, _season_score

    without_keys = [k for k in roster_keys if k != player_key]
    full_score = _season_score(board, roster_keys, None, cfg)
    without_score = _season_score(board, without_keys, None, cfg)
    optimize_delta = full_score - without_score

    bp = board.by_key.get(player_key)
    if bp is None:
        return optimize_delta
    # Depth factors computed from the roster WITHOUT this player, matching
    # how `draft.recommend`/`draft.need` size the same term for a candidate
    # not yet drafted — including him here would double-count his own slot.
    without_bps = [board.by_key[k] for k in without_keys if k in board.by_key]
    depth_factors = _depth_factors(without_bps, cfg.roster_positions, cfg)
    depth_term = cfg.draft.depth_weight * max(0.0, bp.vor) * depth_factors.get(bp.position, 1.0)
    return optimize_delta + depth_term


def hold_margin(
    player_key: str,
    roster_keys: Sequence[str],
    board: Board,
    cfg: Config,
    is_blocking: bool = False,
) -> float:
    """`drop_cost(p) - max over q of streaming_baseline(q)`: does holding
    this player beat the best thing a waiver claim could put in his spot
    instead? Positive = CORE, non-positive = STREAM — see `classify_roster`.

    `is_blocking` (from `roster.yml`'s `blocking: true` — `Player.blocking`)
    adds `cfg.season.blocking_hold_bonus`, an explicit, honest admission
    that a hold is about denying a rival rather than your own lineup — this
    function has no way to see denial value on its own, since it is a
    function of your roster alone.
    """
    margin = drop_cost(player_key, roster_keys, board, cfg) - best_streaming_baseline(roster_keys, board, cfg)
    if is_blocking:
        margin += cfg.season.blocking_hold_bonus
    return margin


@dataclass(frozen=True)
class RosterPlayerClass:
    name: str
    position: str
    hold_margin: float
    classification: str  # "CORE" | "STREAM"


def classify_roster(
    roster: Sequence[Player], pool: Board, cfg: Config
) -> tuple[list[RosterPlayerClass], list[str]]:
    """CORE vs. STREAM for every rostered player, by `hold_margin` — never a
    hardcoded slot count. A 3rd RB is CORE in a deep league and STREAM in a
    shallow one, automatically, purely because `streaming_baseline` differs.

    `cfg.season.min_stream_spots` (default 0, a no-op) is a floor on how
    many spots stay STREAM-classified regardless — a taste for roster
    optionality, applied by demoting the weakest-margin CORE players, never
    an input to the classification math itself.

    Returns `(classes, missing)` — `missing` lists roster names with no
    season-board match, same contract as `roster_board_keys`.
    """
    keys, missing = roster_board_keys(roster, pool)
    baseline = best_streaming_baseline(keys, pool, cfg)
    by_key_player = {
        f"{normalize_name(p.name)}:{_primary_position(p)}": p for p in roster
    }

    classes: list[RosterPlayerClass] = []
    for key in keys:
        bp = pool.by_key[key]
        p = by_key_player.get(key)
        margin = drop_cost(key, keys, pool, cfg) - baseline
        if p is not None and p.blocking:
            margin += cfg.season.blocking_hold_bonus
        classes.append(
            RosterPlayerClass(
                name=bp.name, position=bp.position, hold_margin=margin,
                classification="CORE" if margin > 0 else "STREAM",
            )
        )

    min_stream = cfg.season.min_stream_spots
    stream_count = sum(1 for c in classes if c.classification == "STREAM")
    if stream_count < min_stream:
        core_idx = sorted(
            (i for i, c in enumerate(classes) if c.classification == "CORE"),
            key=lambda i: classes[i].hold_margin,
        )
        demote = set(core_idx[: min_stream - stream_count])
        classes = [
            replace(c, classification="STREAM") if i in demote else c
            for i, c in enumerate(classes)
        ]

    return classes, missing


@dataclass(frozen=True)
class RosterStatus:
    space: RosterSpace
    core: list[RosterPlayerClass]
    stream: list[RosterPlayerClass]
    missing: list[str]


def build_roster_status(
    roster: Sequence[Player], roster_positions: dict[str, int], pool: Board, cfg: Config
) -> RosterStatus:
    """The ROSTER section: capacity plus the derived CORE/STREAM split."""
    classes, missing = classify_roster(roster, pool, cfg)
    return RosterStatus(
        space=roster_space(roster, roster_positions),
        core=[c for c in classes if c.classification == "CORE"],
        stream=[c for c in classes if c.classification == "STREAM"],
        missing=missing,
    )


def _week_score(
    roster: Sequence[Player], extra: Player | None, roster_positions: dict[str, int], cfg: Config
) -> float:
    """Starting-lineup total using each `Player`'s OWN `projected_points` —
    the this-week-scale analog of `draft._season_score`, which always reads
    season-scale points off the board instead. Used only for `ros_blend`,
    where "this week's number" has to come from the roster as the caller
    actually passed it in, not from a re-lookup against the season board.
    """
    players = list(roster) if extra is None else list(roster) + [extra]
    plan = optimize(players, roster_positions, None, cfg)
    return sum(p.projected_points or 0.0 for _, p in plan.assignments)


def waiver_candidates(
    roster: Sequence[Player],
    pool: Board,
    roster_positions: dict[str, int],
    cfg: Config,
    remaining_faab: int = 0,
    my_priority: int | None = None,
    weeks_remaining: int = 17,
    league_rosters: LeagueRosters | None = None,
    limit: int | None = None,
    week: int | None = None,
    weekly: WeeklyIntel | None = None,
    weekly_points: dict[str, float] | None = None,
) -> tuple[list[WaiverCandidate], list[str]]:
    """Ranked free-agent adds, each paired with a drop (or none, given an
    open roster spot) and a cost.

    SCALE: `pool`'s points are season totals (the same board the draft
    used); `roster` is whatever scale the caller has it at, typically this
    week's adjusted projections. Comparing a weekly number against a season
    number directly would rank every waiver candidate as a massive upgrade
    over your actual starters, off by roughly a factor of 17 — `gain`'s two
    halves (`ros_gain`/`week_gain` below) exist specifically to keep both
    sides of every comparison on a matching scale; see `roster_board_keys`
    for the lookup this relies on and `weeks_remaining`'s own note further
    down for the matching pitfall on the this-week half.

    Four real changes from the first cut of this function:

    1. **Replacement subtraction.** `gain` is now `marginal_x - marginal_repl`
       — exactly `draft.need`'s contract — not raw marginal lineup value.
       Without it, a candidate at a thin position reads the same as a real
       starter over nothing.
    2. **Per-candidate drop, by `hold_margin` not raw season points.**
       `hold_margin` already weights a starter's own contribution (hundreds
       of points via `drop_cost`'s `optimize_delta` term) far above any
       bench player's, so the worst-`hold_margin` droppable player is never
       a starter unless literally the whole roster is starters — which is
       what fixes the historical bug of `droppable()` degenerating to
       roster-list order and suggesting the starting QB. One shared "best
       available drop" is genuinely correct here, not a simplification:
       this league's bench slots (`BN`) are position-generic
       (`models.slot_accepts`), so there is no per-position bench to search
       separately — the same drop clears a roster spot for any add.
    3. **Open spots need no drop.** `RosterSpace.open_spots > 0` sets
       `drop_cost = 0`, `drop_name = None` — a real add doesn't have to
       manufacture a cut just because the old code always paired one.
    4. **Ranked by `net = gain - drop_cost - claim_cost`, not gross `gain`.**
       An add that costs you a useful player (or FAAB, or rolling priority)
       is worth less than the identical gain landing in an open bench spot.

    Cost model depends on `cfg.league.waiver_type` (defaults to `"faab"`
    when no `league.yml` is configured):

    - **`"faab"`** — `remaining_faab` sized through `policy.max_faab_bid`,
      unchanged from the original behavior.
    - **`"rolling"`** — no bids exist. The cost of a claim is spending your
      current priority position; `my_priority` (1 = best/most valuable to
      keep, `cfg.draft.num_teams` = worst/cheapest to spend) sizes
      `claim_cost` as a fraction of *that candidate's own gain*
      (`priority_value * gain * (1 - my_priority / num_teams)`) — a
      genuine "decision scale" for a waiver claim doesn't exist anywhere
      else in this codebase, and a claim's own value is the only
      unambiguous unit at hand to scale the cost against. `my_priority`
      unknown (`None`) assumes the cheapest end (no unusual urgency) rather
      than the most expensive, so an unset value can't silently suppress
      every claim.

    `ros_blend` blends `gain` (pure rest-of-season, via the season board)
    with `_week_score`'s this-week-scale equivalent (the roster exactly as
    passed in, and the candidate's season points divided by
    `weeks_remaining` as its weekly-equivalent, mirroring
    `roster_source.season_board_rows`'s own rescaling) — `1.0` is pure ROS,
    `0.0` is pure this-week. `week` (the current NFL week) zeroes a
    candidate's this-week-scale contribution when their `bye_week` matches —
    otherwise a bye-week player's board-derived season points get divided
    into a phantom weekly number as if they were playing, which can rank
    them above a real, available streamer under a low `ros_blend`. The ROS
    side of the blend is untouched: a bye is a one-week absence, not a
    reason to devalue the rest of their season.

    IMPORTANT: `weeks_remaining` must be the FIXED season length (e.g. 17)
    when the roster it's being compared against was built via
    `roster_source.season_board_rows`'s fallback, not a shrinking
    "weeks remaining in the season" — the two must use the identical
    divisor or a candidate's board-fallback price silently drifts out of
    scale with a rostered player's (see `season_board_rows`'s own
    docstring). `webapi.py`/`week_report.py` both pass `weeks_in_season`
    here for exactly this reason, despite the parameter's name.

    `league_rosters` (from `scripts/import_league_rosters.py`) excludes
    every OTHER team's rostered players from the candidate pool — without
    it, this function has no idea the other 11 teams exist and will happily
    recommend adding a player who is already on someone else's roster.
    Missing/`None` is a no-op, same contract as every other optional
    research input in this codebase.

    `weekly`, when given, applies `_momentum_multiplier` (usage/momentum/
    divergence trend) to a candidate's THIS-WEEK point estimate only —
    never to `ros_gain`/`marginal_x`, which stay on the season board
    untouched. This is deliberate, not an oversight: `ros_blend`'s own
    comment exists precisely so "a one-week hot streak doesn't outrank a
    real weekly starter," and folding a trend signal into the ROS half
    would defeat the guard that comment describes. `None` (the default) is
    an exact no-op, same contract as every other optional research input
    here.

    `weekly_points` (`{board key: real this-week points}`, from
    `ffbot.projections` — see `report.load_everything`), when given and
    covering a candidate, REPLACES `bp.points / weeks_remaining` as that
    candidate's this-week estimate — a real live weekly number instead of
    the frozen preseason board rescaled down. A candidate this map doesn't
    cover (a name a live source hasn't picked up yet) falls back to the old
    board-rescaled estimate for that one candidate only, same partial-
    coverage philosophy as `weekly`/`league_rosters`. `None` (the default)
    is an exact no-op. Still applies the bye-zeroing and momentum-multiplier
    logic on top, same as the board-rescaled path. NOTE: `ros_gain` is
    UNTOUCHED by this — it stays fully board-derived via `_season_score`
    regardless (see that function's own docstring); only the this-week half
    of the blend is real here. A real rest-of-season replacement for
    `ros_gain` is a deeper, separate change (`_season_score` rebuilds every
    player from `pool.by_key`, roster included, with no override hook
    today) — deliberately not attempted in this pass.

    `cfg.season.waiver_value_mode` picks between two entirely different
    ranking strategies (B7, see that field's own docstring):

    - `"marginal"` (levels 2-4) — everything described above: replacement
      subtraction, `hold_margin`-based drop pairing, `ros_blend`, the
      `gain <= 0` filter.
    - `"points"` (level 1 only) — the deliberately naive baseline. `gain`
      is just this week's raw projected points for the candidate (live
      `weekly_points` where covered, else `bp.points` prorated over
      `weeks_remaining`, bye-zeroed exactly like the marginal path); no
      replacement subtraction, no `ros_blend`, no `gain <= 0` filter (every
      positive-or-zero candidate is ranked, not just ones that beat a
      VOR-derived bar). The paired drop is the droppable roster player with
      the LOWEST `projected_points`, not the worst `hold_margin` — the
      naive mode has no concept of "what a bench spot is worth as a
      streamer," only "who's scoring the fewest points right now." Its
      cost is always 0.0 — the naive mode doesn't do cost/benefit
      accounting, only ranking. `policy.can_drop`/`policy.can_bid_on`'s
      safety guardrails still apply in both modes; those are facts about
      the transaction, not strategy.

    Returns `(candidates, unmatched_roster_names)` — the second is anyone on
    the roster with no season-board entry, so the caller can warn rather
    than silently value them at zero.
    """
    from . import policy  # local import: avoids a cycle at module load time
    from .draft import _replacement_board_player, _season_score

    naive = cfg.season.waiver_value_mode == "points"
    limit = limit if limit is not None else cfg.season.recommend_count
    roster_keys, missing = roster_board_keys(roster, pool)
    space = roster_space(roster, roster_positions)

    rostered_names = {normalize_name(p.name) for p in roster}
    if league_rosters is not None:
        rostered_names = rostered_names | league_rosters.rostered_names()
    pool_size = max(1, cfg.season.waiver_pool_size)
    available = [bp for bp in pool.players if normalize_name(bp.name) not in rostered_names][:pool_size]

    base_score = _season_score(pool, roster_keys, None, cfg)
    week_base = _week_score(roster, None, roster_positions, cfg)

    # One shared "best available drop" — see point 2 above for why a single
    # ranking is correct here rather than a per-candidate search. Naive
    # mode ranks by raw projected points instead of hold_margin — see the
    # docstring's waiver_value_mode section.
    key_to_player = {
        f"{normalize_name(p.name)}:{_primary_position(p)}": p for p in roster
    }
    droppable_keys = [
        k for k in roster_keys
        if k in key_to_player and policy.can_drop(key_to_player[k], cfg).allowed
    ]
    if naive:
        droppable_keys.sort(key=lambda k: key_to_player[k].projected_points or 0.0)
    else:
        droppable_keys.sort(key=lambda k: hold_margin(k, roster_keys, pool, cfg, key_to_player[k].blocking))
    best_drop_key = droppable_keys[0] if droppable_keys else None
    best_drop_cost = (
        0.0 if naive
        else (max(0.0, drop_cost(best_drop_key, roster_keys, pool, cfg)) if best_drop_key else 0.0)
    )

    waiver_type = cfg.league.waiver_type if cfg.league is not None else "faab"
    max_bid = policy.max_faab_bid(remaining_faab, cfg) if waiver_type != "rolling" else 0
    num_teams = max(1, cfg.draft.num_teams)
    priority = my_priority if my_priority is not None else num_teams  # unknown -> assume no urgency

    denial_active = (
        league_rosters is not None and league_rosters.teams and cfg.season.denial_weight != 0.0
    )
    if denial_active:
        from . import denial

    repl_marginal: dict[str, float] = {}
    scored: list[tuple[float, WaiverCandidate]] = []
    for bp in available:
        if waiver_type != "rolling":
            # Inert without live Yahoo data today — `percent_owned` is never
            # populated on a board-derived candidate, so this always passes
            # — but it is the real call, wired the same way
            # `protect_pct_owned` is in `policy.can_drop`, ready the moment
            # M3 supplies real ownership%. Runs in BOTH waiver_value_modes —
            # this is a policy guardrail (a fact about the transaction),
            # not VOR strategy.
            candidate_check = Player(
                player_id=-1, name=bp.name, eligible_positions=[bp.position],
                percent_owned=None,
            )
            if not policy.can_bid_on(candidate_check, cfg).allowed:
                continue

        on_bye_this_week = week is not None and bp.bye_week == week
        real_weekly = weekly_points.get(bp.key) if weekly_points is not None else None
        if on_bye_this_week:
            candidate_week_pts = 0.0
        elif real_weekly is not None:
            candidate_week_pts = real_weekly
        else:
            candidate_week_pts = bp.points / max(1, weeks_remaining)
        if weekly is not None and not on_bye_this_week:
            entry = weekly.players.get(normalize_name(bp.name))
            candidate_week_pts *= _momentum_multiplier(entry, cfg.season)

        if naive:
            # No replacement subtraction, no ros_blend, no gain<=0 filter —
            # rank purely by this week's raw projected points. See the
            # docstring's waiver_value_mode section.
            gain = candidate_week_pts
        else:
            repl_points = pool.replacement.get(bp.position)
            if bp.position not in repl_marginal:
                if repl_points is None:
                    repl_marginal[bp.position] = 0.0
                else:
                    repl = _replacement_board_player(bp.position, repl_points)
                    repl_marginal[bp.position] = _season_score(pool, roster_keys, repl, cfg) - base_score

            marginal_x = _season_score(pool, roster_keys, bp, cfg) - base_score
            ros_gain = marginal_x - repl_marginal[bp.position]

            candidate_player = Player(
                player_id=-1, name=bp.name, eligible_positions=[bp.position],
                selected_position=BENCH, team=bp.team, bye_week=bp.bye_week,
                projected_points=candidate_week_pts,
            )
            week_gain = _week_score(roster, candidate_player, roster_positions, cfg) - week_base

            blend = cfg.season.ros_blend
            gain = blend * ros_gain + (1.0 - blend) * week_gain
            if gain <= 0.0:
                continue

        if space.open_spots > 0:
            drop_name, drop_reason, this_drop_cost = None, "open roster spot — no drop needed", 0.0
        elif best_drop_key is not None:
            drop_name = key_to_player[best_drop_key].name
            drop_reason = "lowest projected points on your roster" if naive else "worst hold value on your roster"
            this_drop_cost = best_drop_cost
        else:
            drop_name, drop_reason, this_drop_cost = (
                None, "no droppable player found — roster is full of protected players", 0.0,
            )

        if waiver_type == "rolling":
            claim_cost = cfg.season.priority_value * max(0.0, gain) * (1.0 - priority / num_teams)
            claim_note = f"CLAIM (priority {priority}/{num_teams})" if claim_cost < gain else "HOLD PRIORITY"
        else:
            claim_cost = 0.0
            claim_note = ""

        # Claim urgency: a rival threatening to grab this first is an
        # ordinary reason to move now, not a speculative one, so it folds
        # into net directly — unlike a pure denial hold (denial.py's
        # `denial_candidates`), which has no lineup value of its own and
        # stays a separately flagged recommendation. Exactly 0.0 whenever
        # `league_rosters`/`denial_weight` aren't set — see `denial_value`.
        urgency = 0.0
        if denial_active:
            urgency = denial.denial_value(bp, league_rosters, pool, roster_positions, cfg)

        reason = (
            f"+{gain:.1f} projected points this week" if naive
            else f"+{gain:.1f} season pts over your current lineup"
        )
        if on_bye_this_week:
            reason += " (on bye this week)"
        if urgency > 0:
            reason += f" (+{urgency:.1f} claim urgency — a rival needs him too)"

        net = gain - this_drop_cost - claim_cost + urgency
        scored.append((
            net,
            WaiverCandidate(
                add_name=bp.name, position=bp.position, value=gain, net=net,
                drop_name=drop_name, drop_reason=drop_reason,
                max_bid=max_bid, claim_note=claim_note, reason=reason,
            ),
        ))

    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:limit]], missing


# --- IR stash --------------------------------------------------------------


@dataclass(frozen=True)
class IrStashCandidate:
    add_name: str
    position: str
    value: float  # season value; no drop pairing -- an open IR slot costs nothing
    reason: str


def ir_stash_candidates(
    roster: Sequence[Player],
    pool: Board,
    roster_positions: dict[str, int],
    weekly: WeeklyIntel,
    cfg: Config,
    league_rosters: LeagueRosters | None = None,
    limit: int | None = None,
) -> list[IrStashCandidate]:
    """Free agents researched as IR-eligible who could be added straight to
    IR at zero bench cost — this league allows adding injured players from
    waivers directly to an IR slot, so a stash costs nothing on the active
    roster as long as one is open.

    Necessarily as complete as `weekly/week-NN.yml`'s own research, not a
    scan of the whole free-agent pool — there is no offline source for
    "every free agent's current injury status," only whichever names
    `/gameday` happened to research that week. A name researched but not
    yet on the board (a mismatch) is simply absent here, same failure mode
    `unmatched_player_warnings` already covers for the roster.
    """
    ir_capacity = ir_slots(roster_positions)
    parked = sum(1 for p in roster if p.selected_position in IR_SLOTS)
    if parked >= ir_capacity:
        return []

    limit = limit if limit is not None else cfg.season.recommend_count
    rostered_names = {normalize_name(p.name) for p in roster}
    if league_rosters is not None:
        rostered_names = rostered_names | league_rosters.rostered_names()
    by_name: dict[str, list[BoardPlayer]] = {}
    for bp in pool.players:
        by_name.setdefault(normalize_name(bp.name), []).append(bp)

    out: list[IrStashCandidate] = []
    for name_key, entry in weekly.players.items():
        if entry.status not in STATUS_IR_ELIGIBLE or name_key in rostered_names:
            continue
        matches = by_name.get(name_key)
        if not matches:
            continue
        bp = matches[0]
        out.append(IrStashCandidate(
            add_name=bp.name, position=bp.position, value=bp.points,
            reason=f"IR-eligible ({entry.status}) — zero bench cost, open IR slot available",
        ))

    out.sort(key=lambda c: -c.value)
    return out[:limit]
