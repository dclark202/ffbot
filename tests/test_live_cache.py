from __future__ import annotations

import json

import pytest

from ffbot.live.cache import LiveFetchError, cache_path, fetch_cached, fetch_json


def _opener(calls: list, payload: bytes = b'{"ok": true}'):
    def opener(url: str) -> bytes:
        calls.append(url)
        return payload

    return opener


def _raising_opener(url: str) -> bytes:
    raise OSError("network unreachable")


class TestCachePath:
    def test_path_is_key_dot_json(self, tmp_path):
        assert cache_path("openmeteo_forecast_1_2_2026-09-09", cache_dir=tmp_path) == (
            tmp_path / "openmeteo_forecast_1_2_2026-09-09.json"
        )


class TestFetchCached:
    def test_downloads_and_caches(self, tmp_path):
        calls: list[str] = []
        dest = fetch_cached("k", "https://example/x", cache_dir=tmp_path, opener=_opener(calls))
        assert dest.exists()
        assert calls == ["https://example/x"]

    def test_cache_hit_within_ttl_is_reused(self, tmp_path):
        calls: list[str] = []
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0)
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0 + 30 * 60)
        assert len(calls) == 1

    def test_cache_hit_past_ttl_is_refetched(self, tmp_path):
        calls: list[str] = []
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0)
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=60, opener=_opener(calls), now=1000.0 + 61 * 60)
        assert len(calls) == 2

    def test_cache_hit_with_no_ttl_never_refetches(self, tmp_path):
        calls: list[str] = []
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=None, opener=_opener(calls))
        fetch_cached("k", "https://example/x", cache_dir=tmp_path, ttl_minutes=None, opener=_opener(calls))
        assert len(calls) == 1

    def test_different_keys_cache_independently(self, tmp_path):
        calls: list[str] = []
        fetch_cached("a", "https://example/a", cache_dir=tmp_path, opener=_opener(calls))
        fetch_cached("b", "https://example/b", cache_dir=tmp_path, opener=_opener(calls))
        assert len(calls) == 2

    def test_network_failure_raises_live_fetch_error(self, tmp_path):
        with pytest.raises(LiveFetchError):
            fetch_cached("k", "https://example/x", cache_dir=tmp_path, opener=_raising_opener)

    def test_network_failure_does_not_leave_a_partial_cache_file(self, tmp_path):
        with pytest.raises(LiveFetchError):
            fetch_cached("k", "https://example/x", cache_dir=tmp_path, opener=_raising_opener)
        assert not cache_path("k", cache_dir=tmp_path).exists()


class TestFetchJson:
    def test_parses_cached_json(self, tmp_path):
        calls: list[str] = []
        data = fetch_json(
            "k", "https://example/x", cache_dir=tmp_path,
            opener=_opener(calls, payload=json.dumps({"a": 1}).encode("utf-8")),
        )
        assert data == {"a": 1}

    def test_network_failure_raises_live_fetch_error(self, tmp_path):
        with pytest.raises(LiveFetchError):
            fetch_json("k", "https://example/x", cache_dir=tmp_path, opener=_raising_opener)
