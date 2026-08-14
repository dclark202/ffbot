from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import Board  # noqa: E402
from ffbot.config import Config, SeasonConfig  # noqa: E402
from ffbot.league_rosters import (  # noqa: E402
    LeagueRosters,
    build_teams_from_sleeper,
    fetch_league_rosters,
    load_league_rosters,
)
from scripts import import_league_rosters as ilr  # noqa: E402
from tests.conftest import mk_bp  # noqa: E402


def _board():
    players = [
        mk_bp("Josh Allen", "QB", team="BUF"),
        mk_bp("Christian McCaffrey", "RB", team="SF"),
        mk_bp("Ja'Marr Chase", "WR", team="CIN"),
        mk_bp("Free Agent Guy", "WR", team="MIA"),
    ]
    return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})


class TestParseTeamBlocks:
    def test_splits_into_teams(self):
        text = "== Team A ==\nJosh Allen\nChristian McCaffrey\n\n== Team B ==\nJa'Marr Chase\n"
        teams = ilr.parse_team_blocks(text)
        assert teams == {
            "Team A": ["Josh Allen", "Christian McCaffrey"],
            "Team B": ["Ja'Marr Chase"],
        }

    def test_blank_lines_ignored(self):
        text = "== Team A ==\n\nJosh Allen\n\n\nChristian McCaffrey\n"
        teams = ilr.parse_team_blocks(text)
        assert teams["Team A"] == ["Josh Allen", "Christian McCaffrey"]

    def test_no_headers_raises(self):
        with pytest.raises(ilr.RosterImportError):
            ilr.parse_team_blocks("Josh Allen\nChristian McCaffrey\n")

    def test_line_before_first_header_raises(self):
        with pytest.raises(ilr.RosterImportError, match="before any"):
            ilr.parse_team_blocks("Stray Line\n== Team A ==\nJosh Allen\n")

    def test_duplicate_header_merges(self):
        text = "== Team A ==\nJosh Allen\n== Team A ==\nChristian McCaffrey\n"
        teams = ilr.parse_team_blocks(text)
        assert teams["Team A"] == ["Josh Allen", "Christian McCaffrey"]


class TestMatchRosters:
    def test_exact_matches_resolve(self):
        teams = {"Team A": ["Josh Allen", "Christian McCaffrey"], "Team B": ["Ja'Marr Chase"]}
        matched, unmatched = ilr.match_rosters(teams, _board(), Config())
        assert matched["Team A"] == ["Josh Allen", "Christian McCaffrey"]
        assert matched["Team B"] == ["Ja'Marr Chase"]
        assert unmatched == []

    def test_unmatched_name_reported_with_suggestion_not_dropped(self):
        teams = {"Team A": ["Josh Allen", "Totally Unknown Player"]}
        matched, unmatched = ilr.match_rosters(teams, _board(), Config())
        assert matched["Team A"] == ["Josh Allen"]
        assert len(unmatched) == 1
        assert "Totally Unknown Player" in unmatched[0]
        assert "Team A" in unmatched[0]

    def test_typo_gets_a_suggestion(self):
        teams = {"Team A": ["Josh Alen"]}  # missing an 'l'
        matched, unmatched = ilr.match_rosters(teams, _board(), Config())
        assert unmatched  # fuzzy margin may or may not auto-resolve; either way, reported
        # If it auto-resolved, matched should contain the real name; if not,
        # the unmatched entry should suggest it.
        if matched["Team A"]:
            assert matched["Team A"] == ["Josh Allen"]
        else:
            assert "Josh Allen" in unmatched[0]


class TestWriteAndLoadRoundTrip:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "league_rosters.yml"
        teams = {"Team A": ["Josh Allen"], "Team B": ["Christian McCaffrey"]}
        ilr.write_league_rosters(path, week_num=5, teams=teams, unmatched=["Team A: 'X'"], source="paste")
        loaded = load_league_rosters(path)
        assert loaded.week == 5
        assert loaded.teams == teams
        assert loaded.unmatched == ["Team A: 'X'"]
        assert loaded.source == "paste"

    def test_missing_file_is_empty_inert(self, tmp_path):
        loaded = load_league_rosters(tmp_path / "does_not_exist.yml")
        assert loaded == LeagueRosters()
        assert loaded.rostered_names() == set()

    def test_rostered_names_is_normalized_and_flattened(self):
        rosters = LeagueRosters(teams={"A": ["Josh Allen"], "B": ["Ja'Marr Chase", "A.J. Brown"]})
        names = rosters.rostered_names()
        assert "josh allen" in names
        assert "jamarr chase" in names
        assert "aj brown" in names


class TestBuildTeamsFromSleeper:
    """Lives in ffbot/league_rosters.py (moved there so both the one-off
    import script and the per-run live fetch below share one join);
    `scripts/import_league_rosters.py` re-imports it, so `ilr.
    build_teams_from_sleeper` is exercised too for that import path."""

    def test_names_resolved_by_player_id_join(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["4046", "BUF"]}]
        league_users = [{"user_id": "u1", "display_name": "manager1", "metadata": {"team_name": "The Test Squad"}}]
        players = {
            "4046": {"full_name": "Patrick Mahomes"},
            "BUF": {"first_name": "Buffalo", "last_name": "Bills"},
        }
        teams, unmatched = build_teams_from_sleeper(rosters, league_users, players)
        assert teams == {"The Test Squad": ["Patrick Mahomes", "Buffalo Bills"]}
        assert unmatched == []
        # Re-exported at the old import path too -- the script's own tests
        # (TestMainRequiresExactlyOneSource) and any user of `ilr.
        # build_teams_from_sleeper` keep working unchanged.
        assert ilr.build_teams_from_sleeper is build_teams_from_sleeper

    def test_missing_team_name_falls_back_to_display_name_then_owner_id(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": []}]
        league_users = [{"user_id": "u1", "display_name": "duncan"}]
        teams, _ = build_teams_from_sleeper(rosters, league_users, {})
        assert "duncan" in teams

    def test_unknown_player_id_reported_not_dropped(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["9999"]}]
        league_users = [{"user_id": "u1", "display_name": "duncan"}]
        teams, unmatched = build_teams_from_sleeper(rosters, league_users, {})
        assert teams["duncan"] == []
        assert any("9999" in u for u in unmatched)

    def test_unowned_roster_gets_a_placeholder_team_name(self):
        rosters = [{"roster_id": 7, "owner_id": None, "players": []}]
        teams, _ = build_teams_from_sleeper(rosters, [], {})
        assert "roster 7" in teams


class _FakeSleeperClient:
    def __init__(self, rosters=None, users=None):
        self._rosters = rosters or []
        self._users = users or []
        self.rosters_calls: list[dict] = []

    def rosters(self, league_id, **kwargs):
        self.rosters_calls.append(kwargs)
        return self._rosters

    def league_users(self, league_id):
        return self._users


class TestFetchLeagueRosters:
    def test_joins_rosters_and_users_into_league_rosters(self):
        client = _FakeSleeperClient(
            rosters=[{"roster_id": 1, "owner_id": "u1", "players": ["4046"]}],
            users=[{"user_id": "u1", "display_name": "duncan", "metadata": {"team_name": "The Test Squad"}}],
        )
        players = {"4046": {"full_name": "Patrick Mahomes"}}
        result = fetch_league_rosters(client, "L1", players, week=3)
        assert result.teams == {"The Test Squad": ["Patrick Mahomes"]}
        assert result.week == 3
        assert result.source == "api"
        assert result.generated  # today's date, non-empty
        assert result.unmatched == []

    def test_ttl_minutes_passed_through_to_rosters_call(self):
        client = _FakeSleeperClient(rosters=[], users=[])
        fetch_league_rosters(client, "L1", {}, ttl_minutes=15.0)
        assert client.rosters_calls == [{"ttl_minutes": 15.0}]

    def test_no_ttl_override_passes_no_kwargs(self):
        client = _FakeSleeperClient(rosters=[], users=[])
        fetch_league_rosters(client, "L1", {})
        assert client.rosters_calls == [{}]


class TestMainRequiresExactlyOneSource:
    def test_neither_paste_nor_live_is_an_error(self, capsys):
        rc = ilr.main([])
        assert rc == 1
        assert "exactly one" in capsys.readouterr().err

    def test_both_paste_and_live_is_an_error(self, tmp_path, capsys):
        rc = ilr.main(["--paste", str(tmp_path / "x.txt"), "--live"])
        assert rc == 1
        assert "exactly one" in capsys.readouterr().err

    def test_live_without_league_id_is_an_error(self, tmp_path, monkeypatch, capsys):
        config_path = tmp_path / "config.yml"
        config_path.write_text("sleeper:\n  league_id: \"\"\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = ilr.main(["--live", "--config", str(config_path)])
        assert rc == 1
        assert "league_id" in capsys.readouterr().err


class TestWaiverPoolExclusion:
    def test_player_rostered_elsewhere_excluded_from_waivers(self):
        from ffbot import week
        from ffbot.models import Player

        board = Board(
            players=[
                mk_bp("My Rb", "RB", points=100.0),
                mk_bp("Rival Star", "WR", points=250.0),  # a huge gain if not excluded
                mk_bp("Real Free Agent", "WR", points=120.0),
            ],
            by_key={}, replacement={}, starters_per_pos={}, tier_last={},
        )
        board.by_key = {p.key: p for p in board.players}
        roster = [Player(player_id=1, name="My Rb", eligible_positions=["RB"], projected_points=10.0)]
        cfg = Config(roster_positions={"RB": 1, "WR": 1, "BN": 2}, season=SeasonConfig(ros_blend=1.0))
        league_rosters = LeagueRosters(teams={"Rival Team": ["Rival Star"]})

        candidates, _ = week.waiver_candidates(
            roster, board, cfg.roster_positions, cfg, league_rosters=league_rosters,
        )
        names = [c.add_name for c in candidates]
        assert "Rival Star" not in names
        assert "Real Free Agent" in names
