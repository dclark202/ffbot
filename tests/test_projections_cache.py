from __future__ import annotations

import json

import pytest

from ffbot.projections.cache import (
    ProjectionFetchError,
    cache_path,
    fetch_projection_json,
    fetch_projection_week,
)


def _opener(calls: list, payload: bytes = b'{"ok": true}'):
    def opener(url: str) -> bytes:
        calls.append(url)
        return payload

    return opener


def _raising_opener(url: str) -> bytes:
    raise OSError("network unreachable")


class TestCachePath:
    def test_path_includes_source_season_and_zero_padded_week(self, tmp_path):
        p = cache_path("sleeper", 2026, 6, cache_dir=tmp_path)
        assert p == tmp_path / "sleeper_2026_wk06.json"


class TestFetchProjectionWeek:
    def test_downloads_and_caches(self, tmp_path):
        calls: list[str] = []
        dest = fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, opener=_opener(calls))
        assert dest.exists()
        assert calls == ["https://example/x"]

    def test_cache_hit_with_no_ttl_never_refetches(self, tmp_path):
        calls: list[str] = []
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=None, opener=_opener(calls))
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=None, opener=_opener(calls))
        assert len(calls) == 1

    def test_cache_hit_within_ttl_is_reused(self, tmp_path):
        calls: list[str] = []
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0)
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0 + 30 * 60)
        assert len(calls) == 1

    def test_cache_hit_past_ttl_is_refetched(self, tmp_path):
        calls: list[str] = []
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0)
        fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0 + 61 * 60)
        assert len(calls) == 2

    def test_different_weeks_cache_independently(self, tmp_path):
        calls: list[str] = []
        fetch_projection_week("sleeper", "https://example/wk6", 2026, 6, cache_dir=tmp_path, opener=_opener(calls))
        fetch_projection_week("sleeper", "https://example/wk7", 2026, 7, cache_dir=tmp_path, opener=_opener(calls))
        assert len(calls) == 2

    def test_network_failure_raises_projection_fetch_error(self, tmp_path):
        with pytest.raises(ProjectionFetchError):
            fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, opener=_raising_opener)

    def test_network_failure_does_not_leave_a_partial_cache_file(self, tmp_path):
        with pytest.raises(ProjectionFetchError):
            fetch_projection_week("sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path, opener=_raising_opener)
        assert not cache_path("sleeper", 2026, 6, cache_dir=tmp_path).exists()


class TestFetchProjectionJson:
    def test_parses_cached_json(self, tmp_path):
        calls: list[str] = []
        data = fetch_projection_json(
            "sleeper", "https://example/x", 2026, 6, cache_dir=tmp_path,
            opener=_opener(calls, payload=json.dumps({"a": 1}).encode("utf-8")),
        )
        assert data == {"a": 1}
