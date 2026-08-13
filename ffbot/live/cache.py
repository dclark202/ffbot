"""TTL-sidecar cache for live game-conditions fetches (Open-Meteo forecasts,
Kalshi market reads) — the same mechanism `ffbot/sleeper/cache.py` and
`ffbot/projections/cache.py` already use, deliberately NOT reused directly
from either: both of those modules' error types name their own source
(`SleeperFetchError`, `ProjectionFetchError`), which would be a wrong label
on a weather or market fetch, and this package's cache keys (lat/lon/date,
a Kalshi market ticker) don't fit either module's existing key shape any
more naturally than theirs fit each other's (see `ffbot/sleeper/cache.py`'s
own docstring on why it isn't a reuse of `ffbot/projections/cache.py`
either — same reasoning, third instance).

A forecast or a market price is neither immutable (unlike
`ffbot.history.fetch`'s completed seasons) nor as fast-moving as a live
draft pick (`ffbot.sleeper.cache.fetch_uncached_json`) — it genuinely goes
stale on the order of hours, which is what `ttl_minutes` is for.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_CACHE_DIR = Path("data/live")

UrlOpener = Callable[[str], bytes]


class LiveFetchError(RuntimeError):
    """A live game-conditions source (weather or market) could not be fetched."""


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ffbot-live/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - fixed https hosts only
        return resp.read()


def cache_path(cache_key: str, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> Path:
    return Path(cache_dir) / f"{cache_key}.json"


def _meta_path(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + ".meta")


def _is_stale(dest: Path, ttl_minutes: float | None, now: float) -> bool:
    if ttl_minutes is None:
        return False
    meta = _meta_path(dest)
    if not meta.exists():
        return True
    try:
        fetched_at = float(meta.read_text(encoding="utf-8"))
    except ValueError:
        return True
    return (now - fetched_at) / 60.0 > ttl_minutes


def fetch_cached(
    cache_key: str,
    url: str,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = 180.0,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
) -> Path:
    """Download `url` to the cache under `cache_key`, returning the local
    path. A cache hit younger than `ttl_minutes` is trusted and returned
    unread. Raises `LiveFetchError` on a network failure — callers decide
    the fallback, same contract every live seam in this repo follows.
    """
    resolved_now = now if now is not None else time.time()
    dest = cache_path(cache_key, cache_dir)
    if dest.exists() and not _is_stale(dest, ttl_minutes, resolved_now):
        return dest

    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise LiveFetchError(f"{cache_key} ({url}): {exc}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    _meta_path(dest).write_text(repr(resolved_now), encoding="utf-8")
    return dest


def fetch_json(
    cache_key: str,
    url: str,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = 180.0,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
):
    path = fetch_cached(cache_key, url, cache_dir, ttl_minutes, opener, now)
    return json.loads(path.read_text(encoding="utf-8"))
