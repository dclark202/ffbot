"""Open-Meteo FORECAST weather for this week's live games — the current-week
sibling of `ffbot.history.openmeteo`'s archive-API historical enrichment.

Same provider family (`open-meteo.com`), same free/no-key/no-signup
contract, same `data/stadiums.yml` lat/lon lookup, same one-call-per-
stadium-per-day caching idea — but a genuinely different endpoint
(`api.open-meteo.com/v1/forecast`, not `archive-api...`), because a future
date has no archived observation yet, and it needs a genuinely different
field: `precipitation_probability` (a 0-100% forecast, what
`week.weather_severity`'s `precip_pct` actually wants) rather than
`precipitation` (an accumulated-mm OBSERVATION, all the archive API can
ever report for a date that already happened). The historical module
correctly leaves `GameInfo.precip_pct` at `None` forever, by its own
docstring's admission — this module is the fix for that gap on the live
path specifically, not a copy-paste of a module that can't supply it.

Unlike `ffbot.history.openmeteo`'s permanent per-day cache (a past day's
weather never changes), a forecast genuinely goes stale — cached here via
`ffbot.live.cache`'s TTL sidecar instead of a forever-trusted cache file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..week import load_stadiums
from .cache import DEFAULT_CACHE_DIR, LiveFetchError, UrlOpener, _default_opener, fetch_cached
from .schedule import LiveGame

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HOURLY_VARS = "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m"


def _cache_key(lat: float, lon: float, date: str) -> str:
    return f"openmeteo_forecast_{lat:.4f}_{lon:.4f}_{date}"


def _day_forecast(
    lat: float, lon: float, date: str, cache_dir: Path, ttl_minutes: float, opener: UrlOpener, now: Optional[float],
) -> dict:
    url = (
        f"{_FORECAST_URL}?latitude={lat}&longitude={lon}&start_date={date}&end_date={date}"
        f"&hourly={_HOURLY_VARS}&temperature_unit=fahrenheit&wind_speed_unit=mph"
        "&precipitation_unit=mm&timezone=America%2FNew_York"
    )
    path = fetch_cached(_cache_key(lat, lon, date), url, cache_dir, ttl_minutes, opener, now)
    return json.loads(path.read_text(encoding="utf-8"))


def _hour_index(times: list, target: str) -> Optional[int]:
    return times.index(target) if target in times else None


def _nearest_hour(dt: datetime) -> datetime:
    """Open-Meteo's hourly series is stamped on the hour only
    (`"2026-08-13T20:00"`) -- a real kickoff almost never is (`20:20`,
    `16:25`, `19:15`...), so an exact-string lookup against the raw kickoff
    time would silently miss nearly every game. Round to the nearest hour
    instead; the difference is well inside weather's own hour-to-hour
    noise. Returns a full `datetime` (not just a formatted string) so a
    late-night kickoff that rounds past midnight still resolves to the
    RIGHT calendar date for the day-level forecast fetch, not the kickoff's
    own date."""
    rounded = dt.replace(minute=0, second=0, microsecond=0)
    if dt.minute >= 30:
        rounded += timedelta(hours=1)
    return rounded


def forecast_weather(
    games: dict[str, LiveGame],
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float = 180.0,
    opener: UrlOpener = _default_opener,
    now: Optional[float] = None,
) -> dict[str, dict[str, float]]:
    """`{team: {"wind_mph", "precip_pct", "wind_gust_mph", "temp_f",
    "precip_mm"}}` for every OUTDOOR game in `games` (from
    `ffbot.live.schedule.this_week_games`) — dome/closed-roof games are
    skipped entirely, same reasoning `ffbot.history.openmeteo` uses (a
    stadium interior has nothing to fetch, and the multiplier is a no-op
    there regardless). Both teams in a game key to the SAME reading — the
    away team plays in the home stadium's actual forecast, never its own
    city's.

    A single game degrading to no entry (missing `lat`/`lon` in
    `data/stadiums.yml`, no kickoff time, a fetch failure) never affects any
    OTHER game — same fail-open-per-game convention `ffbot.history.openmeteo`
    already documents. Never raises; a wholesale failure (e.g. `load_
    stadiums()` finding nothing) just yields an empty dict, same contract
    `ffbot.live.conditions` expects from every provider it merges.
    """
    cache_dir = Path(cache_dir)
    stadiums = load_stadiums()  # data/stadiums.yml: team -> StadiumInfo(dome, lat, lon)

    out: dict[str, dict[str, float]] = {}
    seen_homes: set[str] = set()
    for team, game in games.items():
        if not game.home or game.is_dome or game.kickoff is None:
            continue
        if team in seen_homes:
            continue
        seen_homes.add(team)

        info = stadiums.get(team)
        if info is None or info.lat is None or info.lon is None:
            continue

        rounded_kickoff = _nearest_hour(game.kickoff)
        date = rounded_kickoff.strftime("%Y-%m-%d")
        try:
            day = _day_forecast(info.lat, info.lon, date, cache_dir, ttl_minutes, opener, now)
        except LiveFetchError:
            continue

        hourly = day.get("hourly", {})
        target = rounded_kickoff.strftime("%Y-%m-%dT%H:%M")
        idx = _hour_index(hourly.get("time", []), target)
        if idx is None:
            continue

        def _at(field: str) -> Optional[float]:
            vals = hourly.get(field, [])
            return vals[idx] if idx < len(vals) and vals[idx] is not None else None

        entry = {
            "wind_mph": _at("wind_speed_10m"),
            "precip_pct": _at("precipitation_probability"),
            "wind_gust_mph": _at("wind_gusts_10m"),
            "temp_f": _at("temperature_2m"),
            "precip_mm": _at("precipitation"),
        }
        entry = {k: v for k, v in entry.items() if v is not None}
        if not entry:
            continue
        out[team] = entry
        out[game.opponent] = entry

    return out
