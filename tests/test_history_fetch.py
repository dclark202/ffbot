from __future__ import annotations

import gzip

import pytest

from ffbot.history.fetch import (
    SOURCES,
    FetchError,
    cache_path,
    coverage_table,
    fetch,
    fetch_rows,
    load_csv_rows,
    parse_seasons,
    source_url,
)


class TestParseSeasons:
    def test_range(self):
        assert parse_seasons("2015-2018") == [2015, 2016, 2017, 2018]

    def test_commas_and_ranges_mixed(self):
        assert parse_seasons("2019,2021-2023,2025") == [2019, 2021, 2022, 2023, 2025]

    def test_dedupes_and_sorts(self):
        assert parse_seasons("2020,2018-2020") == [2018, 2019, 2020]

    def test_blank_segments_ignored(self):
        assert parse_seasons("2020,,2021") == [2020, 2021]


class TestCachePathAndUrl:
    def test_per_season_cache_path(self, tmp_path):
        p = cache_path("stats_player_week", 2023, cache_dir=tmp_path)
        assert p == tmp_path / "stats_player_week_2023.csv"

    def test_non_per_season_cache_path_ignores_season(self, tmp_path):
        p = cache_path("games", cache_dir=tmp_path)
        assert p == tmp_path / "games.csv"

    def test_per_season_requires_season(self, tmp_path):
        with pytest.raises(ValueError):
            cache_path("stats_player_week", cache_dir=tmp_path)

    def test_unknown_source_raises(self, tmp_path):
        with pytest.raises(ValueError):
            cache_path("not_a_real_source", cache_dir=tmp_path)

    def test_source_url_formats_season(self):
        url = source_url("stats_player_week", 2023)
        assert url.endswith("stats_player_week_2023.csv")
        assert "nflverse-data" in url

    def test_source_url_refuses_before_first_season(self):
        with pytest.raises(ValueError):
            source_url("injuries", 2005)  # injuries starts 2009

    def test_source_url_non_per_season_ignores_season(self):
        assert source_url("games") == source_url("games", 1999)


class TestFetch:
    def _opener(self, calls, payload=b"a,b\n1,2\n"):
        def opener(url: str) -> bytes:
            calls.append(url)
            return payload
        return opener

    def test_downloads_and_caches(self, tmp_path):
        calls: list[str] = []
        path = fetch("games", cache_dir=tmp_path, opener=self._opener(calls))
        assert path.exists()
        assert path.read_bytes() == b"a,b\n1,2\n"
        assert len(calls) == 1

    def test_second_call_is_cache_hit_no_network(self, tmp_path):
        calls: list[str] = []
        fetch("games", cache_dir=tmp_path, opener=self._opener(calls))
        fetch("games", cache_dir=tmp_path, opener=self._opener(calls))
        assert len(calls) == 1  # second call never invoked the opener

    def test_refresh_forces_redownload(self, tmp_path):
        calls: list[str] = []
        fetch("games", cache_dir=tmp_path, opener=self._opener(calls))
        fetch("games", cache_dir=tmp_path, refresh=True, opener=self._opener(calls))
        assert len(calls) == 2

    def test_per_season_fetch(self, tmp_path):
        calls: list[str] = []
        path = fetch("stats_player_week", season=2022, cache_dir=tmp_path, opener=self._opener(calls))
        assert path.name == "stats_player_week_2022.csv"
        assert "2022" in calls[0]

    def test_compressed_source_is_cached_still_gzipped(self, tmp_path):
        raw_csv = b"player,ecr\nBob,1.0\n"
        gz = gzip.compress(raw_csv)

        def opener(url: str) -> bytes:
            return gz

        path = fetch("ff_ecr", cache_dir=tmp_path, opener=opener)
        assert path.suffix == ".gz"
        assert path.read_bytes() == gz  # cache stores the still-compressed bytes
        assert gzip.decompress(path.read_bytes()) == raw_csv

    def test_compressed_source_cache_path_has_gz_extension(self, tmp_path):
        p = cache_path("ff_ecr", cache_dir=tmp_path)
        assert p == tmp_path / "ff_ecr.csv.gz"

    def test_load_csv_rows_reads_gzipped_cache(self, tmp_path):
        raw_csv = b"player,ecr\nBob,1.0\n"
        p = tmp_path / "ff_ecr.csv.gz"
        p.write_bytes(gzip.compress(raw_csv))
        assert load_csv_rows(p) == [{"player": "Bob", "ecr": "1.0"}]

    def test_fetch_rows_roundtrips_a_compressed_source(self, tmp_path):
        raw_csv = b"player,ecr\nBob,1.0\n"

        def opener(url: str) -> bytes:
            return gzip.compress(raw_csv)

        rows = fetch_rows("ff_ecr", cache_dir=tmp_path, opener=opener)
        assert rows == [{"player": "Bob", "ecr": "1.0"}]

    def test_url_error_wrapped_as_fetch_error(self, tmp_path):
        import urllib.error

        def opener(url: str) -> bytes:
            raise urllib.error.URLError("boom")

        with pytest.raises(FetchError):
            fetch("games", cache_dir=tmp_path, opener=opener)

    def test_fetch_rows_parses_csv(self, tmp_path):
        calls: list[str] = []
        rows = fetch_rows("games", cache_dir=tmp_path, opener=self._opener(calls))
        assert rows == [{"a": "1", "b": "2"}]

    def test_load_csv_rows(self, tmp_path):
        p = tmp_path / "x.csv"
        p.write_text("name,pos\nBob,QB\n", encoding="utf-8")
        assert load_csv_rows(p) == [{"name": "Bob", "pos": "QB"}]


class TestCoverageTable:
    def test_reports_cached_and_uncached(self, tmp_path):
        calls: list[str] = []
        fetch("stats_player_week", season=2022, cache_dir=tmp_path, opener=self._opener_factory(calls))
        table = coverage_table([2021, 2022], cache_dir=tmp_path)
        assert table["stats_player_week"][2021] is False
        assert table["stats_player_week"][2022] is True

    def test_non_per_season_reported_under_season_zero(self, tmp_path):
        table = coverage_table([2022], cache_dir=tmp_path)
        assert 0 in table["games"]

    def test_every_declared_source_present(self, tmp_path):
        table = coverage_table([2022], cache_dir=tmp_path)
        assert set(table.keys()) == set(SOURCES.keys())

    @staticmethod
    def _opener_factory(calls):
        def opener(url: str) -> bytes:
            calls.append(url)
            return b"a,b\n1,2\n"
        return opener
