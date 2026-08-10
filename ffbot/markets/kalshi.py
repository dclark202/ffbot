"""Kalshi public market-data client — the live-only seam for a future
betting-market spice signal (Idea 1B in the signal-scoping pass this module
came out of).

Reading market data needs NO authentication — verified live during scoping:
a plain unauthenticated GET against `https://api.elections.kalshi.com/
trade-api/v2` returns real markets, including live NFL player-prop events
(e.g. `KXNFLTD-<date><AWAY><HOME>`, anytime-touchdown markets per player).
Only placing orders, checking a balance, or anything else account-scoped
needs the signed-header auth flow, none of which this module touches.

Why this stays UNWIRED from any decision path: Kalshi's NFL PLAYER PROP
markets launched September 2025 (confirmed during scoping via press
coverage), so there is ZERO overlap with this repo's 2021-2024 ECR-clean
backtest window — nothing prop-derived can be graded by the existing
harness today, the same discipline that keeps `ffbot.history.projections
.ecr_projections` from ever fitting on the season it grades. Folding a prop
line into a real projection also means blending it inside
`ffbot.roster_source`/`ffbot.history.projections` — a materially larger
change than adding a spice dial, and not one to make without evidence.

The honest next step (see docs/BACKTEST.md) is prospective, not
retrospective: once the season is underway, log this client's
market-implied read alongside the shipped projection every week, then grade
it against `ffbot.history.actuals.week_actuals` at season end. That is real
evidence in about seventeen weeks, at zero cost before then.

Live data, not immutable history — none of `ffbot.history.fetch`'s "cache
forever, never re-fetch" contract applies here. Every function takes an
injectable `opener` (the same `UrlOpener` convention `ffbot.history.fetch`
uses) purely for testability; nothing here caches to disk.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from ..history.fetch import UrlOpener

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiError(RuntimeError):
    """A Kalshi API call failed."""


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ffbot-markets/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - fixed https host only
        return resp.read()


def _get(path: str, params: dict, opener: UrlOpener) -> dict:
    query = {k: v for k, v in params.items() if v is not None}
    url = f"{_BASE_URL}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise KalshiError(f"kalshi ({url}): {exc}") from exc
    return json.loads(raw)


def list_series(category: Optional[str] = "Sports", opener: UrlOpener = _default_opener) -> list[dict]:
    """Every series Kalshi lists, optionally filtered by `category` (e.g.
    `"Sports"`). A series is a recurring market family — `KXNFLTD`
    ("Pro Football Touchdowns"), `KXNFLPASSINT`
    ("Pro Football Passing Interceptions") — not a single game."""
    data = _get("/series", {"category": category}, opener)
    return data.get("series", [])


def list_events(
    series_ticker: str,
    status: Optional[str] = None,
    limit: int = 50,
    opener: UrlOpener = _default_opener,
) -> list[dict]:
    """Events (one per real-world game/occasion) under `series_ticker`.
    `status`: `"open"`, `"closed"`, `"settled"`, or `None` for all."""
    data = _get("/events", {"series_ticker": series_ticker, "status": status, "limit": limit}, opener)
    return data.get("events", [])


def list_markets(
    event_ticker: Optional[str] = None,
    series_ticker: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    opener: UrlOpener = _default_opener,
) -> list[dict]:
    """Individual tradeable markets (one per player, for a prop event —
    e.g. one `event_ticker` for "DET vs CIN" carries a separate market per
    player's "1+ touchdowns" contract) under an event or series."""
    data = _get(
        "/markets",
        {"event_ticker": event_ticker, "series_ticker": series_ticker, "status": status, "limit": limit},
        opener,
    )
    return data.get("markets", [])


def get_market(ticker: str, opener: UrlOpener = _default_opener) -> dict:
    """One market's current state (bid/ask, volume, status) by its exact ticker."""
    data = _get(f"/markets/{ticker}", {}, opener)
    return data.get("market", {})


def get_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 60,
    opener: UrlOpener = _default_opener,
) -> list[dict]:
    """OHLC price history for one market between `start_ts`/`end_ts` (Unix
    seconds). `period_interval` in minutes: 1, 60, or 1440 (Kalshi's own
    three supported granularities). Works on settled/closed markets too,
    within Kalshi's own historical-cutoff limits (see Kalshi's docs)."""
    data = _get(
        f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        opener,
    )
    return data.get("candlesticks", [])
