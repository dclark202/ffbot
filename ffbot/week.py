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
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

import yaml

from .board import Board, BoardPlayer
from .config import Config, SeasonConfig
from .lineup import LineupPlan, optimize
from .models import BENCH, STATUS_OUT, Player
from .names import defense_key, normalize_name, search_scored

# Positions weather can plausibly affect at all. DEF is deliberately excluded
# — bad weather tends to help defenses (more punts, more turnovers, shorter
# fields) rather than hurt them, so applying the same penalty there would be
# backwards, not just noisy.
WEATHER_AFFECTED_POSITIONS = frozenset({"QB", "WR", "TE", "K", "RB"})

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
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameInfo:
    """One team's game this week, as researched — never assumed from a
    typical weekday slot. See INSEASON.md: Saturday games and international
    early kickoffs make a fixed schedule wrong often enough to matter.
    """

    opponent: str
    kickoff_et: str = ""  # ISO datetime string, e.g. "2026-09-14T13:00"
    home: bool = True
    wind_mph: float | None = None
    precip_pct: float | None = None
    team_total: float | None = None  # this team's Vegas implied total
    opp_total: float | None = None  # opponent's Vegas implied total


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

    Depends on who is hosting, not on the player's own team — an away game at
    a dome is weather-neutral even for a team whose home stadium is outdoors.
    Unknown roof (missing stadium data) is treated as neutral rather than
    penalized, since a missing lookup is a data gap, not evidence of bad
    weather.
    """
    if game is None:
        return True
    venue_team = team if game.home else game.opponent
    info = stadiums.get(venue_team)
    return info.dome if info else True


def weather_severity(game: GameInfo | None, cfg: SeasonConfig) -> float:
    """0..1 how bad the forecast is, independent of position.

    Below both thresholds this is exactly 0.0, not a small positive number —
    a 6mph breeze is not weather, and treating it as a tiny discount would
    make every single player quietly underrated for no real reason.
    """
    if game is None:
        return 0.0
    wind = game.wind_mph or 0.0
    precip = game.precip_pct or 0.0

    wind_sev = 0.0
    if wind > cfg.wind_threshold_mph:
        wind_sev = min(1.0, (wind - cfg.wind_threshold_mph) / cfg.wind_threshold_mph)

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


def spice_bonus(player: Player, weekly: WeeklyIntel, cfg: SeasonConfig, scale: float) -> float:
    """Additive points adjustment from volatility + upside lean.

    This exists specifically so a genuinely close start/sit call can still go
    to the higher-ceiling player on a week with no notable weather or Vegas
    story — without it, a calm week collapses to "whatever the projection
    already said," which reads as just re-deriving consensus.
    """
    entry = weekly.players.get(normalize_name(player.name))
    fraction = cfg.volatility_weight * volatility_score(entry) + cfg.upside_lean_weight * upside_score(entry)
    return fraction * scale


# --- Assembling adjusted players ---------------------------------------


def adjusted_players(
    players: Sequence[Player],
    weekly: WeeklyIntel,
    cfg: SeasonConfig,
    stadiums: dict[str, StadiumInfo] | None = None,
) -> list[Player]:
    """The full weekly transform: status override, then weather x vegas
    multipliers, then the additive spice bonus — feed the result straight
    into `lineup.optimize()` unchanged, exactly as the draft path feeds
    `edge`-adjusted candidates into the same optimizer.
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
        points *= weather_multiplier(pos, team, weekly.games.get(team), cfg, stadiums)
        points *= vegas_multiplier(pos, team, weekly, cfg)
        points += spice_bonus(p, weekly, cfg, scale)
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


def build_week_brief(
    roster: Sequence[Player],
    roster_positions: dict[str, int],
    week: int,
    cfg: Config,
    weekly: WeeklyIntel | None = None,
    stadiums: dict[str, StadiumInfo] | None = None,
) -> WeekBrief:
    """The end-to-end weekly call: adjust the roster, run the same exact
    optimizer the draft path already proved deterministic, and package the
    result with the notes and alerts a human needs to act on it.
    """
    weekly = weekly if weekly is not None else WeeklyIntel()
    unmatched = unmatched_player_warnings(roster, weekly)

    adjusted = adjusted_players(roster, weekly, cfg.season, stadiums)
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
    for p in roster:
        if p.status in STATUS_OUT and p.selected_position not in (BENCH,):
            alerts.append(f"{p.name} is {p.status or 'OUT'} and was benched")

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
) -> list[StreamCandidate]:
    """Rank free-agent K/DEF by this week's matchup rather than season-long
    value — a streamer's entire point is that the matchup dominates their
    track record. `streaming_weight` blends season floor (0.0) with pure
    matchup value (1.0); at the default preset this leans heavily toward
    matchup, which is the point of streaming at all.
    """
    limit = limit if limit is not None else cfg.recommend_count
    candidates = [bp for bp in pool if bp.position == position]

    scored: list[tuple[float, StreamCandidate]] = []
    for bp in candidates:
        team = _resolve_team(position, bp.team, bp.name)
        vegas_mult = vegas_multiplier(position, team, weekly, cfg)
        matchup_value = bp.points * vegas_mult
        floor_value = bp.points
        blended = cfg.streaming_weight * matchup_value + (1 - cfg.streaming_weight) * floor_value

        game = weekly.games.get(team)
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
    value: float
    drop_name: str | None
    drop_reason: str
    max_bid: int
    reason: str


def _season_scale_roster(roster: Sequence[Player], pool: Board) -> tuple[list[Player], list[str]]:
    """Look each rostered player up on the season-long board.

    `pool`'s points are season totals (the same board the draft used, kept
    current by `/intel-refresh`); the roster passed into `waiver_candidates`
    is whatever scale the caller has it at, typically this week's adjusted
    projections. Comparing a weekly number against a season number directly
    would rank every waiver candidate as a massive upgrade over your actual
    starters, off by roughly a factor of 17 — this is what makes the two
    comparable.

    Returns `(season-scale roster, names with no board match)` — the second
    list is usually short (an in-season pickup that predates the last board
    refresh, or a name mismatch), but real, and callers should surface it
    rather than silently under-counting the roster.
    """
    out: list[Player] = []
    missing: list[str] = []
    for i, p in enumerate(roster):
        bp = pool.by_key.get(f"{normalize_name(p.name)}:{_primary_position(p)}")
        if bp is None:
            missing.append(p.name)
            continue
        out.append(
            Player(
                player_id=-(i + 1), name=bp.name, eligible_positions=[bp.position],
                selected_position=BENCH, team=bp.team, bye_week=bp.bye_week,
                projected_points=bp.points,
            )
        )
    return out, missing


def waiver_candidates(
    roster: Sequence[Player],
    pool: Board,
    roster_positions: dict[str, int],
    remaining_faab: int,
    cfg: Config,
    limit: int | None = None,
) -> tuple[list[WaiverCandidate], list[str]]:
    """Ranked free-agent adds, each paired with a drop and a bid.

    Mirrors `draft.need()`'s marginal-value trick — add the candidate, see
    how much the season-long optimal lineup gains — run against the current
    roster instead of a draft-in-progress one, on the season-scale roster
    `_season_scale_roster` builds. Drop pairing is deliberately the single
    worst droppable player on the whole roster, not a per-candidate optimal
    pairing — "is this an upgrade over my worst bench piece" is the question
    that actually matters; who specifically clears the roster spot is a
    separate, lower-stakes call not worth the extra complexity here.

    Every drop suggestion is filtered through `policy.can_drop` (only
    protections that don't need Yahoo data — ownership% and draft-round
    protection stay inert without it, see `roster_source`) and every bid
    through `policy.max_faab_bid`, unmodified.

    Returns `(candidates, unmatched_roster_names)` — the second is anyone on
    the roster with no season-board entry, so the caller can warn rather than
    silently value them at zero.
    """
    from . import policy  # local import: avoids a cycle at module load time

    limit = limit if limit is not None else cfg.season.recommend_count
    season_roster, missing = _season_scale_roster(roster, pool)

    rostered_names = {normalize_name(p.name) for p in roster}
    available = [bp for bp in pool.players if normalize_name(bp.name) not in rostered_names]

    # The baseline must be the actual best STARTING lineup's total, not a raw
    # sum of every rostered player — summing includes bench points that never
    # score, which inflates the baseline and makes every real upgrade look
    # like a downgrade. Same technique as draft._season_score.
    base_plan = optimize(season_roster, roster_positions, None, cfg)
    base_score = sum(p.projected_points or 0.0 for _, p in base_plan.assignments)

    # The drop candidate is chosen from the optimizer's OWN bench, ranked by
    # season value, never from `policy.droppable()` on the full roster
    # directly. Without live Yahoo data every player's `percent_owned` is
    # None, which is exactly what `droppable()` sorts on — so every player
    # ties, and the "worst" pick degenerates to whatever happens to be first
    # in list order. That produced a real, dangerous bug in testing: it
    # suggested dropping the starting QB to add a bench-caliber WR, purely
    # because he was first in roster.yml. Restricting the pool to
    # `base_plan.bench` makes that structurally impossible — a starter is,
    # by definition, not on it.
    # Pre-sorted by season value, worst first; `policy.droppable()` re-sorts
    # by percent_owned, but Python's sort is stable and every percent_owned
    # is None (0.0) without live Yahoo data, so that re-sort is a no-op tie
    # and this value order survives it. Not an accident — rely on it.
    bench_by_value = sorted(base_plan.bench, key=lambda p: (p.projected_points or 0.0))
    droppable_bench = policy.droppable(bench_by_value, cfg)
    fallback_drop = droppable_bench[0] if droppable_bench else None
    max_bid = policy.max_faab_bid(remaining_faab, cfg)

    scored: list[tuple[float, WaiverCandidate]] = []
    for bp in available[:150]:  # ROS VOR-ranked already; 150 covers every plausible claim
        candidate_player = Player(
            player_id=-1, name=bp.name, eligible_positions=[bp.position],
            selected_position=BENCH, team=bp.team, bye_week=bp.bye_week,
            projected_points=bp.points,
        )
        trial = season_roster + [candidate_player]
        trial_plan = optimize(trial, roster_positions, None, cfg)
        trial_score = sum(p.projected_points or 0.0 for _, p in trial_plan.assignments)
        gain = trial_score - base_score
        if gain <= 0.0:
            continue

        drop_reason = "worst droppable player" if fallback_drop else "no droppable player found — roster is full of protected players"

        scored.append((
            gain,
            WaiverCandidate(
                add_name=bp.name, position=bp.position, value=gain,
                drop_name=fallback_drop.name if fallback_drop else None, drop_reason=drop_reason,
                max_bid=max_bid, reason=f"+{gain:.1f} season pts over your current lineup",
            ),
        ))

    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:limit]], missing
