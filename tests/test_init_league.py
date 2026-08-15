from __future__ import annotations

import json

import pytest
import yaml

import scripts.init_league as init_league
from ffbot.sleeper.client import SleeperClient


def _opener(routes: dict[str, object]):
    """Same convention as tests/test_sleeper_client.py's `_opener`: first
    matching URL substring wins; an unrouted URL fails the test loudly
    rather than silently hitting the network."""

    def opener(url: str) -> bytes:
        for substring, payload in routes.items():
            if substring in url:
                return json.dumps(payload).encode("utf-8")
        raise AssertionError(f"unexpected URL: {url}")

    return opener


_SCORING = {"pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "rec": 1.0, "rec_yd": 0.1, "rec_td": 6}
_LEAGUE = {
    "league_id": "L1",
    "name": "Test League",
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "FLEX", "BN", "BN"],
    "scoring_settings": _SCORING,
    "settings": {"playoff_week_start": 15, "playoff_teams": 4, "reserve_slots": 1},
}


def _patch_client(monkeypatch, league=_LEAGUE, rosters=None, tmp_path=None):
    routes = {
        "/state/nfl": {"week": 3, "season": "2026"},
        "/user/duncan": {"user_id": "u1", "username": "duncan"},
        "/leagues/nfl/2026": [league],
        "/league/L1/rosters": rosters if rosters is not None else [{"roster_id": 7, "owner_id": "u1"}],
    }
    monkeypatch.setattr(init_league, "SleeperClient", lambda: SleeperClient(
        cache_dir=tmp_path / "cache", opener=_opener(routes)
    ))


class TestInitLeagueDryRun:
    def test_dry_run_writes_nothing_to_disk(self, tmp_path, monkeypatch, capsys):
        _patch_client(monkeypatch, tmp_path=tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert not (tmp_path / "config.local.yml").exists()
        assert not (tmp_path / "league.yml").exists()
        out = capsys.readouterr().out
        assert "would write" in out

    def test_dry_run_never_touches_config_yml(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path), "--dry-run"])
        assert not (tmp_path / "config.yml").exists()


class TestInitLeagueWrite:
    def test_writes_config_local_with_sleeper_block(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, tmp_path=tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert rc == 0
        overlay = yaml.safe_load((tmp_path / "config.local.yml").read_text())
        assert overlay["sleeper"] == {"league_id": "L1", "username": "duncan", "roster_id": 7}

    def test_never_writes_config_yml_itself(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert not (tmp_path / "config.yml").exists()

    def test_preserves_existing_config_local_overlay_keys(self, tmp_path, monkeypatch):
        (tmp_path / "config.local.yml").write_text(yaml.safe_dump({"draft": {"num_teams": 10}}))
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        overlay = yaml.safe_load((tmp_path / "config.local.yml").read_text())
        assert overlay["draft"] == {"num_teams": 10}
        assert overlay["sleeper"]["league_id"] == "L1"

    def test_writes_league_yml_from_scoring(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        league = yaml.safe_load((tmp_path / "league.yml").read_text())
        assert league["passing"]["yards_per_point"] == 25.0
        assert league["passing"]["td"] == 4
        assert league["name"] == "Test League"

    def test_does_not_overwrite_existing_league_yml_without_force(self, tmp_path, monkeypatch):
        (tmp_path / "league.yml").write_text("name: existing\n")
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert (tmp_path / "league.yml").read_text() == "name: existing\n"

    def test_force_overwrites_existing_league_yml(self, tmp_path, monkeypatch):
        (tmp_path / "league.yml").write_text("name: existing\n")
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path), "--force"])
        league = yaml.safe_load((tmp_path / "league.yml").read_text())
        assert league["name"] == "Test League"

    def test_unmapped_scoring_keys_are_printed_not_silently_dropped(self, tmp_path, monkeypatch, capsys):
        league = dict(_LEAGUE)
        league["scoring_settings"] = dict(_SCORING, idp_tkl_solo=1.0)
        _patch_client(monkeypatch, league=league, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert "idp_tkl_solo" in capsys.readouterr().out

    def test_copies_roster_example_when_absent(self, tmp_path, monkeypatch):
        (tmp_path / "roster.example.yml").write_text("players: []\n")
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert (tmp_path / "roster.yml").exists()

    def test_does_not_overwrite_existing_roster_yml(self, tmp_path, monkeypatch):
        (tmp_path / "roster.example.yml").write_text("players: []\n")
        (tmp_path / "roster.yml").write_text("players: [real]\n")
        _patch_client(monkeypatch, tmp_path=tmp_path)
        init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert (tmp_path / "roster.yml").read_text() == "players: [real]\n"


class TestInitLeagueSyncSetup:
    """`--sync` needs sleeper.league_id (already covered above) and
    draft/sleeper_ids.json — init_league.py now builds the latter too, best
    effort, so sync is live the moment the script finishes."""

    _BOARD_CSV = (
        "Player,Team,POS,BYE,FPTS,AVG\n"
        "Ja'Marr Chase,CIN,WR,6,336.1,1.0\n"
        "Christian McCaffrey,SF,RB,8,310.4,2.0\n"
    )
    _PLAYERS_DUMP = {
        "p1": {"full_name": "Ja'Marr Chase", "position": "WR", "team": "CIN"},
        "p2": {"full_name": "Christian McCaffrey", "position": "RB", "team": "SF"},
    }

    def _write_board_config(self, tmp_path):
        (tmp_path / "board.csv").write_text(self._BOARD_CSV, encoding="utf-8")
        (tmp_path / "config.yml").write_text(
            yaml.safe_dump({
                "draft": {
                    "board_csv": [str(tmp_path / "board.csv")],
                    # Keep this hermetic: the default intel_file
                    # ("draft/intel.yml") is CWD-relative, and pytest's CWD
                    # is the real repo root -- pointing it at a path that
                    # doesn't exist under tmp_path avoids picking up this
                    # repo's own draft/intel.yml (real players our 2-player
                    # fake board doesn't contain, which would otherwise warn).
                    "intel_file": str(tmp_path / "no-intel.yml"),
                }
            }), encoding="utf-8"
        )

    def _patch_client_with_players(self, monkeypatch, tmp_path, league=_LEAGUE):
        routes = {
            "/state/nfl": {"week": 3, "season": "2026"},
            "/user/duncan": {"user_id": "u1", "username": "duncan"},
            "/leagues/nfl/2026": [league],
            "/league/L1/rosters": [{"roster_id": 7, "owner_id": "u1"}],
            "/players/nfl": self._PLAYERS_DUMP,
        }
        monkeypatch.setattr(init_league, "SleeperClient", lambda: SleeperClient(
            cache_dir=tmp_path / "cache", opener=_opener(routes)
        ))

    def test_writes_sleeper_ids_json_when_a_board_is_already_configured(self, tmp_path, monkeypatch, capsys):
        self._write_board_config(tmp_path)
        self._patch_client_with_players(monkeypatch, tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert rc == 0
        ids = json.loads((tmp_path / "draft" / "sleeper_ids.json").read_text())
        assert ids == {"jamarr chase:WR": "p1", "christian mccaffrey:RB": "p2"}
        assert "sync will be live" in capsys.readouterr().out

    def test_no_board_configured_skips_silently_and_says_so(self, tmp_path, monkeypatch, capsys):
        # No config.yml at all -- the common case on a fresh clone, before
        # the FantasyPros CSVs are downloaded (see the printed next steps).
        _patch_client(monkeypatch, tmp_path=tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert rc == 0
        assert not (tmp_path / "draft" / "sleeper_ids.json").exists()
        assert "sleeper_ids.json" in capsys.readouterr().out  # reminder in Next steps

    def test_board_csv_configured_but_files_missing_still_prints_next_steps(self, tmp_path, monkeypatch, capsys):
        # Regression: this is the ACTUAL shape a fresh clone of this repo
        # ships in (config.yml lists draft.board_csv entries; the CSVs
        # themselves are gitignored and not downloaded yet). Before the
        # board.py fix, a configured-but-missing path raised
        # FileNotFoundError, which escaped the `except (ValueError,
        # ImportError)` here and crashed the wizard before it ever printed
        # "Next steps" -- worse than the fully-unconfigured case above.
        (tmp_path / "config.yml").write_text(
            yaml.safe_dump({
                "draft": {
                    "board_csv": [str(tmp_path / "does_not_exist.csv")],
                    "intel_file": str(tmp_path / "no-intel.yml"),
                }
            }), encoding="utf-8"
        )
        _patch_client(monkeypatch, tmp_path=tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert rc == 0
        assert not (tmp_path / "draft" / "sleeper_ids.json").exists()
        assert "Next steps" in capsys.readouterr().out

    def test_dry_run_never_fetches_the_players_dump_or_writes_it(self, tmp_path, monkeypatch):
        self._write_board_config(tmp_path)
        # No "/players/nfl" route -- an unrouted URL fails the test loudly
        # (see _opener), so if --dry-run fetched it anyway this would raise.
        _patch_client(monkeypatch, tmp_path=tmp_path)
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert not (tmp_path / "draft").exists()


class TestInitLeagueEdgeCases:
    def test_unknown_username_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(init_league, "SleeperClient", lambda: SleeperClient(
            cache_dir=tmp_path / "cache",
            opener=_opener({"/state/nfl": {"week": 3, "season": "2026"}, "/user/nobody": None}),
        ))
        rc = init_league.main(["--username", "nobody", "--config-dir", str(tmp_path)])
        assert rc == 1

    def test_multiple_leagues_without_league_id_lists_and_fails(self, tmp_path, monkeypatch, capsys):
        second = dict(_LEAGUE, league_id="L2", name="Second League")
        _patch_client(monkeypatch, league=[_LEAGUE, second][0], tmp_path=tmp_path)
        routes = {
            "/state/nfl": {"week": 3, "season": "2026"},
            "/user/duncan": {"user_id": "u1", "username": "duncan"},
            "/leagues/nfl/2026": [_LEAGUE, second],
        }
        monkeypatch.setattr(init_league, "SleeperClient", lambda: SleeperClient(
            cache_dir=tmp_path / "cache", opener=_opener(routes)
        ))
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path)])
        assert rc == 1
        assert "Second League" in capsys.readouterr().err

    def test_league_id_selects_among_multiple(self, tmp_path, monkeypatch):
        second = dict(_LEAGUE, league_id="L2", name="Second League")
        routes = {
            "/state/nfl": {"week": 3, "season": "2026"},
            "/user/duncan": {"user_id": "u1", "username": "duncan"},
            "/leagues/nfl/2026": [_LEAGUE, second],
            "/league/L2/rosters": [{"roster_id": 3, "owner_id": "u1"}],
        }
        monkeypatch.setattr(init_league, "SleeperClient", lambda: SleeperClient(
            cache_dir=tmp_path / "cache", opener=_opener(routes)
        ))
        rc = init_league.main(["--username", "duncan", "--config-dir", str(tmp_path), "--league-id", "L2"])
        assert rc == 0
        overlay = yaml.safe_load((tmp_path / "config.local.yml").read_text())
        assert overlay["sleeper"]["league_id"] == "L2"
