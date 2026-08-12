"""Cache-first fetch for LIVE weekly projection data — the in-season sibling
of `ffbot/history/fetch.py`, and deliberately not a reuse of it.

`ffbot/history/fetch.py`'s cache is correct because its sources are
immutable: a season that already happened never changes, so "a cache hit is
trusted and returned unread" is exactly right. A live projection is the
opposite — this week's numbers can move every day as practice reports and
injury news land, so trusting a stale cache file forever would mean the
in-season recommender silently runs on Tuesday's numbers all week.

`fetch_projection_week` adds exactly one axis on top of `history/fetch.py`'s
pattern: `ttl_minutes`. The CALLER decides it per call, the same way
`history/fetch.py`'s `refresh` flag is caller-driven rather than inferred —
this module has no NFL schedule knowledge and doesn't need any. A caller who
already knows a given week is over (e.g. summing weeks strictly before the
current one for a rest-of-season total) passes `ttl_minutes=None`, meaning
"never expire, treat like `history/fetch.py`'s default." A caller fetching
the current or a future week passes a real number (from
`ProjectionSourceConfig.cache_ttl_minutes`), and a stale file past that TTL
is refetched exactly like `history/fetch.py`'s `refresh=True`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_CACHE_DIR = Path("data/projections")

UrlOpener = Callable[[str], bytes]


class ProjectionFetchError(RuntimeError):
    """A live projection source could not be downloaded."""


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ffbot-projections/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - fixed https hosts only
        return resp.read()


def cache_path(source_key: str, season: int, week: int, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> Path:
    return Path(cache_dir) / f"{source_key}_{season}_wk{week:02d}.json"


def _meta_path(dest: Path) -> Path:
    """Sidecar file recording when `dest` was actually fetched.

    Deliberately not `dest.stat().st_mtime`: a caller testing TTL behavior
    passes a synthetic `now`, and comparing that against the OS's real wall
    clock would make the test's `now` meaningless. Stamping the fetch time
    ourselves (using the same clock — real or injected — as the freshness
    check) keeps the whole comparison self-consistent and the real content
    file untouched, since callers `json.loads` it directly.
    """
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


def fetch_projection_week(
    source_key: str,
    url: str,
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = None,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
) -> Path:
    """Download `url` (one source's projections for `season`/`week`) to the
    cache, returning the local path.

    A cache hit younger than `ttl_minutes` is trusted and returned unread;
    `ttl_minutes=None` trusts any existing cache hit unconditionally (the
    permanent-cache case for a week the caller already knows is final).
    Raises `ProjectionFetchError` on a network failure — callers must decide
    what "no live data" means for them (see `ffbot/projections/__init__.py`'s
    fallback-to-board contract), this module never degrades silently.
    """
    resolved_now = now if now is not None else time.time()
    dest = cache_path(source_key, season, week, cache_dir)
    if dest.exists() and not _is_stale(dest, ttl_minutes, resolved_now):
        return dest

    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise ProjectionFetchError(f"{source_key} ({url}): {exc}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    _meta_path(dest).write_text(repr(resolved_now), encoding="utf-8")
    return dest


def fetch_projection_json(
    source_key: str,
    url: str,
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    ttl_minutes: float | None = None,
    opener: UrlOpener = _default_opener,
    now: float | None = None,
):
    path = fetch_projection_week(source_key, url, season, week, cache_dir, ttl_minutes, opener, now)
    return json.loads(path.read_text(encoding="utf-8"))
