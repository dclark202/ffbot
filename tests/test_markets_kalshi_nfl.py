from __future__ import annotations

import json
from datetime import datetime

import pytest

from ffbot.live.schedule import LiveGame
from ffbot.markets import kalshi_nfl
from tests.conftest import mk_bp


def _games():
    # Real matchup verified live during scoping: DET @ CIN on Aug 13, 2026 --
    # CIN is home, matching Kalshi's "DET vs CIN" (away vs home) sub_title
    # convention for this exact event.
    kickoff = datetime(2026, 8, 13, 20, 20)
    return {
        "DET": LiveGame(opponent="CIN", home=False, roof="outdoors", kickoff=kickoff),
        "CIN": LiveGame(opponent="DET", home=True, roof="outdoors", kickoff=kickoff),
    }


def _liquid_market(strike: float, bid: float, ask: float, sub_title: str = "", volume: float = 100.0) -> dict:
    return {
        "floor_strike": str(strike),
        "yes_bid_dollars": f"{bid:.4f}",
        "yes_ask_dollars": f"{ask:.4f}",
        "yes_sub_title": sub_title,
        "volume_fp": str(volume),
        "open_interest_fp": str(volume),
    }


def _router(events_by_series: dict, markets_by_event: dict):
    """A fake opener that answers /events and /markets GETs by inspecting
    the query string -- close enough to the real Kalshi client's URL shape
    (see ffbot/markets/kalshi.py's _get) without needing a real HTTP mock."""
    def opener(url: str) -> bytes:
        if "/events?" in url:
            for series, events in events_by_series.items():
                if f"series_ticker={series}" in url:
                    return json.dumps({"events": events}).encode("utf-8")
            return json.dumps({"events": []}).encode("utf-8")
        if "/markets?" in url:
            for event_ticker, markets in markets_by_event.items():
                if f"event_ticker={event_ticker}" in url:
                    return json.dumps({"markets": markets}).encode("utf-8")
            return json.dumps({"markets": []}).encode("utf-8")
        raise AssertionError(f"unexpected URL: {url}")
    return opener


class TestImpliedMedian:
    def test_interpolates_between_straddling_contracts(self):
        markets = [
            _liquid_market(45.5, 0.60, 0.65),  # mid 0.625, >0.5
            _liquid_market(50.5, 0.35, 0.40),  # mid 0.375, <0.5
        ]
        median = kalshi_nfl._implied_median(markets)
        assert median is not None
        assert 45.5 < median < 50.5

    def test_too_few_liquid_markets_returns_none(self):
        markets = [_liquid_market(45.5, 0.60, 0.65)]
        assert kalshi_nfl._implied_median(markets) is None

    def test_illiquid_wide_spread_market_excluded(self):
        markets = [
            _liquid_market(45.5, 0.60, 0.65),
            {"floor_strike": "50.5", "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.90", "volume_fp": "5"},
        ]
        # the wide-spread market is dropped, leaving only one liquid point
        assert kalshi_nfl._implied_median(markets) is None

    def test_zero_volume_market_excluded(self):
        markets = [
            _liquid_market(45.5, 0.60, 0.65),
            _liquid_market(50.5, 0.35, 0.40, volume=0.0),
        ]
        assert kalshi_nfl._implied_median(markets) is None

    def test_no_crossing_falls_back_to_closest_to_half(self):
        markets = [
            _liquid_market(10.5, 0.90, 0.95),
            _liquid_market(20.5, 0.80, 0.85),
        ]
        assert kalshi_nfl._implied_median(markets) == 20.5


class TestEventTeams:
    def test_parses_away_vs_home(self):
        assert kalshi_nfl._event_teams("DET vs CIN (Aug 13)") == ("DET", "CIN")

    def test_no_match_returns_none(self):
        assert kalshi_nfl._event_teams("garbage") is None


class TestIdentifySpreadTeam:
    def test_unambiguous_city_name(self):
        assert kalshi_nfl._identify_spread_team("Seattle wins by over 7.5 points", ("NE", "SEA")) == "SEA"

    def test_same_metro_disambiguates_on_nickname(self):
        assert kalshi_nfl._identify_spread_team("Giants win by over 3.5 points", ("NYG", "NYJ")) == "NYG"

    def test_ambiguous_shared_city_returns_none(self):
        # Neither nickname present, and both candidates would match a bare
        # "New York" -- must not silently guess.
        assert kalshi_nfl._identify_spread_team("New York wins by over 3.5 points", ("NYG", "NYJ")) is None

    def test_no_match_returns_none(self):
        assert kalshi_nfl._identify_spread_team("nothing recognizable here", ("NE", "SEA")) is None


class TestGameOdds:
    def test_total_and_spread_combine_into_team_totals(self):
        events = {
            "KXNFLTOTAL": [{"event_ticker": "KXNFLTOTAL-X", "sub_title": "DET vs CIN (Aug 13)"}],
            "KXNFLSPREAD": [{"event_ticker": "KXNFLSPREAD-X", "sub_title": "DET vs CIN (Aug 13)"}],
        }
        markets = {
            "KXNFLTOTAL-X": [
                _liquid_market(45.5, 0.60, 0.65),
                _liquid_market(50.5, 0.35, 0.40),
            ],
            "KXNFLSPREAD-X": [
                _liquid_market(3.5, 0.60, 0.65, sub_title="Cincinnati wins by over 3.5 points"),
                _liquid_market(7.5, 0.35, 0.40, sub_title="Cincinnati wins by over 7.5 points"),
            ],
        }
        out = kalshi_nfl.game_odds(_games(), opener=_router(events, markets))
        assert "DET" in out and "CIN" in out
        # Cincinnati (away) is favored -- its implied total should exceed Detroit's.
        assert out["CIN"]["team_total"] > out["DET"]["team_total"]
        assert out["DET"]["team_total"] + out["DET"]["opp_total"] == pytest.approx(
            out["CIN"]["team_total"] + out["CIN"]["opp_total"]
        )

    def test_series_not_in_allowlist_is_a_no_op(self):
        events = {"KXNFLTOTAL": [{"event_ticker": "KXNFLTOTAL-X", "sub_title": "DET vs CIN (Aug 13)"}]}
        markets = {"KXNFLTOTAL-X": [_liquid_market(45.5, 0.60, 0.65), _liquid_market(50.5, 0.35, 0.40)]}
        out = kalshi_nfl.game_odds(_games(), series=(), opener=_router(events, markets))
        assert out == {}

    def test_total_without_identifiable_spread_yields_no_data(self):
        # A real market never fabricates a 50/50 split when the spread
        # side can't be identified -- absence is preferred to a guess.
        events = {
            "KXNFLTOTAL": [{"event_ticker": "KXNFLTOTAL-X", "sub_title": "DET vs CIN (Aug 13)"}],
            "KXNFLSPREAD": [{"event_ticker": "KXNFLSPREAD-X", "sub_title": "DET vs CIN (Aug 13)"}],
        }
        markets = {
            "KXNFLTOTAL-X": [_liquid_market(45.5, 0.60, 0.65), _liquid_market(50.5, 0.35, 0.40)],
            "KXNFLSPREAD-X": [
                _liquid_market(3.5, 0.60, 0.65, sub_title="unrecognizable text"),
                _liquid_market(7.5, 0.35, 0.40, sub_title="unrecognizable text"),
            ],
        }
        out = kalshi_nfl.game_odds(_games(), opener=_router(events, markets))
        assert out == {}

    def test_unmatched_event_ignored(self):
        events = {"KXNFLTOTAL": [{"event_ticker": "KXNFLTOTAL-Y", "sub_title": "KC vs BUF (Aug 13)"}]}
        out = kalshi_nfl.game_odds(_games(), opener=_router(events, {}))
        assert out == {}

    def test_no_games_is_a_no_op_before_any_fetch(self):
        def opener(url: str) -> bytes:
            raise AssertionError("must not fetch when there are no games")

        assert kalshi_nfl.game_odds({}, opener=opener) == {}

    def test_transport_failure_degrades_to_empty_never_raises(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        assert kalshi_nfl.game_odds(_games(), opener=failing_opener) == {}


class TestPlayerPropProbabilities:
    def test_returns_name_to_probability(self):
        events = {"KXNFLTD": [{"event_ticker": "KXNFLTD-X", "sub_title": "DET vs CIN (Aug 13)"}]}
        markets = {
            "KXNFLTD-X": [
                {
                    "yes_sub_title": "Mike Washington Jr.: 1+",
                    "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.34",
                    "volume_fp": "50", "open_interest_fp": "50",
                },
            ],
        }
        out = kalshi_nfl.player_prop_probabilities("KXNFLTD", _games(), opener=_router(events, markets))
        assert out["Mike Washington Jr."] == pytest.approx(0.32)

    def test_illiquid_market_excluded(self):
        events = {"KXNFLTD": [{"event_ticker": "KXNFLTD-X", "sub_title": "DET vs CIN (Aug 13)"}]}
        markets = {
            "KXNFLTD-X": [
                {
                    "yes_sub_title": "Some Player: 1+",
                    "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.90",
                    "volume_fp": "5", "open_interest_fp": "5",
                },
            ],
        }
        out = kalshi_nfl.player_prop_probabilities("KXNFLTD", _games(), opener=_router(events, markets))
        assert out == {}

    def test_unmatched_event_ignored(self):
        events = {"KXNFLTD": [{"event_ticker": "KXNFLTD-Y", "sub_title": "KC vs BUF (Aug 13)"}]}
        out = kalshi_nfl.player_prop_probabilities("KXNFLTD", _games(), opener=_router(events, {}))
        assert out == {}

    def test_no_games_is_a_no_op(self):
        def opener(url: str) -> bytes:
            raise AssertionError("must not fetch when there are no games")

        assert kalshi_nfl.player_prop_probabilities("KXNFLTD", {}, opener=opener) == {}

    def test_transport_failure_degrades_to_empty_never_raises(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        assert kalshi_nfl.player_prop_probabilities("KXNFLTD", _games(), opener=failing_opener) == {}


class TestSeasonPlayerProbabilities:
    def test_returns_name_to_probability_no_event_matching_needed(self):
        markets = {
            "KXNFLFFTOP-X": [
                {
                    "yes_sub_title": "Ja'Marr Chase: Top Fantasy WR",
                    "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44",
                    "volume_fp": "50", "open_interest_fp": "50",
                },
            ],
        }

        def opener(url: str) -> bytes:
            assert "series_ticker=KXNFLFFTOP" in url
            return json.dumps({"markets": markets["KXNFLFFTOP-X"]}).encode("utf-8")

        out = kalshi_nfl.season_player_probabilities("KXNFLFFTOP", opener=opener)
        assert out["Ja'Marr Chase"] == pytest.approx(0.42)

    def test_illiquid_market_excluded(self):
        def opener(url: str) -> bytes:
            return json.dumps({"markets": [{
                "yes_sub_title": "Some Player: Top Fantasy RB",
                "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.90",
                "volume_fp": "1", "open_interest_fp": "1",
            }]}).encode("utf-8")

        assert kalshi_nfl.season_player_probabilities("KXNFLFFTOP", opener=opener) == {}

    def test_transport_failure_degrades_to_empty_never_raises(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        assert kalshi_nfl.season_player_probabilities("KXNFLFFTOP", opener=failing_opener) == {}


class TestRankWithinPosition:
    def test_ranks_within_position_not_across(self):
        probs = {"Alpha": 0.9, "Beta": 0.1, "Gamma": 0.5}
        position_by_key = {"a:rb": "RB", "b:rb": "RB", "c:wr": "WR"}
        name_to_key = {"alpha": "a:rb", "beta": "b:rb", "gamma": "c:wr"}
        out = kalshi_nfl.rank_within_position(probs, position_by_key, name_to_key)
        # Only one WR -- neutral midpoint, no peers to rank against.
        assert out["c:wr"] == 0.5
        # Two RBs: Alpha (0.9) ranks above Beta (0.1).
        assert out["a:rb"] == 1.0
        assert out["b:rb"] == 0.0

    def test_unmatched_name_is_silently_dropped(self):
        probs = {"Nobody On The Board": 0.9}
        out = kalshi_nfl.rank_within_position(probs, {}, {})
        assert out == {}

    def test_empty_probabilities_returns_empty(self):
        assert kalshi_nfl.rank_within_position({}, {}, {}) == {}


class TestDraftSignal:
    def _board(self):
        players = [mk_bp("Alpha", "RB", points=200.0), mk_bp("Beta", "RB", points=150.0)]
        return type("FakeBoard", (), {"players": players})()

    def test_matches_by_normalized_name_and_ranks_within_position(self):
        def opener(url: str) -> bytes:
            return json.dumps({"markets": [
                {"yes_sub_title": "Alpha: Top Fantasy RB", "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.74",
                 "volume_fp": "50", "open_interest_fp": "50"},
                {"yes_sub_title": "Beta: Top Fantasy RB", "yes_bid_dollars": "0.20", "yes_ask_dollars": "0.24",
                 "volume_fp": "50", "open_interest_fp": "50"},
            ]}).encode("utf-8")

        out = kalshi_nfl.draft_signal(self._board(), opener=opener)
        assert out["alpha:RB"] == 1.0
        assert out["beta:RB"] == 0.0

    def test_no_markets_returns_empty(self):
        def opener(url: str) -> bytes:
            return json.dumps({"markets": []}).encode("utf-8")

        assert kalshi_nfl.draft_signal(self._board(), opener=opener) == {}

    def test_transport_failure_degrades_to_empty_never_raises(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        assert kalshi_nfl.draft_signal(self._board(), opener=failing_opener) == {}


class TestWeeklySignal:
    """weekly_signal is game-matched (per-week events), unlike draft_signal's
    season-long series -- reuses _games() from the top of this file (the
    real DET@CIN matchup verified live during scoping)."""

    def _board(self):
        players = [mk_bp("Mike Washington Jr.", "WR", points=100.0), mk_bp("Other Guy", "WR", points=80.0)]
        return type("FakeBoard", (), {"players": players})()

    def test_matches_by_normalized_name_and_ranks_within_position(self):
        events = {"KXNFLTD": [{"event_ticker": "KXNFLTD-X", "sub_title": "DET vs CIN (Aug 13)"}]}
        markets = {
            "KXNFLTD-X": [
                {"yes_sub_title": "Mike Washington Jr.: 1+", "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.74",
                 "volume_fp": "50", "open_interest_fp": "50"},
                {"yes_sub_title": "Other Guy: 1+", "yes_bid_dollars": "0.20", "yes_ask_dollars": "0.24",
                 "volume_fp": "50", "open_interest_fp": "50"},
            ],
        }
        out = kalshi_nfl.weekly_signal(_games(), self._board(), opener=_router(events, markets))
        assert out["mike washington jr.:WR"] == 1.0
        assert out["other guy:WR"] == 0.0

    def test_no_games_is_a_no_op_before_any_fetch(self):
        def opener(url: str) -> bytes:
            raise AssertionError("must not fetch when there are no games")

        assert kalshi_nfl.weekly_signal({}, self._board(), opener=opener) == {}

    def test_no_markets_returns_empty(self):
        events = {"KXNFLTD": [{"event_ticker": "KXNFLTD-X", "sub_title": "DET vs CIN (Aug 13)"}]}
        out = kalshi_nfl.weekly_signal(_games(), self._board(), opener=_router(events, {}))
        assert out == {}

    def test_transport_failure_degrades_to_empty_never_raises(self):
        def failing_opener(url: str) -> bytes:
            raise OSError("simulated network failure")

        assert kalshi_nfl.weekly_signal(_games(), self._board(), opener=failing_opener) == {}
