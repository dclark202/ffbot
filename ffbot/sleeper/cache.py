"""Cache-first fetch for Sleeper HTTP responses — the league-client sibling
of `ffbot/projections/cache.py`, and deliberately the same shape as that
module rather than `ffbot/history/fetch.py`'s immutable-source cache.

`ffbot/history/fetch.py`'s cache is correct because its sources are
immutable (a season that already happened never changes). Sleeper's league
state is the opposite — rosters, matchups, and injury status can all change
within a day, so "a cache hit is trusted forever" would mean the tool runs on
stale data all week. Every call here takes a caller-supplied `ttl_minutes`,
same contract as `ffbot/projections/cache.py`: `None` means "never expire"
(the right answer for something that cannot change, like a completed draft's
picks), a real number means "stale past this many minutes, refetch."

Deliberately NOT a reuse of `ffbot/projections/cache.py` itself, even though
the mechanism is identical: that module's cache key is a fixed
`(source_key, season, week)` shape specific to one projection per week, while
a league client needs an arbitrary, endpoint-specific key (a league id, a
week number, "the full players dump", a draft id with no season at all).
Generalizing that module's signature to fit would make it a worse fit for
its own one job. Two small modules, one shared idea.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_CACHE_DIR = Path("data/sleeper")

UrlOpener = Callable[[str], bytes]


class SleeperFetchError(RuntimeError):
    """A Sleeper endpoint could not be downloaded."""


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ffbot-sleeper/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - fixed https hosts only
        return resp.read()


def cache_path(cache_key: str, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> Path:
    """`cache_key` is caller-assigned and already encodes whatever varies for
    that endpoint (e.g. `"league_289646328504385536"`, `"matchups_.../wk03"`,
    `"players_nfl"`) — this module has no opinion on that shape."""
    return Path(cache_dir) / f"{cache_key}.json"


def _meta_path(dest: Path) -> Path:
    """Sidecar recording when `dest` was actually fetched — see
    `ffbot/projections/cache.py`'s identical helper for why this is stamped
    ourselves rather than read from `dest.stat().st_mtime` (a test's
    synthetic `now` must compare against a value on the same clock)."""
    return dest.with_suffix(dest.suffix + ".meta")


def _is_stale(dest: Path, ttl_minutes: float | None, now: float) -> bool:
    if ttl_minutes is None:
        return False
    meta = _meta_path(dest)
    if not meta.exists():
        return True  # no fetch-time record -- treat as stale rather than trust blindly
    try:
        fetched_at = float(meta.read_text(encoding="utf-8"))
    except ValueError:
        return True
    age_minutes = (now - fetched_at) / 60.0
    return age_minutes > ttl_minutes


def fetch_cached(
    cache_key: str,
    url: str,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = None,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
) -> Path:
    """Download `url` to the cache under `cache_key`, returning the local
    path. A cache hit younger than `ttl_minutes` is trusted and returned
    unread; `ttl_minutes=None` trusts any existing hit unconditionally.
    `ttl_minutes=0` (or a caller that never writes a cache file — see
    `fetch_uncached`) forces a fresh fetch every call, for data that must
    never be stale (live draft picks).

    Raises `SleeperFetchError` on a network failure — callers decide what "no
    live data" means for them, same contract as
    `ffbot.projections.cache.fetch_projection_week`.
    """
    resolved_now = now if now is not None else time.time()
    dest = cache_path(cache_key, cache_dir)
    if dest.exists() and not _is_stale(dest, ttl_minutes, resolved_now):
        return dest

    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise SleeperFetchError(f"{cache_key} ({url}): {exc}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    _meta_path(dest).write_text(repr(resolved_now), encoding="utf-8")
    return dest


def fetch_json(
    cache_key: str,
    url: str,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = None,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
):
    path = fetch_cached(cache_key, url, cache_dir, ttl_minutes, opener, now)
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_uncached_json(url: str, opener: UrlOpener = _default_opener):
    """No cache file at all, ever — for live draft picks, where a stale read
    is worse than the cost of a fresh HTTP call every poll. Still raises
    `SleeperFetchError` on a network failure, same as the cached path."""
    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise SleeperFetchError(f"{url}: {exc}") from exc
    return json.loads(raw)
