"""Open-Meteo GAME-level weather enrichment for historical replay.

Two structural weather gaps are flagged in docs/BACKTEST.md's leakage
register before this module exists: `games.csv`'s `wind` column is blank on
roughly a fifth of outdoor games (missing data, not confirmed calm — see
`ffbot.week.weather_severity`'s own docstring on this), and nflverse's
`games.csv` carries NO precipitation field at all, so the precipitation half
of `weather_severity` has been structurally inert in every backtest ever
run — wind was doing 100% of the work. Neither gap is fixable from data
already cached; both need an independent weather source.

Open-Meteo's historical archive (`https://archive-api.open-meteo.com`,
verified live during scoping: free, no API key or signup, ~10k requests/day,
hourly data back to 1940, global) is queried per `(lat, lon, date)` using
`data/stadiums.yml`'s existing `lat`/`lon` columns — present for every
outdoor team and neutral-site venue, loaded by `week.load_stadiums` but
never read by any code before this module. One HTTP call covers a stadium's
ENTIRE day (24 hourly values), cached to disk and never re-fetched — a past
date's weather is exactly as immutable as `ffbot.history.fetch`'s own
"never re-fetch a completed season" contract. Every game at that
stadium/date shares the one cached response.

This is a GAME-level provider, not a `ffbot.history.signals.SignalProvider`
(that seam is deliberately player-level — see its own module docstring).
`WeekSnapshot.with_game_weather()` (`ffbot/history/index.py`) is the sibling
merge point, following the identical "outside as_of(), never inside it"
precedent `with_signals` established: weather is observed rather than
results-bearing, so the leakage argument for staying outside `as_of()` is
weaker here than for `historical_form`/`usage_form`, but keeping every
enrichment source on one uniform seam (rather than special-casing which
ones get to live inside the `len(calls) == 2` boundary) is worth more than
the marginal simplicity of folding this one in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Protocol

from ..config import Config
from ..week import load_stadiums
from .fetch import DEFAULT_CACHE_DIR, FetchError, UrlOpener, _default_opener, fetch_rows

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_HOURLY_VARS = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,snowfall"

# Roof strings from games.csv that mean "weather cannot touch this game" --
# same set week.py's own historical adapter (`index._ROOF_DOME`) uses.
_ROOF_DOME = {"dome", "closed"}


class GameProvider(Protocol):
    """`(season, week, cfg, cache_dir, opener) -> {team: {field: value}}`,
    the game-level analog of `ffbot.history.signals.SignalProvider`. Fields
    match `week.GameInfo`'s enrichment attributes: `wind_mph`,
    `wind_gust_mph`, `temp_f`, `precip_mm`."""

    def __call__(
        self,
        season: int,
        week: int,
        cfg: Config,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        opener: UrlOpener = _default_opener,
    ) -> dict[str, dict[str, float]]: ...


def _cache_path(cache_dir: Path, lat: float, lon: float, date: str) -> Path:
    return Path(cache_dir) / "openmeteo" / f"{lat:.4f}_{lon:.4f}_{date}.json"


def _day_weather(lat: float, lon: float, date: str, cache_dir: Path, opener: UrlOpener) -> dict:
    """One stadium/date's full 24-hour series, cache-first. A cache hit is
    trusted and returned unread — a past date's weather never changes,
    the identical reasoning `ffbot.history.fetch.fetch`'s own docstring
    gives for nflverse's season files.
    """
    dest = _cache_path(Path(cache_dir), lat, lon, date)
    if dest.exists():
        return json.loads(dest.read_text(encoding="utf-8"))

    url = (
        f"{_ARCHIVE_URL}?latitude={lat}&longitude={lon}&start_date={date}&end_date={date}"
        f"&hourly={_HOURLY_VARS}&temperature_unit=fahrenheit&wind_speed_unit=mph"
        "&precipitation_unit=mm&timezone=America%2FNew_York"
    )
    try:
        raw = opener(url)
    except Exception as exc:  # noqa: BLE001 -- any transport failure degrades this one game, see caller
        raise FetchError(f"open-meteo ({url}): {exc}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return json.loads(raw)


def _hour_index(times: list, target: str) -> Optional[int]:
    return times.index(target) if target in times else None


def open_meteo_game_weather(
    season: int,
    week: int,
    cfg: Config,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[str, dict[str, float]]:
    """`{team: {"wind_mph", "wind_gust_mph", "temp_f", "precip_mm"}}` for
    every OUTDOOR game this week — dome/closed-roof games are skipped
    entirely (weather-neutral by `week.is_dome_game`'s own definition, and
    an enclosed stadium's interior has nothing to fetch). Both the home and
    away team key to the SAME game's reading — the away team plays in the
    home stadium's actual conditions, never its own city's.

    A game degrading to no entry (missing `lat`/`lon` in
    `data/stadiums.yml`, a malformed `gameday`/`gametime`, a fetch that
    raised) never affects any OTHER game this week — same "a data gap is
    not evidence of bad weather" fail-open convention `week.is_dome_game`
    already documents, applied per-game rather than per-week.

    `cfg` is accepted for `GameProvider` signature parity with
    `ffbot.history.signals.SignalProvider` but unused — weather needs no
    league scoring rules.
    """
    cache_dir = Path(cache_dir)
    stadiums = load_stadiums()  # data/stadiums.yml: team -> StadiumInfo(dome, lat, lon)

    game_rows = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    out: dict[str, dict[str, float]] = {}
    for row in game_rows:
        try:
            if int(row.get("season", -1)) != season or int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue

        if (row.get("roof") or "").strip().lower() in _ROOF_DOME:
            continue

        home, away = row.get("home_team"), row.get("away_team")
        gameday, gametime = row.get("gameday"), row.get("gametime")
        if not home or not away or not gameday or not gametime:
            continue

        info = stadiums.get(home)
        if info is None or info.lat is None or info.lon is None:
            continue

        try:
            day = _day_weather(info.lat, info.lon, gameday, cache_dir, opener)
        except FetchError:
            continue

        hourly = day.get("hourly", {})
        idx = _hour_index(hourly.get("time", []), f"{gameday}T{gametime}")
        if idx is None:
            continue

        def _at(field: str) -> Optional[float]:
            vals = hourly.get(field, [])
            return vals[idx] if idx < len(vals) and vals[idx] is not None else None

        entry = {
            "wind_mph": _at("wind_speed_10m"),
            "wind_gust_mph": _at("wind_gusts_10m"),
            "temp_f": _at("temperature_2m"),
            "precip_mm": _at("precipitation"),
        }
        entry = {k: v for k, v in entry.items() if v is not None}
        if not entry:
            continue
        out[home] = entry
        out[away] = entry

    return out
