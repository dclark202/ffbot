from __future__ import annotations

import json

import pytest

from ffbot.sleeper.cache import SleeperFetchError
from ffbot.sleeper.client import SleeperClient


def _opener(routes: dict[str, object]):
    """`{url_substring: payload}` -> an opener that returns the first
    matching payload as JSON bytes, or raises if nothing matches (so a test
    fails loudly on an unexpected URL rather than silently misrouting)."""

    def opener(url: str) -> bytes:
        for substring, payload in routes.items():
            if substring in url:
                return json.dumps(payload).encode("utf-8")
        raise AssertionError(f"unexpected URL: {url}")

    return opener


def _raising_opener(url: str) -> bytes:
    raise OSError("network unreachable")


class TestDocumentedEndpoints:
    def test_nfl_state(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/state/nfl": {"week": 3, "season": "2026"}}))
        assert client.nfl_state() == {"week": 3, "season": "2026"}

    def test_user_found(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/user/duncan": {"user_id": "1", "username": "duncan"}}))
        assert client.user("duncan") == {"user_id": "1", "username": "duncan"}

    def test_user_not_found_returns_none_not_raise(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_raising_opener)
        assert client.user("nobody") is None

    def test_user_leagues(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/leagues/nfl/2026": [{"league_id": "L1"}]}))
        assert client.user_leagues("1", 2026) == [{"league_id": "L1"}]

    def test_league(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/league/L1": {"league_id": "L1", "roster_positions": ["QB"]}}))
        assert client.league("L1")["roster_positions"] == ["QB"]

    def test_rosters(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/rosters": [{"roster_id": 1, "players": ["4046"]}]}))
        assert client.rosters("L1") == [{"roster_id": 1, "players": ["4046"]}]

    def test_league_users(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/league/L1/users": [{"user_id": "1"}]}))
        assert client.league_users("L1") == [{"user_id": "1"}]

    def test_matchups_url_includes_week(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return b"[]"

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.matchups("L1", 6)
        assert calls == ["https://api.sleeper.app/v1/league/L1/matchups/6"]

    def test_transactions_url_includes_week(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return b"[]"

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.transactions("L1", 6)
        assert calls == ["https://api.sleeper.app/v1/league/L1/transactions/6"]

    def test_traded_picks(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/traded_picks": [{"round": 1}]}))
        assert client.traded_picks("L1") == [{"round": 1}]

    def test_league_drafts(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/league/L1/drafts": [{"draft_id": "D1"}]}))
        assert client.league_drafts("L1") == [{"draft_id": "D1"}]

    def test_user_drafts(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/drafts/nfl/2026": [{"draft_id": "D1"}]}))
        assert client.user_drafts("1", 2026) == [{"draft_id": "D1"}]

    def test_draft_is_never_cached(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return json.dumps({"status": "drafting", "last_picked": 123}).encode("utf-8")

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.draft("D1")
        client.draft("D1")
        assert len(calls) == 2  # never trusts a cache file
        assert not list(tmp_path.glob("*.json"))  # and never writes one

    def test_draft_picks_is_never_cached(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return b"[]"

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.draft_picks("D1")
        client.draft_picks("D1")
        assert len(calls) == 2

    def test_draft_picks_returns_pick_objects(self, tmp_path):
        pick = {
            "draft_id": "D1", "pick_no": 1, "picked_by": "76888557872365568", "player_id": "2315",
            "roster_id": 10, "round": 1, "draft_slot": 1,
            "metadata": {"first_name": "Todd", "last_name": "Gurley", "position": "RB", "team": "LAR"},
        }

        def opener(url: str) -> bytes:
            return json.dumps([pick]).encode("utf-8")

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        picks = client.draft_picks("D1")
        assert picks == [pick]

    def test_draft_traded_picks_is_never_cached(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return b"[]"

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.draft_traded_picks("D1")
        client.draft_traded_picks("D1")
        assert len(calls) == 2

    def test_players_dump(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_opener({"/players/nfl": {"4046": {"full_name": "Patrick Mahomes"}}}))
        assert client.players()["4046"]["full_name"] == "Patrick Mahomes"

    def test_trending_rejects_bad_kind(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_raising_opener)
        with pytest.raises(ValueError, match="add.*drop"):
            client.trending(kind="sideways")

    def test_trending_url_shape(self, tmp_path):
        calls: list[str] = []

        def opener(url: str) -> bytes:
            calls.append(url)
            return b"[]"

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        client.trending(kind="add", lookback_hours=24, limit=25)
        assert calls == [
            "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=25"
        ]


class TestUndocumentedEndpointsIsolatedWithFallback:
    def test_season_projections_url_and_shape(self, tmp_path):
        calls: list[str] = []
        entry = {"company": "rotowire", "player": {"position": "RB"}, "stats": {"pts_ppr": 300.0, "adp_ppr": 1.6}}

        def opener(url: str) -> bytes:
            calls.append(url)
            return json.dumps([entry]).encode("utf-8")

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        rows = client.season_projections(2026)
        assert rows == [entry]
        assert calls[0].startswith("https://api.sleeper.com/projections/nfl/2026?season_type=regular&")
        assert "order_by=adp_ppr" in calls[0]

    def test_season_projections_network_failure_raises_fetch_error(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_raising_opener)
        with pytest.raises(SleeperFetchError):
            client.season_projections(2026)

    def test_ownership_url_and_shape(self, tmp_path):
        calls: list[str] = []
        payload = {"4046": {"owned": 99.2, "started": 96.3}}

        def opener(url: str) -> bytes:
            calls.append(url)
            return json.dumps(payload).encode("utf-8")

        client = SleeperClient(cache_dir=tmp_path, opener=opener)
        result = client.ownership(2026, 1)
        assert result == payload
        assert calls == ["https://api.sleeper.com/players/nfl/research/regular/2026/1"]

    def test_ownership_network_failure_raises_fetch_error(self, tmp_path):
        client = SleeperClient(cache_dir=tmp_path, opener=_raising_opener)
        with pytest.raises(SleeperFetchError):
            client.ownership(2026, 1)
