"""Assembles this week's auto-fetched game conditions (schedule + weather +
market odds) into `{team: GameInfo}` and merges them with whatever
`weekly/week-NN.yml` states, field by field: research wins for kickoff, venue
and Vegas totals, the live forecast wins for weather (see
`merge_conditions`). This is the unattended-run analog of `/gameday`'s
research step: what fills a `games:` block when nobody ran it first.

Each of the three sources (schedule/weather/odds) degrades independently —
one source failing never blocks another, and a wholesale failure here
degrades to "no auto-fetched conditions this run," never a crash. See
CLAUDE.md's live-seam contract: every fetch failure is surfaced as an
alert, never a silent success and never a crash.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import Optional

from ..config import GameConditionsConfig
from ..week import GameInfo, WeeklyIntel
from . import schedule as schedule_module
from . import weather as weather_module
from .schedule import ScheduleError


def fetch_conditions(
    season: int,
    week: int,
    cfg: GameConditionsConfig,
    cache_dir: Optional[str] = None,
    opener=None,
    kalshi_opener=None,
) -> tuple[dict[str, GameInfo], list[str]]:
    """`({team: GameInfo}, alerts)` — auto-fetched conditions for
    `(season, week)`, gated by `cfg.weather_source`/`cfg.odds_source`.
    Never raises: a source that's off, or that fails, degrades to "no data
    from that source," appended to `alerts` rather than propagated.
    `opener`/`kalshi_opener` are separate injection points because the two
    sources hit different hosts (Open-Meteo vs. Kalshi) and a test faking
    one must never accidentally also stand in for the other.
    """
    alerts: list[str] = []
    schedule_kwargs = {}
    if cache_dir or cfg.cache_dir:
        schedule_kwargs["cache_dir"] = cache_dir or cfg.cache_dir
    if opener is not None:
        schedule_kwargs["opener"] = opener

    try:
        games = schedule_module.this_week_games(season, week, **schedule_kwargs)
    except ScheduleError as exc:
        alerts.append(f"live schedule fetch failed ({exc}) — no auto-fetched conditions this run")
        return {}, alerts
    if not games:
        return {}, alerts

    weather: dict[str, dict[str, float]] = {}
    if cfg.weather_source == "open_meteo":
        try:
            weather_kwargs = dict(schedule_kwargs)
            weather_kwargs["ttl_minutes"] = cfg.cache_ttl_minutes
            weather = weather_module.forecast_weather(games, **weather_kwargs)
        except Exception as exc:  # noqa: BLE001 -- a weather failure must never block odds or the schedule already fetched
            alerts.append(f"live weather fetch failed ({exc}) — no auto-fetched weather this run")

    odds: dict[str, dict[str, float]] = {}
    if cfg.odds_source == "kalshi":
        from ..markets import kalshi_nfl  # local import: keeps ffbot.live importable with no network at module level
        try:
            odds_kwargs = {}
            if kalshi_opener is not None:
                odds_kwargs["opener"] = kalshi_opener
            odds = kalshi_nfl.game_odds(games, series=cfg.kalshi_odds_series, **odds_kwargs)
        except Exception as exc:  # noqa: BLE001 -- see weather note above
            alerts.append(f"live odds fetch failed ({exc}) — no auto-fetched odds this run")

    out: dict[str, GameInfo] = {}
    for team, game in games.items():
        w = weather.get(team, {})
        o = odds.get(team, {})
        out[team] = GameInfo(
            opponent=game.opponent,
            kickoff_et=game.kickoff.strftime("%Y-%m-%dT%H:%M") if game.kickoff else "",
            home=game.home,
            wind_mph=w.get("wind_mph"),
            precip_pct=w.get("precip_pct"),
            team_total=o.get("team_total"),
            opp_total=o.get("opp_total"),
            temp_f=w.get("temp_f"),
            wind_gust_mph=w.get("wind_gust_mph"),
            precip_mm=w.get("precip_mm"),
        )
    return out, alerts


WEATHER_FIELDS = ("wind_mph", "precip_pct", "wind_gust_mph", "temp_f", "precip_mm")

# Display thresholds for the disagreement alert only -- the merge itself uses
# the forecast whenever it has one, at any size of disagreement.
WIND_DISAGREE_MPH = 10.0
PRECIP_DISAGREE_PCT = 40.0


def merge_conditions(intel: WeeklyIntel, auto: dict[str, GameInfo]) -> tuple[WeeklyIntel, list[str]]:
    """`(intel with auto merged in, alerts)`, merged field by field.

    Weather (`WEATHER_FIELDS`): the live forecast wins whenever it has a
    value, and a researched number only fills a gap (a failed fetch, a
    stadium with no coordinates). Everything else -- kickoff, venue, Vegas
    totals -- the researched value wins and the auto-fetched one fills a gap.

    Weather is the exception because research reads prose about weather and
    turns it into a number, and the forecast IS the number. On 2026-09-13
    research wrote JAX-CLE as `wind_mph: 40, precip_pct: 40` from a note about
    storm gusts "near the I-95 corridor"; Open-Meteo had about 6 mph and no
    rain. Under the old whole-entry precedence the researched guess replaced
    the forecast, cut Trevor Lawrence 21%, and pushed "bench Lawrence" -- he
    scored 26. A large disagreement is surfaced as an alert so the human sees
    research was overruled.

    Dome games get no forecast, so a researched wind can fill in there, but
    `week.weather_multiplier` is exactly 1.0 in a dome regardless.
    """
    alerts: list[str] = []
    merged: dict[str, GameInfo] = {}
    for team in sorted(set(auto) | set(intel.games)):
        hand = intel.games.get(team)
        fetched = auto.get(team)
        if hand is None or fetched is None:
            merged[team] = hand if hand is not None else fetched
            continue
        updates = {}
        for f in dataclasses.fields(GameInfo):
            h, a = getattr(hand, f.name), getattr(fetched, f.name)
            if f.name in WEATHER_FIELDS:
                if a is not None:
                    updates[f.name] = a
            elif h in (None, ""):
                updates[f.name] = a
        merged[team] = replace(hand, **updates)
        alerts.extend(_disagreements(team, hand, fetched))
    return replace(intel, games=merged), alerts


def _disagreements(team: str, hand: GameInfo, fetched: GameInfo) -> list[str]:
    out = []
    if hand.wind_mph is not None and fetched.wind_mph is not None and abs(hand.wind_mph - fetched.wind_mph) >= WIND_DISAGREE_MPH:
        out.append(
            f"{team}: research said wind {hand.wind_mph:.0f} mph, forecast {fetched.wind_mph:.0f} mph — using the forecast"
        )
    if (
        hand.precip_pct is not None
        and fetched.precip_pct is not None
        and abs(hand.precip_pct - fetched.precip_pct) >= PRECIP_DISAGREE_PCT
    ):
        out.append(
            f"{team}: research said {hand.precip_pct:.0f}% rain, forecast {fetched.precip_pct:.0f}% — using the forecast"
        )
    return out
