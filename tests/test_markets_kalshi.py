from __future__ import annotations

import json

import pytest

from ffbot.markets import kalshi


def _opener(response: dict, calls: list | None = None):
    def opener(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        return json.dumps(response).encode("utf-8")
    return opener


class TestListSeries:
    def test_returns_series_list(self):
        out = kalshi.list_series(opener=_opener({"series": [{"ticker": "KXNFLTD"}]}))
        assert out == [{"ticker": "KXNFLTD"}]

    def test_missing_key_defaults_to_empty_list(self):
        assert kalshi.list_series(opener=_opener({})) == []

    def test_category_is_passed_as_a_query_param(self):
        calls: list = []
        kalshi.list_series(category="Sports", opener=_opener({"series": []}, calls))
        assert "category=Sports" in calls[0]

    def test_none_category_omits_the_param(self):
        calls: list = []
        kalshi.list_series(category=None, opener=_opener({"series": []}, calls))
        assert "category" not in calls[0]


class TestListEvents:
    def test_returns_events_and_passes_series_ticker(self):
        calls: list = []
        out = kalshi.list_events("KXNFLTD", status="open", opener=_opener({"events": [{"event_ticker": "X"}]}, calls))
        assert out == [{"event_ticker": "X"}]
        assert "series_ticker=KXNFLTD" in calls[0]
        assert "status=open" in calls[0]


class TestListMarkets:
    def test_returns_markets_for_an_event(self):
        calls: list = []
        out = kalshi.list_markets(event_ticker="KXNFLTD-26AUG13DETCIN", opener=_opener({"markets": [{"ticker": "M1"}]}, calls))
        assert out == [{"ticker": "M1"}]
        assert "event_ticker=KXNFLTD-26AUG13DETCIN" in calls[0]


class TestGetMarket:
    def test_returns_the_market_object(self):
        out = kalshi.get_market("SOME-TICKER", opener=_opener({"market": {"ticker": "SOME-TICKER", "yes_bid": 55}}))
        assert out["yes_bid"] == 55

    def test_url_includes_the_ticker_path(self):
        calls: list = []
        kalshi.get_market("SOME-TICKER", opener=_opener({"market": {}}, calls))
        assert "/markets/SOME-TICKER" in calls[0]


class TestGetCandlesticks:
    def test_returns_candlestick_list(self):
        out = kalshi.get_candlesticks(
            "KXNFLTD", "SOME-TICKER", start_ts=1000, end_ts=2000,
            opener=_opener({"candlesticks": [{"open": 50}]}),
        )
        assert out == [{"open": 50}]

    def test_url_includes_series_and_market_path_and_params(self):
        calls: list = []
        kalshi.get_candlesticks(
            "KXNFLTD", "SOME-TICKER", start_ts=1000, end_ts=2000, period_interval=60,
            opener=_opener({"candlesticks": []}, calls),
        )
        url = calls[0]
        assert "/series/KXNFLTD/markets/SOME-TICKER/candlesticks" in url
        assert "start_ts=1000" in url and "end_ts=2000" in url and "period_interval=60" in url


class TestErrorHandling:
    def test_transport_failure_raises_kalshi_error(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        with pytest.raises(kalshi.KalshiError):
            kalshi.list_series(opener=failing_opener)
